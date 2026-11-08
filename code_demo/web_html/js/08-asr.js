/* 08-asr.js — ASR 语音识别 (MediaRecorder 录音) */

// ── 录音 (MediaRecorder) ──

async function startASRStream() {
    if (state.isRecording) return;

    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                sampleRate: 16000,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });

        state.mediaRecorder = new MediaRecorder(stream, {
            mimeType: getSupportedMimeType(),
        });

        // 累积所有音频块，定期发送完整 webm 做实时识别
        // 原理：MediaRecorder.start(timeslice) 的 ondataavailable 每次产出一个片段，
        // 累积所有片段后 new Blob() 可组装成完整 webm 文件（含 EBML 头），ffmpeg 可解码。
        state.audioChunks = [];
        state.asrLastSentTime = 0;
        const SEND_INTERVAL = 1000;  // 每 1 秒发送一次音频快照

        state.mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                state.audioChunks.push(event.data);
            }
            // 定期发送累积音频做实时识别
            const now = Date.now();
            if (now - state.asrLastSentTime >= SEND_INTERVAL && state.audioChunks.length > 0) {
                state.asrLastSentTime = now;
                const snapshot = new Blob([...state.audioChunks], {
                    type: state.mediaRecorder.mimeType || 'audio/webm'
                });
                const reader = new FileReader();
                reader.onloadend = () => {
                    const base64 = reader.result.split(',')[1];
                    console.log('[ASR] 定期发送累积音频, chunks=', state.audioChunks.length,
                                ', size=', snapshot.size, 'bytes');
                    sendWS('audio', base64);
                };
                reader.readAsDataURL(snapshot);
            }
        };

        state.mediaRecorder.onstop = () => {
            console.log('[ASR] onstop, chunks=', state.audioChunks.length);
            // 发送最终累积音频（仅在距上次发送 >2s 时，避免重复）
            const now = Date.now();
            if (state.audioChunks.length > 0 && now - state.asrLastSentTime > 2000) {
                const fullBlob = new Blob([...state.audioChunks], {
                    type: state.mediaRecorder.mimeType || 'audio/webm'
                });
                console.log('[ASR] 发送最终音频, size=', fullBlob.size, 'bytes');
                const reader = new FileReader();
                reader.onloadend = () => {
                    const base64 = reader.result.split(',')[1];
                    sendWS('audio', base64);
                };
                reader.readAsDataURL(fullBlob);
            }
            state.audioChunks = [];
            stream.getTracks().forEach(track => track.stop());
        };

        state.mediaRecorder.onerror = (e) => {
            console.error('[ASR] MediaRecorder error:', e);
        };

        // start(3000) → 每 3 秒触发一次 ondataavailable，用于实时识别
        state.mediaRecorder.start(3000);
        state.isRecording = true;
        console.log('[ASR] MediaRecorder.start(3000) 已调用, mimeType=', state.mediaRecorder.mimeType);

        // UI 更新
        micBtn.classList.add('listening');
        asrPopup.classList.add('active');
        asrLiveText.textContent = '等待语音输入...';
        asrLiveText.classList.add('listening');
        asrSendBtn.style.display = 'none';
        asrEditBtn.style.display = 'none';
        asrCancelBtn.style.display = 'none';
        setStatus('ASR 已开启，正在实时监听');

    } catch (e) {
        console.error('无法访问麦克风:', e);
        setStatus('无法访问麦克风，请检查浏览器权限');
        state.asrEnabled = false;
    }
}

function stopASRStream() {
    if (!state.isRecording || !state.mediaRecorder) return;

    state.isRecording = false;
    state.mediaRecorder.stop();

    // UI 更新
    micBtn.classList.remove('listening');
    asrPopup.classList.remove('active');
    setStatus('ASR 已关闭');
}

function getSupportedMimeType() {
    const types = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
        'audio/mp4',
    ];
    for (const type of types) {
        if (MediaRecorder.isTypeSupported(type)) return type;
    }
    return 'audio/webm';
}

// ── ASR 文本读取辅助函数 ──

/** 获取当前 ASR 弹窗中的文本（编辑模式从 textarea 读，否则从 state 读） */
function getASRDisplayText() {
    if (state.asrEditing) {
        return asrLiveText.value || '';
    }
    return (state.asrLockedText + state.asrPartialText).trim();
}
