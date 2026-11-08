/* 11-send.js — 发送消息 + TTS/ASR 切换 */

// ── 发送文本消息 ──

async function sendTextMessage() {
    let text = textInput.value.trim();
    if (!text && !FileManager.hasFiles()) return;

    // 后台任务运行中，消息将通过伴侣通道处理
    if (state.taskRunning) {
        console.log('[send] 后台任务运行中，消息将通过伴侣通道处理');
    }

    // 长文本自动转文件
    const threshold = SettingsManager.get('longTextThreshold');
    if (text && text.length > threshold) {
        const blob = new Blob([text], { type: 'text/plain' });
        const d = new Date();
        const dateStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
        const fileName = `message_${dateStr}.txt`;
        const autoFile = new File([blob], fileName, { type: 'text/plain' });
        FileManager.addFiles([autoFile]);
        text = `[文件: ${fileName}]`;
    }

    // 上传所有待传文件
    let attachments = [];
    if (FileManager.hasFiles()) {
        attachments = await FileManager.uploadAll();
    }

    // 构建 context_limit
    const compression = SettingsManager.get('contextCompression');
    const contextLimit = _getContextLimitFromPct(compression);

    // 懒创建会话
    if (!state.sessionId) {
        const autoName = (text || '文件').slice(0, 10) + ((text || '').length > 10 ? '...' : '');
        state.sessionName = autoName;
        state.nameCustomized = false;
        updateSessionNameDisplay();
        sendWS('session_new', '', { name: autoName });
    }

    addMessage('user', text, null, attachments.length > 0 ? attachments : null);
    sendWS('text', text, {
        thinking: state.thinkingEnabled,
        attachments,
        context_limit: contextLimit,
    });
    showTypingIndicator();

    // 清空输入框
    textInput.value = '';
    textInput.style.height = 'auto';
    FileManager.clear();
}

// ── TTS / ASR 切换 ──

function toggleASR() {
    state.asrEnabled = !state.asrEnabled;

    if (state.asrEnabled) {
        // 开启 ASR
        asrIndicator.classList.add('on');
        micBtn.classList.add('active');
        state.asrLockedText = '';
        state.asrPartialText = '';
        state.asrLastSentText = '';
        state.asrLastSentTime = 0;
        clearTimeout(state.asrAutoSendTimer);
        startASRStream();
    } else {
        // 关闭 ASR — 停止录音，放弃未发送的文本（不自动发送）
        asrIndicator.classList.remove('on');
        micBtn.classList.remove('active');
        clearTimeout(state.asrAutoSendTimer);
        if (state.asrEditing) exitEditMode();
        stopASRStream();
        state.asrLockedText = '';
        state.asrPartialText = '';
        asrSendBtn.style.display = 'none';
        asrEditBtn.style.display = 'none';
        asrCancelBtn.style.display = 'none';
    }
    // 通知后端 ASR 状态
    sendWS('toggle_asr', '', { enabled: state.asrEnabled });
}

function toggleTTS() {
    state.ttsEnabled = !state.ttsEnabled;
    sendWS('toggle_tts', '', { enabled: state.ttsEnabled });
    updateTTSIndicator();
    if (!state.ttsEnabled) {
        stopAudio();
    }
}

/** 根据 state.ttsEnabled 更新 TTS 按钮的视觉状态 */
function updateTTSIndicator() {
    if (state.ttsEnabled) {
        ttsIndicator.classList.add('on');
        ttsBtn.classList.add('active');
    } else {
        ttsIndicator.classList.remove('on');
        ttsBtn.classList.remove('active');
    }
}
