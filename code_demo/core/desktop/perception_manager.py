"""
core/desktop/perception_manager.py — 感知流水线统一管理器

统一管理桌面感知流水线：截屏流 + 系统音频捕获 + ASR 转写 + VLM 场景理解。

启动时机：
  - Agent 启动时不自动开启（节省资源）
  - 由外部调用 start_watching()（用户指令或认知循环 act 触发）
  - 视频结束/用户离开时调用 stop_watching()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .controller import DesktopController
from .screen_stream import ScreenStream
from .audio_capture import SystemAudioCapture
from .vision import VisionInterpreter

logger = logging.getLogger(__name__)

# 场景分析默认间隔（秒）
_SCENE_ANALYSIS_INTERVAL = 10.0


class PerceptionManager:
    """统一管理桌面感知流水线：截屏 + 录音 + ASR + VLM 理解"""

    def __init__(self):
        self.controller = DesktopController()  # 执行桌面控制和截图的控制器
        self.screen_stream = ScreenStream()  # 管理连续屏幕帧的采集器
        self.audio_capture = SystemAudioCapture()  # 采集系统音频的组件
        self.vision = VisionInterpreter()  # 自动加载 VLM 配置
        self._watching = False  # 环境持续观察是否开启
        self._scene_task: asyncio.Task | None = None  # 当前环境观察异步任务
        self._last_scene_summary: str = ""  # 最近一次场景理解摘要
        self._mode: str = "full"  # 当前感知模式

    # ─── 生命周期 ─────────────────────────────────────────────

    async def start_watching(self, mode: str = "full") -> None:
        """
        开始持续感知。

        Args:
            mode: 感知模式
              - "full": 完整流水线（LLM 模式：截屏+VLM场景分析+音频捕获）
              - "stream_only": 仅截屏流（VLM 模式：截图由伴侣AI直接理解，省去独立VLM分析）

        幂等调用：已在运行时不重复启动。
        """
        if self._watching:
            logger.warning("[PerceptionManager] 已在感知中，忽略重复启动")
            return

        logger.info("[PerceptionManager] 启动感知流水线 (mode=%s)...", mode)

        # 1. 启动截屏流（两种模式都需要）
        await self.screen_stream.start()

        if mode == "full":
            # 2. 启动音频捕获（仅 full 模式）
            self.audio_capture.set_asr_callback(self._asr_callback)
            await self.audio_capture.start()

            # 3. 启动定期场景分析协程（仅 full 模式：独立VLM分析）
            self._scene_task = asyncio.create_task(self._scene_analysis_loop())
        else:
            # stream_only 模式：不启动音频捕获和VLM场景分析
            logger.info("[PerceptionManager] stream_only 模式：跳过音频捕获和场景分析")

        self._watching = True
        self._mode = mode  # 记录当前模式
        logger.info("[PerceptionManager] 感知流水线已启动 (mode=%s)", mode)

    async def stop_watching(self) -> None:
        """停止所有感知，释放后台协程。"""
        if not self._watching:
            return

        logger.info("[PerceptionManager] 停止感知流水线...")

        # 取消场景分析协程（仅 full 模式启动了）
        if self._scene_task and not self._scene_task.done():
            self._scene_task.cancel()
            try:
                await self._scene_task
            except asyncio.CancelledError:
                pass
        self._scene_task = None

        # 停止音频捕获（仅 full 模式启动了）
        if self._mode == "full":
            await self.audio_capture.stop()

        # 停止截屏流（两种模式都需要）
        await self.screen_stream.stop()

        self._watching = False
        self._last_scene_summary = ""
        logger.info("[PerceptionManager] 感知流水线已停止")

    @property
    def is_watching(self) -> bool:
        """是否正在感知。"""
        return self._watching

    # ─── 数据访问（供认知循环注入） ──────────────────────────────

    def get_screen_summary(self) -> str:
        """
        获取当前感知摘要（供认知循环上下文注入）。

        返回格式包括：
          - 是否在观看视频
          - 最近场景描述
          - 最近音频转写文本摘要
        """
        if not self._watching:
            return ""

        parts: list[str] = []

        # 视频状态
        is_video = self.screen_stream.is_video_playing()
        if is_video:
            parts.append("[视频播放中]")
        else:
            parts.append("[桌面静态]")

        # 最近场景描述
        if self._last_scene_summary:
            parts.append(f"场景: {self._last_scene_summary}")

        # 最近音频转写
        recent_text = self.audio_capture.get_recent_transcripts(seconds=30)
        if recent_text:
            # 截断过长文本
            display_text = recent_text[:200] + "..." if len(recent_text) > 200 else recent_text
            parts.append(f"音频: {display_text}")

        return " | ".join(parts)

    async def get_latest_screenshot_b64(self) -> str | None:
        """获取最新截图的 base64 编码（供多模态 LLM 直接使用）。

        - 感知已启动 → 取 screen_stream 最新帧的 image bytes
        - 感知未启动 → 临时调用 controller.screenshot() 截取一张
        - 压缩到 720p 短边 → base64 编码返回

        Returns:
            PNG 格式的 base64 字符串，失败时返回 None
        """
        from .vision import _compress_image, _encode_base64

        try:
            image_bytes: bytes | None = None

            if self._watching and self.screen_stream:
                frames = self.screen_stream.get_recent_frames(n=1)
                if frames:
                    image_bytes = frames[0].image
            
            if image_bytes is None:
                loop = asyncio.get_event_loop()
                image_bytes = await loop.run_in_executor(
                    None, self.controller.screenshot
                )

            if image_bytes is None:
                return None

            compressed = _compress_image(image_bytes, max_short_side=720)
            return _encode_base64(compressed)
        except Exception as e:
            logger.error("[PerceptionManager] get_latest_screenshot_b64 失败: %s", e)
            return None

    # ─── 交互式查询 ────────────────────────────────────────────

    async def answer_about_screen(self, question: str) -> str:
        """
        用户问当前画面内容时，立即截屏 + VLM 回答。

        Args:
            question: 用户的问题

        Returns:
            VLM 生成的自然语言回答
        """
        # 1. 立即截屏
        screenshot = await asyncio.get_event_loop().run_in_executor(
            None, self.controller.screenshot
        )

        # 2. 获取最近 ASR 文本作为上下文
        audio_text = self.audio_capture.get_recent_transcripts(seconds=15)

        # 3. VLM 回答
        answer = await self.vision.answer_question(screenshot, audio_text, question)
        return answer

    async def take_action(self, action: str, params: dict) -> dict[str, Any]:
        """
        执行桌面操作（click/type/hotkey/scroll/drag）。

        Args:
            action: 操作类型，支持 "click", "type", "hotkey", "scroll", "drag", "move"
            params: 操作参数字典

        Returns:
            {"success": bool, "message": str}
        """
        try:
            if action == "click":
                self.controller.click(
                    x=params["x"],
                    y=params["y"],
                    button=params.get("button", "left"),
                    clicks=params.get("clicks", 1),
                )
            elif action == "type":
                self.controller.type_text(
                    text=params["text"],
                    interval=params.get("interval", 0.05),
                )
            elif action == "hotkey":
                keys = params.get("keys", [])
                self.controller.hotkey(*keys)
            elif action == "scroll":
                self.controller.scroll(
                    clicks=params["clicks"],
                    x=params.get("x"),
                    y=params.get("y"),
                )
            elif action == "drag":
                self.controller.drag(
                    x1=params["x1"],
                    y1=params["y1"],
                    x2=params["x2"],
                    y2=params["y2"],
                    duration=params.get("duration", 0.5),
                )
            elif action == "move":
                self.controller.move_to(
                    x=params["x"],
                    y=params["y"],
                    duration=params.get("duration", 0.3),
                )
            else:
                return {"success": False, "message": f"未知操作类型: {action}"}

            return {"success": True, "message": f"{action} 操作执行成功"}

        except Exception as e:
            logger.error("[PerceptionManager] 桌面操作失败: action=%s, error=%s", action, e)
            return {"success": False, "message": f"{action} 操作失败: {e}"}

    async def analyze_and_locate(self, description: str) -> tuple[int, int] | None:
        """
        截屏 + VLM 定位 UI 元素。

        Args:
            description: UI 元素的自然语言描述

        Returns:
            (x, y) 坐标元组，未找到时返回 None
        """
        # 1. 截屏
        screenshot = await asyncio.get_event_loop().run_in_executor(
            None, self.controller.screenshot
        )

        # 2. VLM 定位
        coords = await self.vision.locate_element(screenshot, description)
        return coords

    # ─── 内部方法 ──────────────────────────────────────────────

    async def _scene_analysis_loop(self) -> None:
        """
        定期分析场景的后台协程。

        触发条件：
          - 每 _SCENE_ANALYSIS_INTERVAL 秒定期触发
          - 或 screen_stream 检测到画面在高频变化（视频播放中）时更频繁触发
        """
        logger.debug("[PerceptionManager] 场景分析循环启动")

        while True:
            try:
                await asyncio.sleep(_SCENE_ANALYSIS_INTERVAL)

                if not self._watching:
                    break

                # 获取最近关键帧
                recent_frames = self.screen_stream.get_recent_frames(n=3)
                if not recent_frames:
                    continue

                # 提取帧图片数据
                frame_images = [f.image for f in recent_frames]

                # 获取最近音频文本
                audio_text = self.audio_capture.get_recent_transcripts(seconds=15)

                # 调用 VLM 场景理解
                result = await self.vision.understand_scene(
                    frames=frame_images,
                    audio_text=audio_text,
                    context="",
                )

                # 更新场景摘要
                scene = result.get("scene", "")
                action = result.get("action", "")
                comment = result.get("comment", "")

                summary_parts = []
                if scene:
                    summary_parts.append(scene)
                if action:
                    summary_parts.append(action)
                if comment:
                    summary_parts.append(comment)

                self._last_scene_summary = "; ".join(summary_parts)

                # 将 VLM 分析结果回填到最新帧的 description
                if recent_frames:
                    latest = recent_frames[-1]
                    latest.description = self._last_scene_summary

                logger.debug(
                    "[PerceptionManager] 场景分析完成: %s",
                    self._last_scene_summary[:100],
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[PerceptionManager] 场景分析异常: %s", e, exc_info=True)
                # 出错不退出，等下一轮
                await asyncio.sleep(5.0)

        logger.debug("[PerceptionManager] 场景分析循环退出")

    async def _asr_callback(self, audio_bytes: bytes, sample_rate: int) -> str:
        """
        ASR 回调：将音频 bytes 转写为文字。

        尝试复用项目现有 ASR 引擎（dashscope）。
        如果 ASR 不可用或接口不兼容，做优雅降级（返回空字符串）。

        Args:
            audio_bytes: WAV 格式音频数据
            sample_rate: 采样率

        Returns:
            转写文本，ASR 不可用时返回空字符串
        """
        try:
            # 尝试使用 dashscope 的文件识别 API（非流式）
            from dashscope.audio.asr import Recognition

            import tempfile
            import os

            # 将 WAV 音频写入临时文件
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            try:
                # 使用 dashscope 的同步识别（在 executor 中运行避免阻塞）
                loop = asyncio.get_running_loop()
                transcript = await loop.run_in_executor(
                    None, self._run_asr_sync, tmp_path, sample_rate
                )
                return transcript
            finally:
                # 清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        except ImportError:
            logger.warning(
                "[PerceptionManager] dashscope 未安装，ASR 不可用，降级为空转写"
            )
            return ""
        except Exception as e:
            logger.warning(
                "[PerceptionManager] ASR 转写失败（优雅降级）: %s", e
            )
            return ""

    @staticmethod
    def _run_asr_sync(audio_path: str, sample_rate: int) -> str:
        """
        同步执行 ASR 识别（在线程池中调用）。

        使用 dashscope paraformer 模型进行文件级别离线识别。
        """
        try:
            import dashscope
            from dashscope.audio.asr import Transcription

            # 使用 dashscope 文件转写 API
            task_response = Transcription.async_call(
                model="paraformer-v2",
                file_urls=[audio_path],
                language_hints=["zh", "en"],
            )

            # 等待转写完成
            transcription = Transcription.wait(task=task_response.output.task_id)

            if transcription.output and transcription.output.results:
                texts = []
                for result in transcription.output.results:
                    if hasattr(result, "text") and result.text:
                        texts.append(result.text)
                return " ".join(texts)

            return ""

        except Exception as e:
            logger.debug("[PerceptionManager] dashscope 文件转写不可用: %s", e)
            # 回退：尝试使用 Recognition 流式方案的简化版本
            # 如果都不行，返回空字符串
            return ""
