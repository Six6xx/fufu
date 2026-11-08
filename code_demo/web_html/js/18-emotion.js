/**
 * 情绪渲染器 - 接收WebSocket emotion消息，驱动Live2D表情
 */
const EmotionRenderer = (() => {
    let currentEmotion = 'neutral';
    let targetParams = {};
    let currentParams = {};
    let animationFrame = null;
    const LERP_SPEED = 0.08; // 每帧插值系数

    function apply(emotionData) {
        // emotionData: { expression, params, transition_ms, motion }
        if (!emotionData || !emotionData.params) return;

        currentEmotion = emotionData.expression || 'default';
        targetParams = emotionData.params;

        // 如果有建议动作，播放
        if (emotionData.motion) {
            Live2DManager.playMotion(emotionData.motion, 0);
        }

        // 启动平滑过渡
        if (!animationFrame) {
            animate();
        }
    }

    function animate() {
        let allReached = true;
        const newParams = {};

        for (const [key, target] of Object.entries(targetParams)) {
            const current = currentParams[key] || 0;
            const diff = target - current;

            if (Math.abs(diff) > 0.01) {
                newParams[key] = current + diff * LERP_SPEED;
                allReached = false;
            } else {
                newParams[key] = target;
            }
        }

        currentParams = newParams;
        Live2DManager.setEmotion(currentParams);

        if (!allReached) {
            animationFrame = requestAnimationFrame(animate);
        } else {
            animationFrame = null;
        }
    }

    function reset() {
        targetParams = {};
        currentParams = {};
        if (animationFrame) {
            cancelAnimationFrame(animationFrame);
            animationFrame = null;
        }
        Live2DManager.setEmotion({});
    }

    return { apply, reset, getCurrentEmotion: () => currentEmotion };
})();
