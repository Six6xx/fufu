/* 15-events.js — 主 UI 事件绑定 + 文件/拖拽/工作空间事件 + 初始化 */

// ── 事件绑定 ──

// 发送按钮
sendBtn.addEventListener('click', sendTextMessage);

// 回车发送 (Shift+Enter 换行)
// 中文输入法组合输入（IME composition）期间不发送，需手动跟踪以兼容 Safari
let isComposing = false;
textInput.addEventListener('compositionstart', () => { isComposing = true; });
textInput.addEventListener('compositionend', () => {
    // Safari 在 compositionend 之后才触发 keydown，需要延迟重置
    setTimeout(() => { isComposing = false; }, 0);
});
textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !isComposing && !e.isComposing) {
        e.preventDefault();
        sendTextMessage();
    }
});

// 输入框自动高度
textInput.addEventListener('input', autoResizeTextarea);

// 粘贴事件 — 捕获剪贴板中的文件和图片
textInput.addEventListener('paste', (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    const files = [];
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file') {
            const file = item.getAsFile();
            if (file) files.push(file);
        }
    }
    if (files.length > 0) {
        e.preventDefault();
        FileManager.addFiles(files);
    }
    // 无文件时正常粘贴文本
});

// 麦克风按钮 — 切换实时 ASR
micBtn.addEventListener('click', toggleASR);

// 头像按钮 (TTS)
ttsBtn.addEventListener('click', toggleTTS);

// MCP 按钮
if (mcpReady) {
    mcpBtn.addEventListener('click', toggleMcpPopup);
}

// 点击其他地方关闭 MCP 弹出面板
if (mcpReady) {
    document.addEventListener('click', (e) => {
        if (state.mcpPopupOpen && !e.target.closest('#mcpPopup') && !e.target.closest('#mcpBtn')) {
            state.mcpPopupOpen = false;
            mcpPopup.classList.remove('active');
        }
    });
}

// MCP 新增模态窗口事件
if (mcpModal) {
    $('mcpModalClose').addEventListener('click', closeMcpModal);
    $('mcpModalCancel').addEventListener('click', closeMcpModal);
    $('mcpModalConfirm').addEventListener('click', submitMcpAdd);
    // 点击遮罩关闭
    mcpModal.addEventListener('click', (e) => {
        if (e.target === mcpModal) closeMcpModal();
    });
    // Tab 切换
    document.querySelectorAll('.mcp-tab').forEach(t => {
        t.addEventListener('click', () => switchMcpTab(t.dataset.tab));
    });
    // 类型按钮
    document.querySelectorAll('.mcp-type-btn').forEach(b => {
        b.addEventListener('click', () => switchMcpType(b.dataset.type));
    });
}

// ASR 弹窗关闭按钮 — 停止录音并放弃未发送的文本
asrPopupClose.addEventListener('click', () => {
    if (state.asrEnabled) {
        clearTimeout(state.asrAutoSendTimer);
        if (state.asrEditing) exitEditMode();
        stopASRStream();
        state.asrEnabled = false;
        state.asrLockedText = '';
        state.asrPartialText = '';
        asrIndicator.classList.remove('on');
        micBtn.classList.remove('active', 'listening');
        asrPopup.classList.remove('active');
        asrSendBtn.style.display = 'none';
        asrEditBtn.style.display = 'none';
        asrCancelBtn.style.display = 'none';
        setStatus('ASR 已关闭');
        sendWS('toggle_asr', '', { enabled: false });
    }
});

// ASR 弹窗发送按钮 — 发送累积文本给 Agent，但保持 ASR 开启
asrSendBtn.addEventListener('click', () => {
    const text = getASRDisplayText();
    if (!text) return;

    clearTimeout(state.asrAutoSendTimer);
    state.asrLastSentText = text;
    addMessage('user', text);
    sendWS('text', text, { thinking: state.thinkingEnabled, context_limit: _getContextLimitFromPct(SettingsManager.get('contextCompression')) });
    showTypingIndicator();

    if (state.asrEditing) exitEditMode();

    // 重置状态，继续监听
    state.asrLockedText = '';
    state.asrPartialText = '';
    asrLiveText.textContent = '等待语音输入...';
    asrLiveText.classList.add('listening');
    asrSendBtn.style.display = 'none';
    asrEditBtn.style.display = 'none';
    asrCancelBtn.style.display = 'none';
});

// ASR 编辑按钮 — 进入编辑模式，暂停自动发送倒计时
asrEditBtn.addEventListener('click', () => {
    enterEditMode();
});

// ASR 取消按钮 — 放弃识别结果，继续监听
asrCancelBtn.addEventListener('click', () => {
    cancelASRText();
});

/**
 * 进入 ASR 编辑模式。
 * 将识别文本区域替换为 textarea，暂停自动发送倒计时，
 * 用户可以修改识别结果后再手动发送。
 */
function enterEditMode() {
    if (state.asrEditing) return;
    state.asrEditing = true;

    // 暂停自动发送倒计时
    clearTimeout(state.asrAutoSendTimer);
    state.asrAutoSendTimer = null;

    // 创建 textarea 替换原始 div
    const textarea = document.createElement('textarea');
    textarea.id = 'asrLiveText';
    textarea.className = 'asr-popup-text asr-edit-textarea';
    textarea.value = state.asrLockedText + state.asrPartialText;
    textarea.rows = 3;

    // 保存原始 div 并替换
    state.asrEditBackup = asrLiveText;
    asrLiveText.parentNode.replaceChild(textarea, asrLiveText);
    asrLiveText = textarea;
    asrLiveText.focus();
    // 光标移到末尾
    asrLiveText.setSelectionRange(asrLiveText.value.length, asrLiveText.value.length);

    // 隐藏编辑按钮，保留发送和取消
    asrEditBtn.style.display = 'none';
    setStatus('编辑模式：修改文本后点击发送');
}

