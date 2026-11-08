"""
桌面控制器模块 - 提供跨平台桌面控制能力（鼠标、键盘、截屏）。
"""

import logging
import time

import mss
import pyautogui

logger = logging.getLogger(__name__)

# pyautogui 全局设置
pyautogui.PAUSE = 0.1  # 每次操作后默认暂停 0.1 秒


class DesktopController:
    """跨平台桌面控制类，封装鼠标、键盘和截屏操作。"""

    def __init__(self):
        self._sct = mss.mss()  # 桌面截图采集器
        logger.info("DesktopController 初始化完成, FAILSAFE=%s", pyautogui.FAILSAFE)

    # ─── 截屏 ───────────────────────────────────────────────

    def screenshot(self, region: dict | None = None) -> bytes:
        """
        截取全屏或指定区域，返回 PNG bytes。

        Args:
            region: 可选截取区域，格式 {"x": int, "y": int, "width": int, "height": int}

        Returns:
            PNG 格式的 bytes 数据
        """
        try:
            if region:
                monitor = {
                    "left": region["x"],
                    "top": region["y"],
                    "width": region["width"],
                    "height": region["height"],
                }
            else:
                monitor = self._sct.monitors[0]  # 全屏（包含所有显示器）

            sct_img = self._sct.grab(monitor)
            png_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)
            logger.debug("截屏完成, region=%s, size=%d bytes", region, len(png_bytes))
            return png_bytes
        except Exception as e:
            logger.error("截屏失败: %s", e, exc_info=True)
            raise

    # ─── 鼠标操作 ─────────────────────────────────────────────

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        """
        鼠标点击。

        Args:
            x: 屏幕 x 坐标
            y: 屏幕 y 坐标
            button: 'left', 'right', 'middle'
            clicks: 点击次数
        """
        try:
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            logger.debug("点击 (%d, %d), button=%s, clicks=%d", x, y, button, clicks)
        except pyautogui.FailSafeException:
            logger.warning("FailSafe 触发：鼠标移动到屏幕角落，操作中止")
            raise
        except Exception as e:
            logger.error("点击操作失败: %s", e, exc_info=True)
            raise

    def move_to(self, x: int, y: int, duration: float = 0.3) -> None:
        """
        鼠标移动到指定位置。

        Args:
            x: 目标 x 坐标
            y: 目标 y 坐标
            duration: 移动耗时（秒）
        """
        try:
            pyautogui.moveTo(x=x, y=y, duration=duration)
            logger.debug("移动鼠标到 (%d, %d), duration=%.2f", x, y, duration)
        except pyautogui.FailSafeException:
            logger.warning("FailSafe 触发：鼠标移动到屏幕角落，操作中止")
            raise
        except Exception as e:
            logger.error("鼠标移动失败: %s", e, exc_info=True)
            raise

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
        """
        从 (x1, y1) 拖拽到 (x2, y2)。

        Args:
            x1, y1: 起始坐标
            x2, y2: 目标坐标
            duration: 拖拽耗时（秒）
        """
        try:
            pyautogui.moveTo(x1, y1, duration=0.1)
            time.sleep(0.05)
            pyautogui.drag(
                x2 - x1, y2 - y1, duration=duration, button="left"
            )
            logger.debug("拖拽 (%d,%d)->(%d,%d), duration=%.2f", x1, y1, x2, y2, duration)
        except pyautogui.FailSafeException:
            logger.warning("FailSafe 触发：鼠标移动到屏幕角落，操作中止")
            raise
        except Exception as e:
            logger.error("拖拽操作失败: %s", e, exc_info=True)
            raise

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> None:
        """
        滚动鼠标滚轮。

        Args:
            clicks: 滚动量，正数向上，负数向下
            x: 可选，滚动位置 x
            y: 可选，滚动位置 y
        """
        try:
            kwargs: dict = {"clicks": clicks}
            if x is not None:
                kwargs["x"] = x
            if y is not None:
                kwargs["y"] = y
            pyautogui.scroll(**kwargs)
            logger.debug("滚动 clicks=%d, pos=(%s, %s)", clicks, x, y)
        except pyautogui.FailSafeException:
            logger.warning("FailSafe 触发：鼠标移动到屏幕角落，操作中止")
            raise
        except Exception as e:
            logger.error("滚动操作失败: %s", e, exc_info=True)
            raise

    # ─── 键盘操作 ─────────────────────────────────────────────

    def type_text(self, text: str, interval: float = 0.05) -> None:
        """
        键盘输入文字。

        Args:
            text: 要输入的文本
            interval: 每个字符之间的间隔（秒）
        """
        try:
            pyautogui.typewrite(text, interval=interval)
            logger.debug("输入文字: %s (len=%d)", text[:20], len(text))
        except Exception as e:
            logger.error("键盘输入失败: %s", e, exc_info=True)
            raise

    def hotkey(self, *keys: str) -> None:
        """
        执行组合键，如 hotkey('command', 'c')。

        Args:
            keys: 按键序列
        """
        try:
            pyautogui.hotkey(*keys)
            logger.debug("组合键: %s", "+".join(keys))
        except Exception as e:
            logger.error("组合键操作失败: %s", e, exc_info=True)
            raise

    # ─── 信息查询 ─────────────────────────────────────────────

    def get_screen_size(self) -> tuple[int, int]:
        """
        获取屏幕分辨率。

        Returns:
            (width, height) 元组
        """
        size = pyautogui.size()
        logger.debug("屏幕分辨率: %dx%d", size.width, size.height)
        return (size.width, size.height)
