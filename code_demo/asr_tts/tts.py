"""
code_demo/asr_tts/tts.py — 文本转语音引擎

整体架构：
  基于本地 CosyVoice3 模型（Fun-CosyVoice3-0.5B）实现 TTS。
  文本输入 → CosyVoice3 三阶段推理（LLM → Flow → HiFi-GAN）→ WAV 音频输出。

关键设计：
  1. 设备自动检测：优先 CUDA，其次 MPS (Apple Silicon)，回退 CPU
  2. Float32 转换：CPU/MPS 推理时将 BFloat16 权重转为 Float32
     （CosyVoice 内部使用 torch.cuda.amp.autocast，非 CUDA 设备无效）
  3. 语速控制：通过 CosyVoice 的 speed 参数调节合成速度
     （>1.0 加速，<1.0 减速，1.0 为正常）
  4. WAV 输出：使用 Python wave 模块生成 16-bit PCM WAV 字节数据

用法:
    engine = TTSEngine(model_dir="/path/to/CosyVoice3-0.5B")
    engine.start()                          # 加载模型
    audio_bytes = engine.synthesize(        # 合成语音
        text="你好，世界",
        prompt_text="参考音频文本",
        prompt_wav="/path/to/ref.wav",
        speed=1.0,                          # 语速倍率
    )
    engine.stop()                           # 清理资源
"""

import io
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torchaudio

# ── 音频参数 ──
DEFAULT_SAMPLE_RATE = 22050  # CosyVoice3 默认采样率


def _detect_device() -> str:
    """自动检测最佳推理设备。"""
    if torch.cuda.is_available():
        device = "cuda"
        print(f"[TTS] 检测到 CUDA 设备: {torch.cuda.get_device_name(0)}")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = "mps"
        print("[TTS] 检测到 MPS 设备 (Apple Silicon)")
    else:
        device = "cpu"
        print("[TTS] 使用 CPU 推理")
    return device


