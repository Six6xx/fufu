"""情感有限状态机 — 追踪情绪状态、优先级转换、自然衰减。"""

from dataclasses import dataclass
import time


@dataclass
class EmotionState:
    """情绪状态快照"""
    emotion: str = "neutral"     # 当前情绪
    intensity: float = 0.0       # 0.0~1.0
    triggered_at: float = 0.0    # 触发时间戳
    decay_rate: float = 0.02     # 每秒衰减量
    source: str = ""             # 触发来源（"user_input" / "ai_reply" / "segment"）


class EmotionFSM:
    """情感有限状态机 — 追踪、转换、衰减"""

    # 情绪转换权重：高优先级可覆盖低优先级
    PRIORITY = {
        "shocked": 9, "angry": 8, "hurt": 7,
        "happy": 6, "excited": 6, "sad": 5,
        "surprised": 5, "embarrassed": 4, "curious": 4,
        "thinking": 3, "pleased": 3, "focused": 2,
        "neutral": 0,
    }

    def __init__(self):
        self.state = EmotionState()  # 当前情绪状态及其强度
        self.history: list[tuple[str, float, float]] = []  # (emotion, intensity, timestamp)

    def transition(self, new_emotion: str, intensity: float, source: str = "") -> bool:
        """尝试转换情绪。返回是否成功转换。"""
        if new_emotion == "neutral":
            return False

        # 相同情绪：取较高强度
        if new_emotion == self.state.emotion:
            self.state.intensity = max(self.state.intensity, intensity)
            self.state.triggered_at = time.time()
            return True

        # 不同情绪：高优先级可覆盖，或当前已衰减到低强度时允许任何转换
        new_priority = self.PRIORITY.get(new_emotion, 0)
        cur_priority = self.PRIORITY.get(self.state.emotion, 0)

        if new_priority >= cur_priority or self.current_intensity() < 0.3:
            self._record_history()
            self.state = EmotionState(
                emotion=new_emotion,
                intensity=min(intensity, 1.0),
                triggered_at=time.time(),
                source=source,
            )
            return True
        return False

    def tick(self) -> EmotionState:
        """心跳调用：应用衰减，返回当前状态。"""
        if self.state.emotion == "neutral":
            return self.state

        elapsed = time.time() - self.state.triggered_at
        decayed = self.state.intensity - (elapsed * self.state.decay_rate)

        if decayed <= 0.05:
            self._record_history()
            self.state = EmotionState()  # 归零到 neutral
        else:
            self.state.intensity = decayed

        return self.state

    def current_intensity(self) -> float:
        """获取经衰减后的当前强度"""
        if self.state.emotion == "neutral":
            return 0.0
        elapsed = time.time() - self.state.triggered_at
        return max(0.0, self.state.intensity - elapsed * self.state.decay_rate)

    def get_state(self) -> EmotionState:
        """获取当前状态快照（含实时衰减计算）"""
        self.tick()
        return self.state

    def _record_history(self):
        if self.state.emotion != "neutral":
            self.history.append((self.state.emotion, self.state.intensity, self.state.triggered_at))
            if len(self.history) > 10:
                self.history.pop(0)