/**
 * 退出 ASR 编辑模式。
 * 恢复原始 div，重新启用自动发送逻辑。
 */
function exitEditMode() {
    if (!state.asrEditing) return;
    state.asrEditing = false;

    // 恢复原始 div
    if (state.asrEditBackup) {
        asrLiveText.parentNode.replaceChild(state.asrEditBackup, asrLiveText);
        asrLiveText = state.asrEditBackup;
        state.asrEditBackup = null;
    }

    // 更新显示为当前识别文本
    asrLiveText.textContent = state.asrLockedText + state.asrPartialText;

    // 重新显示编辑按钮
    if (state.asrLockedText || state.asrPartialText) {
        asrEditBtn.style.display = '';
    }
    setStatus('ASR 已开启，正在实时转换');
}

/**
 * 取消 ASR 识别结果 — 放弃当前文本，重置状态，继续监听。
 */
function cancelASRText() {
    clearTimeout(state.asrAutoSendTimer);
    state.asrAutoSendTimer = null;

    if (state.asrEditing) exitEditMode();

    state.asrLockedText = '';
    state.asrPartialText = '';
    asrLiveText.textContent = '等待语音输入...';
    asrLiveText.classList.add('listening');
    asrSendBtn.style.display = 'none';
    asrEditBtn.style.display = 'none';
    asrCancelBtn.style.display = 'none';
    setStatus('已取消，继续监听...');
}

// 思考模式开关
thinkingToggle.addEventListener('change', () => {
    state.thinkingEnabled = thinkingToggle.checked;
});

// ── 文件上传按钮事件 ──
if (uploadBtn) {
    let uploadTooltipTimer = null;
    uploadBtn.addEventListener('mouseenter', () => {
        uploadTooltipTimer = setTimeout(() => {
            if (uploadTooltip) uploadTooltip.classList.add('visible');
        }, 1500);
    });
    uploadBtn.addEventListener('mouseleave', () => {
        clearTimeout(uploadTooltipTimer);
        if (uploadTooltip) uploadTooltip.classList.remove('visible');
    });
    uploadBtn.addEventListener('click', () => {
        if (uploadTooltip) uploadTooltip.classList.remove('visible');
        if (fileInput) fileInput.click();
    });
}
if (fileInput) {
    fileInput.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
            FileManager.addFiles(e.target.files);
            fileInput.value = '';
        }
    });
}

// ── 文件预览弹窗事件 ──
if ($('filePreviewClose')) $('filePreviewClose').addEventListener('click', () => FileManager.closePreview());
if (filePreviewOverlay) {
    filePreviewOverlay.addEventListener('click', (e) => {
        if (e.target === filePreviewOverlay) FileManager.closePreview();
    });
}

// ── 拖拽上传 ──
const appEl = document.getElementById('app');
if (appEl) {
    appEl.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
    });
    appEl.addEventListener('drop', (e) => {
        e.preventDefault();
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            FileManager.addFiles(e.dataTransfer.files);
        }
    });
}

// ── 工作空间事件 ──
if (workspaceApplyBtn) {
    workspaceApplyBtn.addEventListener('click', () => {
        const path = workspaceInput ? workspaceInput.value.trim() : '';
        if (path && path !== state.workspace) {
            sendWS('workspace_set', '', { path });
        }
    });
}
if (workspaceInput) {
    workspaceInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            workspaceApplyBtn && workspaceApplyBtn.click();
        }
    });
}
if (workspaceResetBtn) {
    workspaceResetBtn.addEventListener('click', () => {
        if (state.defaultWorkspace) {
            if (workspaceInput) workspaceInput.value = state.defaultWorkspace;
            sendWS('workspace_set', '', { path: state.defaultWorkspace });
        }
    });
}
if (workspaceBrowseBtn) {
    workspaceBrowseBtn.addEventListener('click', () => {
        DirPicker.open(state.workspace, (path) => {
            if (workspaceInput) workspaceInput.value = path;
            sendWS('workspace_set', '', { path });
        });
    });
}
// 目录选择弹窗事件
if (dirPickerClose) dirPickerClose.addEventListener('click', () => DirPicker.close());
if (dirPickerCancel) dirPickerCancel.addEventListener('click', () => DirPicker.close());
if (dirPickerConfirm) dirPickerConfirm.addEventListener('click', () => DirPicker.confirm());
if (dirPickerPathInput) {
    dirPickerPathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const path = dirPickerPathInput.value.trim();
            if (path) DirPicker.navigate(path);
        }
    });
}
if (dirPickerOverlay) {
    dirPickerOverlay.addEventListener('click', (e) => {
        if (e.target === dirPickerOverlay) DirPicker.close();
    });
}

// 模型选择栏事件
if (modelBarBtn) {
    modelBarBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        ModelSelector.toggle();
    });
}
document.addEventListener('click', (e) => {
    const bar = $('modelBar');
    if (bar && !bar.contains(e.target)) {
        ModelSelector.close();
    }
});

// ── 初始化 ──
connectWS();
scrollToBottom();
