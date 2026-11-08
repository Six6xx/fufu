"""
实时画面捕获模块 - 异步屏幕截屏流，支持变化检测和关键帧缓存。
"""

import asyncio
import io
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import imagehash
from PIL import Image

from .controller import DesktopController

logger = logging.getLogger(__name__)


@dataclass
class Frame:
    """单帧截屏数据。"""

    image: bytes  # PNG bytes
    timestamp: float  # 屏幕帧采集时间
    description: str = ""  # VLM 分析结果（异步填充）
    audio_text: str = ""  # 对应时段的 ASR 文本
    _hash: str = field(default="", repr=False)  # 用于比较屏幕变化的图像哈希


class ScreenStream:
    """异步屏幕画面捕获流，支持变化检测和关键帧缓存。"""

    # 默认变化检测阈值（perceptual hash 汉明距离）
    DEFAULT_HASH_THRESHOLD = 8
    # 视频播放判定：10 秒内关键帧数超过此值视为视频
    VIDEO_FRAME_THRESHOLD = 5
    VIDEO_WINDOW_SECONDS = 10.0

    def __init__(self, fps: float = 1.0, buffer_seconds: int = 30):
        """
        Args:
            fps: 截屏频率（帧/秒）
            buffer_seconds: 关键帧缓冲时长（秒）
        """
        self._fps = fps  # 屏幕采集目标帧率
        self._buffer_seconds = buffer_seconds  # 屏幕帧缓冲区覆盖的秒数
        self._interval = 1.0 / fps  # 相邻屏幕采集的时间间隔

        # 关键帧缓冲区 - maxlen 按 buffer_seconds * fps 估算上限
        max_frames = int(buffer_seconds * fps)
        self._keyframes: deque[Frame] = deque(maxlen=max(max_frames, 30))  # 保留的关键屏幕帧

        # 最新帧（含非关键帧）
        self._latest_frame: Optional[Frame] = None  # 最近采集到的屏幕帧
        self._last_hash: Optional[str] = None  # 上一帧的图像哈希

        # 控制
        self._running = False  # 屏幕采集是否运行
        self._task: Optional[asyncio.Task] = None  # 当前屏幕采集异步任务
        self._controller = DesktopController()  # 负责截取屏幕的桌面控制器

        # 变化检测阈值
        self._hash_threshold = self.DEFAULT_HASH_THRESHOLD  # 判断屏幕变化的哈希距离阈值

        logger.info(
            "ScreenStream 初始化: fps=%.1f, buffer=%ds, maxlen=%d",
            fps,
            buffer_seconds,
            max_frames,
        )

    # ─── 生命周期 ─────────────────────────────────────────────

    async def start(self) -> None:
        """启动截屏循环。"""
        if self._running:
            logger.warning("ScreenStream 已在运行中")
            return

        self._running = True
        self._task = asyncio.create_task(self._capture_loop())
        logger.info("ScreenStream 已启动")

    async def stop(self) -> None:
        """停止截屏循环。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("ScreenStream 已停止")

    # ─── 数据访问 ─────────────────────────────────────────────

    def get_latest_frame(self) -> Optional[Frame]:
        """获取最新一帧。"""
        return self._latest_frame

    def get_recent_frames(self, n: int = 5) -> list[Frame]:
        """
        获取最近 N 个关键帧。

        Args:
            n: 需要的帧数

        Returns:
            关键帧列表，按时间升序
        """
        frames = list(self._keyframes)
        return frames[-n:] if len(frames) > n else frames

    def is_video_playing(self) -> bool:
        """
        根据帧变化频率判断是否在播放视频。

        逻辑：最近 VIDEO_WINDOW_SECONDS 秒内关键帧数量超过阈值 → 视频播放中。
        """
        now = time.time()
        cutoff = now - self.VIDEO_WINDOW_SECONDS
        recent_count = sum(
            1 for f in self._keyframes if f.timestamp >= cutoff
        )
        is_video = recent_count > self.VIDEO_FRAME_THRESHOLD
        logger.debug(
            "is_video_playing: recent_keyframes=%d, threshold=%d, result=%s",
            recent_count,
            self.VIDEO_FRAME_THRESHOLD,
            is_video,
        )
        return is_video

    # ─── 内部逻辑 ─────────────────────────────────────────────

    async def _capture_loop(self) -> None:
        """截屏主循环。"""
        logger.debug("截屏循环开始, interval=%.3fs", self._interval)

        while self._running:
            loop_start = time.time()
            try:
                # 在线程池中执行截屏（mss 是同步的）
                image_bytes = await asyncio.get_event_loop().run_in_executor(
                    None, self._controller.screenshot
                )

                current_hash = self._compute_frame_hash(image_bytes)
                now = time.time()

                frame = Frame(
                    image=image_bytes,
                    timestamp=now,
                    _hash=current_hash,
                )
                self._latest_frame = frame

                # 变化检测：与上一帧对比
                if self._last_hash is None or self._is_significant_change(
                    self._last_hash, current_hash
                ):
                    self._keyframes.append(frame)
                    self._last_hash = current_hash
                    logger.debug(
                        "关键帧捕获 #%d, hash=%s", len(self._keyframes), current_hash[:8]
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("截屏循环异常: %s", e, exc_info=True)

            # 精确控制帧率
            elapsed = time.time() - loop_start
            sleep_time = max(0, self._interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    def _compute_frame_hash(self, image_bytes: bytes) -> str:
        """
        计算图片感知哈希，用于变化检测。

        Args:
            image_bytes: PNG 格式图片 bytes

        Returns:
            哈希字符串
        """
        try:
            img = Image.open(io.BytesIO(image_bytes))
            # 使用 phash（感知哈希），对缩放和轻微变化更鲁棒
            h = imagehash.phash(img)
            return str(h)
        except Exception as e:
            logger.error("计算帧哈希失败: %s", e)
            # 返回空字符串，确保被视为「有变化」
            return ""

    def _is_significant_change(self, hash1: str, hash2: str) -> bool:
        """
        判断两帧之间是否有显著变化。

        Args:
            hash1: 前一帧的哈希
            hash2: 当前帧的哈希

        Returns:
            True 表示有显著变化
        """
        if not hash1 or not hash2:
            return True

        try:
            h1 = imagehash.hex_to_hash(hash1)
            h2 = imagehash.hex_to_hash(hash2)
            distance = h1 - h2  # 汉明距离
            is_changed = distance > self._hash_threshold
            if is_changed:
                logger.debug("帧变化检测: distance=%d > threshold=%d", distance, self._hash_threshold)
            return is_changed
        except Exception as e:
            logger.error("哈希比较失败: %s", e)
            return True

    # ─── 属性 ─────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """是否正在运行。"""
        return self._running

    @property
    def keyframe_count(self) -> int:
        """当前缓冲区中的关键帧数量。"""
        return len(self._keyframes)
