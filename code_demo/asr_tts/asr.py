"""
code_demo/asr_tts/asr.py — 实时语音识别引擎

基于 dashscope Recognition (WebSocket 双向流) + pyaudio 麦克风采集。
音频输入 → 流式 ASR → 实时文本输出 → 累积为完整字符串 → 送入 LLM。

用法:
    engine = ASREngine()
    engine.start()                # 启动麦克风 + ASR 连接
    text = engine.listen()        # 阻塞式录制，返回完整识别文本
    engine.stop()                 # 释放资源
"""

import os
import threading
import time
from queue import Queue
from typing import Optional

import pyaudio
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

# ── 音频参数 ──
SAMPLE_RATE = 16000       # 采样率 Hz
CHANNELS = 1               # 单声道
CHUNK_SIZE = 1600          # 每次读取的帧数 (100ms)
FORMAT = pyaudio.paInt16   # 16-bit PCM
ASR_FORMAT = "pcm"         # dashscope 格式标识

# ── ASR 模型 ──
ASR_MODEL = "paraformer-realtime-v2"


class ASRCallback(RecognitionCallback):
    """自定义回调：累积语音识别结果到文本缓冲区。"""

    def __init__(self):
        self._lock = threading.Lock()              # 实例级共享锁
        self._text_buffer: list[str] = []        # 按顺序存储每句识别文本
        self._current_sentence: str = ""          # 当前正在识别的句子
        self._result_queue: Queue = Queue()        # 线程安全的结果队列
        self._is_complete: bool = False  # 当前识别是否已完成
        self._error: Optional[str] = None  # 最近一次识别错误信息

    @property
    def accumulated_text(self) -> str:
        """返回当前累积的完整识别文本。"""
        with self._lock:
            return "".join(self._text_buffer) + self._current_sentence

    @property
    def text_parts(self) -> list[str]:
        """返回已完成的句子列表。"""
        with self._lock:
            return list(self._text_buffer)

    @property
    def is_complete(self) -> bool:
        return self._is_complete

    @property
    def error(self) -> Optional[str]:
        return self._error

    def on_open(self) -> None:
        print("[ASR] 语音识别连接已建立")

    def on_complete(self) -> None:
        self._is_complete = True
        print("[ASR] 语音识别完成")

    def on_error(self, result: RecognitionResult) -> None:
        self._error = result.message if result else "未知 ASR 错误"
        print(f"[ASR] 错误: {self._error}")

    def on_close(self) -> None:
        pass

    def on_event(self, result: RecognitionResult) -> None:
        """每次识别出一个句子片段时触发。"""
        sentence = result.get_sentence()
        if sentence is None:
            return

        text = sentence.get("text", "")
        if not text:
            return

        # 判断是否是句子结束
        if RecognitionResult.is_sentence_end(sentence):
            with self._lock:
                self._text_buffer.append(text)
                self._current_sentence = ""
        else:
            # 中间结果：更新当前句子
            with self._lock:
                self._current_sentence = text

        # 放入结果队列（供外部实时消费）
        self._result_queue.put({
            "text": text,
            "is_end": RecognitionResult.is_sentence_end(sentence),
        })


