/* 02-utils.js — WebSocket + Markdown 渲染 + UI 辅助 */

// ── WebSocket 连接 ──

function connectWS() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws`;

    updateConnectionStatus('connecting');
    state.ws = new WebSocket(url);

    state.ws.onopen = () => {
        state.isConnected = true;
        updateConnectionStatus('connected');
        setStatus('已连接');
        // 从服务器同步 TTS 状态（服务可能通过 --tts 启动参数自动开启）
        fetch('/health').then(r => r.json()).then(data => {
            if (data.tts_mode !== state.ttsEnabled) {
                state.ttsEnabled = data.tts_mode;
                updateTTSIndicator();
            }
            // 同步工作空间
            state.defaultWorkspace = data.default_workspace || '';
            state.workspace = data.workspace || state.defaultWorkspace;
            if (workspaceInput) workspaceInput.value = state.workspace;
            // 同步模型供应商
            ModelSelector.init(
                data.model || '',
                data.provider_models || [],
                data.provider_id || null,
                data.provider_name || '',
            );
            // 同步权限模式
            if (data.permission_mode && typeof PermissionUI !== 'undefined') {
                PermissionUI.updateMode(data.permission_mode);
            }
            // WS 重连版本校验：服务端重启后静态版本变化则强制刷新
            if (state._staticVersion && data.static_version !== state._staticVersion) {
                location.reload();
                return;
            }
            state._staticVersion = data.static_version;
        }).catch(() => {});
        // 请求会话列表和活跃会话
        sendWS('session_list', '');
        sendWS('session_load_active', '');
        // 同步 MCP 服务器状态
        fetchMcpStatus();
    };

    state.ws.onmessage = (event) => {
        let msg;
        try {
            msg = JSON.parse(event.data);
        } catch (e) {
            console.error('消息解析失败:', e);
            return;
        }
        try {
            handleServerMessage(msg);
        } catch (e) {
            console.error('消息处理异常:', msg && msg.type, e);
        }
    };

    state.ws.onclose = () => {
        state.isConnected = false;
        updateConnectionStatus('disconnected');
        setStatus('连接已断开，正在重连...');

        // 重置 ASR 状态：服务端 session 已在连接断开时销毁，
        // 前端必须同步停止录制，避免 MediaRecorder 持续录制、
        // audioChunks 无限增长而服务端静默丢弃
        if (state.isRecording && state.mediaRecorder) {
            state.isRecording = false;
            try {
                state.mediaRecorder.stop();
            } catch (e) { /* ignore */ }
        }
        if (state.asrEnabled) {
            state.asrEnabled = false;
            state.asrLockedText = '';
            state.asrPartialText = '';
            clearTimeout(state.asrAutoSendTimer);
            if (state.asrEditing) exitEditMode();
            micBtn.classList.remove('listening', 'active');
            asrIndicator.classList.remove('on');
            asrPopup.classList.remove('active');
            asrSendBtn.style.display = 'none';
            asrEditBtn.style.display = 'none';
            asrCancelBtn.style.display = 'none';
        }

        // 自动重连
        setTimeout(connectWS, WS_RECONNECT_DELAY);
    };

    state.ws.onerror = (error) => {
        console.error('WebSocket 错误:', error);
        state.isConnected = false;
        updateConnectionStatus('disconnected');
    };
}

function sendWS(type, content, extra = {}) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
        setStatus('未连接，请等待...');
        return false;
    }
    state.ws.send(JSON.stringify({ type, content, ...extra }));
    return true;
}

// ── HTML 转义 ──

/** 标准 HTML 转义（& < > " '），入参非字符串时做防御性转换 */
function escapeHtml(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ── 任务取消与变更需求 ──

/** 发送取消任务消息 */
function sendCancel() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: 'cancel' }));
    }
}

/** 弹出输入框让用户输入新需求，并发送变更请求 */
function promptChangeRequest() {
    const newReq = prompt('请输入新的需求:');
    if (newReq && newReq.trim()) {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send(JSON.stringify({
                type: 'change_request',
                content: newReq.trim()
            }));
        }
    }
}

// ── Markdown 渲染 ──
// 使用 marked.js 将 LLM 输出的 Markdown 转换为 HTML，
// 再用 DOMPurify 过滤 XSS 风险。流式输出时为避免频繁重排，
// 仅在内容包含 Markdown 语法时才渲染，否则使用 textContent。

function renderMarkdown(text) {
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
        // 库未加载时回退到纯文本
        return null;
    }
    try {
        const html = marked.parse(text);
        return DOMPurify.sanitize(html);
    } catch (e) {
        return null;
    }
}

/** 将文本写入消息元素：优先 Markdown 渲染，回退纯文本 */
function setMessageContent(el, text) {
    if (!SettingsManager.get('markdownEnabled')) {
        el.textContent = text;
        return;
    }
    const html = renderMarkdown(text);
    if (html !== null) {
        el.innerHTML = html;
    } else {
        el.textContent = text;
    }
}

// ── UI 辅助 ──

function setStatus(text) {
    statusBar.textContent = text;
    statusBar.classList.toggle('active', text.includes('思考') || text.includes('合成') || text.includes('识别'));
}

function updateConnectionStatus(status) {
    const dot = connectionStatus.querySelector('.status-dot');
    const text = connectionStatus.querySelector('.status-text');

    dot.className = `status-dot ${status}`;
    switch (status) {
        case 'connected':
            text.textContent = '已连接';
            break;
        case 'connecting':
            text.textContent = '连接中...';
            break;
        case 'disconnected':
            text.textContent = '未连接';
            break;
    }
}

function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    });
}

// ── 输入框自动高度 ──

function autoResizeTextarea() {
    // 先重置为 hidden（应对内容被删除后缩小的情况）
    textInput.style.overflowY = 'hidden';
    textInput.style.height = 'auto';
    const MAX_H = 160;
    if (textInput.scrollHeight > MAX_H) {
        // 内容超出上限：固定高度 + 显示滚动条
        textInput.style.height = MAX_H + 'px';
        textInput.style.overflowY = 'auto';
    } else {
        // 内容未超出：高度贴合内容，无滚动条
        textInput.style.height = textInput.scrollHeight + 'px';
    }
}
