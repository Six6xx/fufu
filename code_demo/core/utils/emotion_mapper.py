class EmotionMapper:
    """将情绪字符串映射为 Live2D 模型参数"""

    EMOTION_MAP = {
        "happy":        {"expression": "smile",    "params": {"ParamEyeLOpen": 0.7, "ParamEyeROpen": 0.7, "ParamMouthOpenY": 0.3}},
        "thinking":     {"expression": "thinking", "params": {"ParamEyeLOpen": 0.5, "ParamEyeROpen": 0.5}},
        "focused":      {"expression": "serious",  "params": {"ParamEyeLOpen": 0.9, "ParamEyeROpen": 0.9}},
        "pleased":      {"expression": "proud",    "params": {"ParamEyeLOpen": 0.6, "ParamEyeROpen": 0.6, "ParamMouthOpenY": 0.2}},
        "sad":          {"expression": "sad",      "params": {"ParamEyeLOpen": 0.4, "ParamEyeROpen": 0.4}},
        "surprised":    {"expression": "surprise", "params": {"ParamEyeLOpen": 1.0, "ParamEyeROpen": 1.0, "ParamMouthOpenY": 0.6}},
        "neutral":      {"expression": "default",  "params": {}},
        # Phase 5.3 新增
        "shocked":      {"expression": "surprise", "params": {"ParamEyeLOpen": 1.0, "ParamEyeROpen": 1.0, "ParamMouthOpenY": 0.8}},
        "hurt":         {"expression": "sad",      "params": {"ParamEyeLOpen": 0.3, "ParamEyeROpen": 0.3, "ParamBrowLY": -0.3}},
        "angry":        {"expression": "angry",    "params": {"ParamEyeLOpen": 0.8, "ParamEyeROpen": 0.8, "ParamBrowLY": -0.5}},
        "curious":      {"expression": "thinking", "params": {"ParamEyeLOpen": 0.8, "ParamEyeROpen": 0.6}},
        "embarrassed":  {"expression": "shy",      "params": {"ParamEyeLOpen": 0.4, "ParamEyeROpen": 0.4, "ParamMouthOpenY": 0.1}},
        "excited":      {"expression": "smile",    "params": {"ParamEyeLOpen": 0.9, "ParamEyeROpen": 0.9, "ParamMouthOpenY": 0.5}},
    }

    def map(self, emotion: str, intensity: float = 1.0) -> dict:
        """
        返回格式:
        {
            "expression": str,
            "params": {paramId: value, ...},
            "transition_ms": int,
            "motion": str | None  # 可选触发的动作
        }
        """
        entry = self.EMOTION_MAP.get(emotion, self.EMOTION_MAP["neutral"])
        return {
            "expression": entry["expression"],
            "params": {k: v * intensity for k, v in entry["params"].items()},
            "transition_ms": 300,
            "motion": self._suggest_motion(emotion),
        }

    def _suggest_motion(self, emotion: str) -> str | None:
        """某些情绪可触发特定动作"""
        motion_triggers = {
            "happy": "Tap",
            "surprised": "Flick",
            "shocked": "Flick",
            "excited": "Tap",
        }
        return motion_triggers.get(emotion)
