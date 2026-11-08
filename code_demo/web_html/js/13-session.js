/* 13-session.js — 会话管理 + 侧边栏 + 会话名称编辑 */

// ── 会话管理 ──

/** 更新头部会话名称显示（截断 >10 字符） */
function updateSessionNameDisplay() {
    const name = state.sessionName || '新会话';
    const display = name.length > 10 ? name.slice(0, 10) + '...' : name;
    sessionNameEl.textContent = display;
    sessionNameEl.title = name;
}

/** 清除消息区域并恢复欢迎消息 */
function clearMessages() {
    messagesEl.classList.add('empty');
    messagesEl.innerHTML = `
        <div class="welcome-message">
            <p>你好，我是芙芙。</p>
            <p>你可以输入文字与我对话，也可以点击麦克风图标用语音交流。</p>
            <p>点击右侧的头像图标可以开启语音合成（TTS）。</p>
        </div>`;
    removeTypingIndicator();
    stopAudio();
}

/** 回放历史消息到界面 */
function replayHistory(history) {
    clearMessages();
    if (!history || history.length === 0) return;
    history.forEach(msg => {
        const role = msg.role;
        if (role === 'system' || role === 'tool') return;
        if (role === 'assistant' && msg.tool_calls) return;
        // 过滤内部消息（自主行动产生的 prompt 和结果）
        if (msg.internal) return;
        if (msg.content) {
            // 使用原始文本（未注入文件内容）显示，附件单独渲染
            const displayText = msg.original_content || msg.content;
            const attachments = msg.attachments || null;
            addMessage(role, displayText, null, attachments);
        }
    });
}

/** 渲染侧边栏会话列表 */
function renderSessionList(sessions, activeId) {
    sessionList.innerHTML = '';
    if (!sessions || sessions.length === 0) {
        sessionList.innerHTML = '<div class="session-empty">暂无会话</div>';
        return;
    }
    sessions.forEach(s => {
        const item = document.createElement('div');
        item.className = 'session-item' + (s.id === activeId ? ' active' : '');
        item.dataset.sessionId = s.id;

        const nameSpan = document.createElement('span');
        nameSpan.className = 'session-item-name';
        const displayName = s.name.length > 10 ? s.name.slice(0, 10) + '...' : s.name;
        nameSpan.textContent = displayName;
        nameSpan.title = s.name;

        const actions = document.createElement('div');
        actions.className = 'session-item-actions';

        const renameBtn = document.createElement('button');
        renameBtn.className = 'session-action-btn rename';
        renameBtn.textContent = '✎';
        renameBtn.title = '重命名';
        renameBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            startInlineRename(item, nameSpan, s);
        });

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'session-action-btn delete';
        deleteBtn.textContent = '×';
        deleteBtn.title = '删除会话';
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteSession(s.id);
        });

        actions.appendChild(renameBtn);
        actions.appendChild(deleteBtn);
        item.appendChild(nameSpan);
        item.appendChild(actions);
        item.addEventListener('click', () => switchSession(s.id));
        sessionList.appendChild(item);
    });
}

/** 切换侧边栏（折叠/展开） */
function toggleSidebar() {
    const isCollapsed = sidebar.classList.contains('collapsed');
    sidebar.classList.toggle('collapsed', !isCollapsed);
    state.sidebarOpen = isCollapsed;  // now open if was collapsed
    if (isCollapsed) {
        sendWS('session_list', '');
    }
}

/** 切换到指定会话 */
function switchSession(sessionId) {
    if (sessionId === state.sessionId) {
        toggleSidebar();
        return;
    }
    clearMessages();
    sendWS('session_load', '', { session_id: sessionId });
    toggleSidebar();
}

/** 新建会话 */
function createNewSession() {
    clearMessages();
    state.sessionId = null;
    state.sessionName = '新会话';
    state.nameCustomized = false;
    updateSessionNameDisplay();
    toggleSidebar();
}

/** 删除会话 */
function deleteSession(sessionId) {
    sendWS('session_delete', '', { session_id: sessionId });
}

// ── 侧边栏事件绑定 ──

hamburgerBtn.addEventListener('click', () => openSettings('general'));
newSessionBtn.addEventListener('click', createNewSession);

/**
 * 侧边栏内联重命名：将会话名称 span 替换为 input，
 * 回车或失焦保存，Escape 取消。
 */
function startInlineRename(item, nameSpan, session) {
    // 防止重复创建
    if (item.querySelector('.session-rename-input')) return;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-rename-input';
    input.value = session.name;
    input.maxLength = 50;

    // 隐藏名称 span，插入 input
    nameSpan.style.display = 'none';
    item.insertBefore(input, nameSpan.nextSibling);
    input.focus();
    input.select();

    let finished = false;
    function finishRename() {
        if (finished) return;
        finished = true;
        const newName = input.value.trim() || session.name;
        input.remove();
        nameSpan.style.display = '';
        if (newName !== session.name) {
            // 如果是当前会话，更新头部名称
            if (session.id === state.sessionId) {
                state.sessionName = newName;
                state.nameCustomized = true;
                updateSessionNameDisplay();
            }
            sendWS('session_rename', '', { session_id: session.id, name: newName });
        }
    }

    function cancelRename() {
        if (finished) return;
        finished = true;
        input.remove();
        nameSpan.style.display = '';
    }

    input.addEventListener('keydown', (e) => {
        if (e.isComposing) return;  // IME 组合期间不处理
        if (e.key === 'Enter') {
            e.preventDefault();
            finishRename();
        } else if (e.key === 'Escape') {
            cancelRename();
        }
    });
    input.addEventListener('blur', finishRename);
    // 阻止点击 input 冒泡到 item（触发 switchSession）
    input.addEventListener('click', (e) => e.stopPropagation());
}

// ── 会话名称编辑 ──

sessionNameEl.addEventListener('click', () => {
    if (!state.sessionId) return;
    sessionNameEl.setAttribute('contenteditable', 'true');
    sessionNameEl.textContent = state.sessionName;
    sessionNameEl.focus();
    const range = document.createRange();
    range.selectNodeContents(sessionNameEl);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
});

sessionNameEl.addEventListener('blur', () => {
    sessionNameEl.removeAttribute('contenteditable');
    const newName = sessionNameEl.textContent.trim() || '新会话';
    if (newName !== state.sessionName && state.sessionId) {
        state.sessionName = newName;
        state.nameCustomized = true;
        sendWS('session_rename', '', { name: newName });
    }
    updateSessionNameDisplay();
});

sessionNameEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
        e.preventDefault();
        sessionNameEl.blur();
    }
});