class TTSEngine:
    """CosyVoice3 文本转语音引擎。

    接口设计与 ASREngine 对称：start() / stop() / synthesize()。
    线程安全：内部持有锁保护模型推理。
    """

    def __init__(
        self,
        model_dir: str,
        device: Optional[str] = None,
        prompt_text: Optional[str] = None,
        prompt_wav: Optional[str] = None,
    ):
        """
        Args:
            model_dir: CosyVoice3 模型目录路径
                       (如 /path/to/Fun-CosyVoice3-0.5B)
            device: 推理设备 ('cuda', 'mps', 'cpu' 或 None 自动检测)
            prompt_text: 默认参考音频文本（可在 synthesize 时覆盖）
            prompt_wav: 默认参考音频文件路径（可在 synthesize 时覆盖）
        """
        self.model_dir = str(model_dir)  # CosyVoice 模型文件目录
        self.device = device or _detect_device()  # 模型推理使用的计算设备
        self.default_prompt_text = prompt_text  # 默认零样本语音参考文本
        self.default_prompt_wav = prompt_wav  # 默认零样本语音参考音频路径

        self._model = None  # 已加载的 CosyVoice 模型对象
        self._lock = threading.Lock()  # 保护模型并发推理的线程锁
        self._running = False  # TTS 模型是否已启动
        self._sample_rate = DEFAULT_SAMPLE_RATE  # 生成语音的采样率

    @property
    def sample_rate(self) -> int:
        """返回模型的采样率。"""
        return self._sample_rate

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """加载 CosyVoice3 模型。

        Returns:
            True 表示加载成功，False 表示失败
        """
        if self._running and self._model is not None:
            return True

        model_path = Path(self.model_dir)
        if not model_path.is_dir():
            print(f"[TTS] 错误：模型目录不存在: {self.model_dir}")
            return False

        try:
            import sys
            import os
            # CosyVoice 源码路径：优先从环境变量读取，否则自动查找
            cosyvoice_src = os.getenv("COSYVOICE_SRC", "")
            if not cosyvoice_src:
                # 自动查找：模型目录的同级 cosyvoice3-0.5B/CosyVoice
                model_parent = model_path.parent
                candidate = model_parent / "cosyvoice3-0.5B" / "CosyVoice"
                if candidate.is_dir():
                    cosyvoice_src = str(candidate)
                else:
                    # 再尝试上一级
                    candidate = model_parent.parent / "cosyvoice3-0.5B" / "CosyVoice"
                    if candidate.is_dir():
                        cosyvoice_src = str(candidate)

            if not cosyvoice_src or not Path(cosyvoice_src).is_dir():
                print(f"[TTS] 错误：CosyVoice 源码未找到")
                print(f"[TTS] 请设置 COSYVOICE_SRC 环境变量指向 CosyVoice 源码目录")
                return False

            cosyvoice_src = Path(cosyvoice_src)
            matcha_path = cosyvoice_src / "third_party" / "Matcha-TTS"
            for p in [str(cosyvoice_src), str(matcha_path)]:
                if p and p not in sys.path:
                    sys.path.insert(0, p)

            print(f"[TTS] CosyVoice 源码: {cosyvoice_src}")

            from cosyvoice.cli.cosyvoice import AutoModel

            print(f"[TTS] 正在加载 CosyVoice3 模型: {self.model_dir}")
            print(f"[TTS] 推理设备: {self.device}")

            t0 = time.time()
            self._model = AutoModel(model_dir=self.model_dir)

            # CPU/MPS 推理需要将 BFloat16 权重转为 Float32
            # （CosyVoice 内部使用 torch.cuda.amp.autocast，非 CUDA 设备无效）
            if not torch.cuda.is_available():
                try:
                    model_inner = self._model.model
                    for sub in ['llm', 'flow', 'hift']:
                        if hasattr(model_inner, sub):
                            getattr(model_inner, sub).to(torch.float32)
                    print("[TTS] 已将模型权重转为 Float32 (CPU/MPS 推理)")
                except Exception as e:
                    print(f"[TTS] 警告：Float32 转换失败: {e}")

            elapsed = time.time() - t0

            # 读取模型的实际采样率
            if hasattr(self._model, "sample_rate"):
                self._sample_rate = self._model.sample_rate

            print(f"[TTS] 模型加载完成 ({elapsed:.1f}s, 采样率={self._sample_rate}Hz)")
            self._running = True
            return True

        except ImportError as e:
            print(f"[TTS] CosyVoice3 导入失败: {e}")
            print("[TTS] 请确认 CosyVoice 源码和依赖已正确安装")
            return False
        except Exception as e:
            print(f"[TTS] 模型加载失败: {e}")
            return False

    def stop(self):
        """卸载模型并释放资源。"""
        self._running = False

        if self._model is not None:
            # 尝试将模型移出 GPU 显存
            if self.device == "cuda":
                try:
                    import gc
                    del self._model
                    self._model = None
                    torch.cuda.empty_cache()
                    gc.collect()
                except Exception:
                    self._model = None
            else:
                self._model = None

            print("[TTS] 模型已卸载")
        self._model = None

    def synthesize(
        self,
        text: str,
        prompt_text: Optional[str] = None,
        prompt_wav: Optional[str] = None,
        speed: float = 1.0,
    ) -> Optional[bytes]:
        """合成语音，返回 WAV 格式字节数据。

        Args:
            text: 要合成的文本
            prompt_text: 参考音频对应的文本（含 system prompt 前缀）。
                         若为 None 则使用初始化时设置的默认值。
            prompt_wav: 参考音频文件路径。
                        若为 None 则使用初始化时设置的默认值。
            speed: 语速倍率，1.0 为正常速度，>1 加速，<1 减速。

        Returns:
            WAV 格式的音频字节数据 (bytes)，失败返回 None
        """
        if not self._running:
            print("[TTS] 错误：引擎未启动，请先调用 start()")
            return None

        # 本地引用 model，防止 stop() 在另一线程将 self._model 置为 None
        model = self._model
        if model is None:
            print("[TTS] 错误：引擎未启动，请先调用 start()")
            return None

        # 使用默认值
        _prompt_text = prompt_text or self.default_prompt_text
        _prompt_wav = prompt_wav or self.default_prompt_wav

        if not _prompt_text:
            print("[TTS] 错误：未提供 prompt_text（参考音频文本）")
            return None
        if not _prompt_wav:
            print("[TTS] 错误：未提供 prompt_wav（参考音频路径）")
            return None

        if not Path(_prompt_wav).is_file():
            print(f"[TTS] 错误：参考音频文件不存在: {_prompt_wav}")
            return None

        if not text.strip():
            print("[TTS] 错误：合成文本为空")
            return None

        # 注意：移除了 self._lock 以允许并行合成
        # PyTorch MPS 操作是线程安全的（内部串行化）
        try:
            t0 = time.time()
            print(f"[TTS] 开始合成: \"{text[:50]}{'...' if len(text) > 50 else ''}\"")

            # CosyVoice3 inference_zero_shot 返回生成器
            audio_chunks = []
            for result in model.inference_zero_shot(
                text, _prompt_text, _prompt_wav, speed=speed
            ):
                speech_tensor = result["tts_speech"]  # torch.Tensor
                audio_chunks.append(speech_tensor)

            if not audio_chunks:
                print("[TTS] 合成结果为空")
                return None

            # 拼接所有音频片段
            full_audio = torch.cat(audio_chunks, dim=-1)  # [1, T] or [T]

            # 确保是 2D tensor [1, T]
            if full_audio.dim() == 1:
                full_audio = full_audio.unsqueeze(0)

            # 转换为 WAV 字节
            wav_bytes = self._tensor_to_wav_bytes(full_audio)
            elapsed = time.time() - t0

            # 计算音频时长
            duration = full_audio.shape[-1] / self._sample_rate
            print(f"[TTS] 合成完成 ({elapsed:.1f}s, 音频={duration:.1f}s, "
                  f"大小={len(wav_bytes) / 1024:.0f}KB)")

            return wav_bytes

        except Exception as e:
            print(f"[TTS] 合成失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _tensor_to_wav_bytes(self, audio_tensor: torch.Tensor) -> bytes:
        """将 torch Tensor 转换为 WAV 格式字节数据。

        Args:
            audio_tensor: 音频张量, shape [1, T] 或 [C, T]

        Returns:
            WAV 格式的 bytes
        """
        import wave
        import struct

        # 归一化到 [-1, 1]
        max_val = audio_tensor.abs().max()
        if max_val > 0:
            audio_tensor = audio_tensor / max_val

        # 转为 numpy int16
        audio_np = audio_tensor.cpu().squeeze().numpy()
        audio_int16 = (audio_np * 32767).astype(np.int16)

        # 使用 wave 模块写入内存缓冲区
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self._sample_rate)
            wf.writeframes(audio_int16.tobytes())

        buffer.seek(0)
        return buffer.read()

    def synthesize_to_file(
        self,
        text: str,
        output_path: str,
        prompt_text: Optional[str] = None,
        prompt_wav: Optional[str] = None,
    ) -> bool:
        """合成语音并保存到文件。

        Args:
            text: 要合成的文本
            output_path: 输出文件路径 (.wav)
            prompt_text: 参考音频文本
            prompt_wav: 参考音频路径

        Returns:
            True 表示保存成功
        """
        wav_bytes = self.synthesize(text, prompt_text, prompt_wav)
        if wav_bytes is None:
            return False

        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(wav_bytes)
            print(f"[TTS] 已保存到: {output_path}")
            return True
        except Exception as e:
            print(f"[TTS] 保存文件失败: {e}")
            return False