class ASREngine:
    """实时语音识别引擎。"""

    def __init__(
        self,
        model: str = ASR_MODEL,
        sample_rate: int = SAMPLE_RATE,
        api_key: str = None,
    ):
        self.model = model  # ASR 服务使用的模型名称
        self.sample_rate = sample_rate  # 输入音频采样率
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")  # 访问 ASR 服务的 API Key

        self._audio: Optional[pyaudio.PyAudio] = None  # PyAudio 音频设备管理对象
        self._stream: Optional[pyaudio.Stream] = None  # 当前麦克风音频流
        self._asr: Optional[Recognition] = None  # 底层 ASR 识别客户端
        self._callback: Optional[ASRCallback] = None  # ASR 识别结果回调
        self._running: bool = False  # 语音识别服务是否运行
        self._capture_thread: Optional[threading.Thread] = None  # 麦克风音频采集线程

    def start(self) -> bool:
        """启动 ASR 引擎：打开麦克风 + 建立 WebSocket 连接。"""
        if self._running:
            return True

        if not self.api_key:
            print("[ASR] 错误：DASHSCOPE_API_KEY 未设置")
            return False

        # 创建回调
        self._callback = ASRCallback()

        # 初始化 dashscope Recognition
        try:
            self._asr = Recognition(
                model=self.model,
                callback=self._callback,
                format=ASR_FORMAT,
                sample_rate=self.sample_rate,
            )
            self._asr.start()
        except Exception as e:
            print(f"[ASR] Recognition 启动失败: {e}")
            return False

        # 初始化 pyaudio 麦克风
        try:
            self._audio = pyaudio.PyAudio()
            self._stream = self._audio.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
            )
        except Exception as e:
            print(f"[ASR] 麦克风初始化失败: {e}")
            self._asr.stop()
            return False

        self._running = True
        return True

    def stop(self):
        """停止 ASR 引擎并释放资源。"""
        self._running = False

        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2)

        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        if self._audio:
            try:
                self._audio.terminate()
            except Exception:
                pass
            self._audio = None

        if self._asr:
            try:
                self._asr.stop()
            except Exception:
                pass
            self._asr = None

        self._callback = None

    def listen(self, timeout: float = 30.0) -> str:
        """
        开始录音并等待用户停止。
        返回识别出的完整文本。

        Args:
            timeout: 最大录音时长（秒），超时自动停止

        Returns:
            识别出的文本字符串
        """
        if not self._running:
            if not self.start():
                return ""
            # start() 重置了 _callback，需要重新获取
        if self._callback is None:
            return ""

        callback = self._callback

        # 重置缓冲区
        with callback._lock:
            callback._text_buffer.clear()
            callback._current_sentence = ""
        callback._is_complete = False
        callback._error = None

        # 清空结果队列
        while not callback._result_queue.empty():
            callback._result_queue.get_nowait()

        print("[ASR] 🎤 开始录音... (按 Enter 停止)")
        start_time = time.time()

        # 在后台线程中采集麦克风数据并发送给 ASR
        stop_flag = threading.Event()

        def capture_loop():
            """麦克风采集线程。"""
            chunk_count = 0
            while self._running and not stop_flag.is_set():
                try:
                    if self._stream is None:
                        break
                    data = self._stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    if data and self._asr:
                        self._asr.send_audio_frame(data)
                        chunk_count += 1
                except Exception as e:
                    if "Stream closed" not in str(e) and "Input overflowed" not in str(e):
                        print(f"[ASR] 采集错误: {e}")
                    break

            total_ms = chunk_count * (CHUNK_SIZE / self.sample_rate * 1000)
            print(f"\n[ASR] 采集结束 (共 {total_ms/1000:.1f}s)")

        self._capture_thread = threading.Thread(target=capture_loop, daemon=True)
        self._capture_thread.start()

        # 显示实时识别结果
        last_display = ""
        try:
            while not stop_flag.is_set():
                # 超时检查
                if time.time() - start_time > timeout:
                    print("\n[ASR] 录音超时，自动停止")
                    stop_flag.set()
                    break

                # 检查是否有新的识别结果
                try:
                    result = callback._result_queue.get(timeout=0.1)
                    current = callback.accumulated_text
                    if current != last_display:
                        print(f"\r[ASR] 📝 {current}", end="", flush=True)
                        last_display = current
                except Exception:
                    pass

                # 检查 Enter 键（非阻塞）
                import sys, select
                if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                    input()  # 消费 Enter
                    stop_flag.set()
        except KeyboardInterrupt:
            stop_flag.set()

        print()  # 换行

        # 等待采集线程结束
        self._capture_thread.join(timeout=3)

        # 收集最终文本
        final_text = callback.accumulated_text.strip()

        if callback.error:
            print(f"[ASR] 识别过程出错: {callback.error}")

        return final_text
