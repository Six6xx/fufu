/* 07-audio.js — 音频播放 + TTS 流式队列 */

// ── 音频播放 ──

function playAudio(base64Data, onEnded = null) {
    stopAudio();

    const audioSrc = `data:audio/wav;base64,${base64Data}`;
    state.currentAudio = new Audio(audioSrc);

    state.currentAudio.onended = () => {
        state.currentAudio = null;
        if (onEnded) onEnded();
    };

    state.currentAudio.onerror = (e) => {
        console.error('音频播放失败:', e);
        state.currentAudio = null;
        if (onEnded) onEnded();
    };

    state.currentAudio.play().catch((e) => {
        console.error('音频播放被阻止:', e);
        if (onEnded) onEnded();
    });

    // 启动 Live2D 口型同步
    if (typeof LipSyncDriver !== 'undefined') {
        LipSyncDriver.start(state.currentAudio);
    }
}

function stopAudio() {
    if (state.currentAudio) {
        state.currentAudio.pause();
        state.currentAudio.currentTime = 0;
        state.currentAudio = null;
    }
    // 清空音频队列
    state.audioQueue = [];
    state.audioQueuePlaying = false;

    // 停止 Live2D 口型同步
    if (typeof LipSyncDriver !== 'undefined') {
        LipSyncDriver.stop();
    }
}

// ── 音频队列（流式 TTS 顺序播放）──
// 后端按句子分段合成语音，通过 stream_audio 消息逐个推送。
// 前端使用队列缓存，按到达顺序依次播放，实现"边出文字边出声音"。
// 当一段音频播放结束后，自动从队列取出下一段继续播放。

state.audioQueue = [];
state.audioQueuePlaying = false;

function enqueueAudio(base64Data) {
    state.audioQueue.push(base64Data);
    if (!state.audioQueuePlaying) {
        playNextInQueue();
    }
}

function playNextInQueue() {
    if (state.audioQueue.length === 0) {
        state.audioQueuePlaying = false;
        return;
    }

    state.audioQueuePlaying = true;
    const base64Data = state.audioQueue.shift();
    const audioSrc = `data:audio/wav;base64,${base64Data}`;
    state.currentAudio = new Audio(audioSrc);

    state.currentAudio.onended = () => {
        state.currentAudio = null;
        playNextInQueue();
    };

    state.currentAudio.onerror = (e) => {
        console.error('音频播放失败:', e);
        state.currentAudio = null;
        playNextInQueue();
    };

    state.currentAudio.play().catch((e) => {
        console.error('音频播放被阻止:', e);
        state.currentAudio = null;
        playNextInQueue();
    });

    // 启动 Live2D 口型同步
    if (typeof LipSyncDriver !== 'undefined') {
        LipSyncDriver.start(state.currentAudio);
    }
}
