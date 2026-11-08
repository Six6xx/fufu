/**
 * 19-lipsync.js — Live2D 口型同步驱动
 * 通过 Web Audio API 分析正在播放的 TTS 音频，
 * 将音量能量映射为 Live2D 模型的嘴巴开合度。
 */

const LipSyncDriver = (() => {
    let audioContext = null;   // AudioContext 实例（懒加载）
    let analyser = null;       // AnalyserNode
    let animFrame = null;      // requestAnimationFrame id
    let active = false;        // 是否正在同步
    let smoothValue = 0;       // 平滑后的嘴巴开合度
    let currentAudio = null;   // 当前正在跟踪的 audio 元素

    // 缓存已连接的 audio 元素，同一个元素不能重复 createMediaElementSource
    const sourceMap = new WeakMap();

    // 调参
    const FFT_SIZE = 256;
    const SMOOTHING_TIME = 0.5;
    const SMOOTH_FACTOR = 0.35;   // 越大越灵敏
    const THRESHOLD = 0.02;       // 静音阈值
    const POWER_CURVE = 0.6;      // 非线性映射曲线（<1 增强中间音量）
    const GAIN = 1.6;             // 整体增益
    const MIN_VOICE_FREQ = 100;   // 人声下限 Hz
    const MAX_VOICE_FREQ = 3000;  // 人声上限 Hz

    /**
     * 开始跟踪指定 audio 元素的口型同步
     * @param {HTMLAudioElement} audioElement
     */
    function start(audioElement) {
        if (!audioElement) return;

        // 如果已经在跟踪同一个元素，不重复启动
        if (active && currentAudio === audioElement) return;

        // 停止之前可能存在的同步
        stop();
        currentAudio = audioElement;

        // 懒初始化 AudioContext
        if (!audioContext) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            audioContext = new AudioContextClass();

            analyser = audioContext.createAnalyser();
            analyser.fftSize = FFT_SIZE;
            analyser.smoothingTimeConstant = SMOOTHING_TIME;

            // 让音频最终还能输出到扬声器
            analyser.connect(audioContext.destination);
        }

        // 浏览器自动播放策略：在用户交互后尝试 resume
        if (audioContext.state === 'suspended') {
            audioContext.resume().catch(() => {
                // resume 失败通常是因为没有用户交互，静默处理
            });
        }

        // 每个 audio 元素只能连接一次 Web Audio 图
        if (!sourceMap.has(audioElement)) {
            try {
                const source = audioContext.createMediaElementSource(audioElement);
                source.connect(analyser);
                sourceMap.set(audioElement, source);
            } catch (err) {
                console.warn('[LipSync] 连接音频源失败:', err);
                currentAudio = null;
                return;
            }
        }

        active = true;
        smoothValue = 0;
        Live2DManager.setSpeaking(true);
        update();

        // 音频自然结束或暂停时自动停止同步
        audioElement.addEventListener('ended', _onAudioStop, { once: true });
        audioElement.addEventListener('pause', _onAudioStop, { once: true });
    }

    function _onAudioStop() {
        stop();
    }

    /**
     * 每一帧从 AnalyserNode 读取频域数据并更新嘴型
     */
    function update() {
        if (!active || !analyser) return;

        const dataArray = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(dataArray);

        const sampleRate = audioContext.sampleRate || 48000;
        const binWidth = sampleRate / analyser.fftSize;
        const binCount = dataArray.length;

        // 取人声主要频段对应的 bin 范围
        const startBin = Math.max(1, Math.floor(MIN_VOICE_FREQ / binWidth));
        const endBin = Math.min(binCount, Math.ceil(MAX_VOICE_FREQ / binWidth));

        let sum = 0;
        let weightSum = 0;
        for (let i = startBin; i < endBin; i++) {
            // 低频对人声感知更重要，给予略高权重
            const weight = 1 + (endBin - i) / endBin;
            sum += dataArray[i] * weight;
            weightSum += weight;
        }

        // 归一化到 0~1
        const raw = weightSum > 0 ? sum / (weightSum * 255) : 0;

        // 非线性映射：增强中间音量，抑制底噪，限制上限
        const mapped = Math.min(1.0, Math.max(0, Math.pow(raw, POWER_CURVE) * GAIN));

        // 平滑插值，避免嘴巴抖动
        smoothValue += (mapped - smoothValue) * SMOOTH_FACTOR;

        // 低于阈值直接归零
        const finalValue = smoothValue < THRESHOLD ? 0 : smoothValue;

        Live2DManager.setMouthOpen(finalValue);

        animFrame = requestAnimationFrame(update);
    }

    /**
     * 停止口型同步，嘴巴归零
     */
    function stop() {
        active = false;
        currentAudio = null;
        if (animFrame) {
            cancelAnimationFrame(animFrame);
            animFrame = null;
        }
        smoothValue = 0;

        // 通知 Live2DManager 退出说话状态（内部会将嘴巴归零）
        Live2DManager.setSpeaking(false);
    }

    /**
     * 是否正在同步
     * @returns {boolean}
     */
    function isActive() {
        return active;
    }

    return {
        start,
        stop,
        isActive,
    };
})();
