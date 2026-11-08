/* 09-message-ui.js — 消息气泡 + 流式渲染 + 工具调用块 */

// ── 消息 UI ──

// ── 文件类型工具函数（消息气泡内附件卡片复用） ──
function _getFileTypeName(mimeType, name) {
    const ext = (name || '').split('.').pop().toLowerCase();
    const DOC_EXTS = ['doc', 'docx'];
    const CODE_EXTS = ['js','ts','jsx','tsx','py','html','css','json','yaml','yml','xml','sql','sh','bash','go','rs','java','c','cpp','h','rb','toml','ini','cfg'];
    const TEXT_EXTS = ['txt','md','csv','log'];
    if (DOC_EXTS.includes(ext) || (mimeType && mimeType.includes('word'))) return 'doc';
    if (ext === 'pdf' || (mimeType && mimeType.includes('pdf'))) return 'pdf';
    if ((mimeType && mimeType.startsWith('image/')) || ['png','jpg','jpeg','gif','svg','webp','bmp'].includes(ext)) return 'image';
    if (CODE_EXTS.includes(ext)) return 'code';
    if ((mimeType && mimeType.startsWith('text/')) || TEXT_EXTS.includes(ext)) return 'text';
    return 'default';
}

function _getFileIcon(typeName) {
    const icons = { doc: 'DOC', pdf: 'PDF', image: 'IMG', code: '</>', text: 'TXT', default: 'FILE' };
    return icons[typeName] || 'FILE';
}
function _getFileEmoji(typeName) {
    const emoji = { doc: '📄', pdf: '📕', image: '🖼', code: '💻', text: '📝', default: '📎' };
    return emoji[typeName] || '📎';
}

function addMessage(role, text, audioData = null, attachments = null) {
    // 移除欢迎消息
    const welcome = messagesEl.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    // 移除空状态
    messagesEl.classList.remove('empty');

    // 移除打字指示器
    const typing = messagesEl.querySelector('.typing-indicator');
    if (typing) typing.parentElement.remove();

    const wrapper = document.createElement('div');
    wrapper.className = `message ${role}`;

    // 头像
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    if (role === 'user') {
        avatar.textContent = '你';
    } else {
        avatar.innerHTML = `<svg viewBox="0 0 64 64" width="36" height="36" fill="none">
            <circle cx="32" cy="32" r="28" fill="#4A90D9"/>
            <ellipse cx="32" cy="28" rx="10" ry="11" fill="#FDE8D0"/>
            <path d="M22 24c0-6 4-12 10-12s10 6 10 12" fill="#4A6FA5"/>
            <circle cx="28" cy="27" r="1.5" fill="#2C3E50"/>
            <circle cx="36" cy="27" r="1.5" fill="#2C3E50"/>
            <path d="M30 32c1 1 3 1 4 0" stroke="#E74C3C" stroke-width="1" fill="none"/>
        </svg>`;
    }

    // 消息体容器
    const body = document.createElement('div');
    body.className = 'message-body';

    // 附件文件卡片（显示在文本上方）
    if (attachments && attachments.length > 0) {
        const attachWrap = document.createElement('div');
        attachWrap.className = 'msg-attachments';
        for (const att of attachments) {
            const typeName = _getFileTypeName(att.type, att.name);
            const card = document.createElement('div');
            card.className = 'msg-file-card';
            const icon = document.createElement('div');
            icon.className = 'msg-file-icon type-' + typeName;
            icon.textContent = _getFileEmoji(typeName);
            const label = document.createElement('span');
            label.className = 'msg-file-label';
            label.textContent = _getFileIcon(typeName);
            icon.appendChild(label);
            const nameEl = document.createElement('div');
            nameEl.className = 'msg-file-name';
            nameEl.textContent = att.name;
            nameEl.title = att.name;
            card.appendChild(icon);
            card.appendChild(nameEl);
            attachWrap.appendChild(card);
        }
        body.appendChild(attachWrap);
    }

    const content = document.createElement('div');
    content.className = 'message-content';
    if (role === 'assistant' && text) {
        setMessageContent(content, text);
    } else {
        content.textContent = text;
    }

    body.appendChild(content);

    // Footer（时间戳 + 字数统计）
    const showTimestamp = SettingsManager.get('showTimestamp');
    const showWordCount = SettingsManager.get('showWordCount');

    // TTS 播放按钮（仅 assistant 消息，且有文本）
    if (role === 'assistant' && text && state.ttsEnabled) {
        const footer = document.createElement('div');
        footer.className = 'message-footer';

        if (audioData) {
            const playBtn = createPlayButton(audioData);
            footer.appendChild(playBtn);
        }

        if (showTimestamp) {
            const time = document.createElement('span');
            time.className = 'message-time';
            time.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
            footer.appendChild(time);
        }

        if (showWordCount && text) {
            const wc = document.createElement('span');
            wc.className = 'message-word-count';
            wc.textContent = text.length + ' 字';
            footer.appendChild(wc);
        }

        body.appendChild(footer);
    } else if (showTimestamp || showWordCount) {
        const footer = document.createElement('div');
        footer.className = 'message-footer';

        if (showTimestamp) {
            const time = document.createElement('span');
            time.className = 'message-time';
            time.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
            footer.appendChild(time);
        }

        if (showWordCount && text) {
            const wc = document.createElement('span');
            wc.className = 'message-word-count';
            wc.textContent = text.length + ' 字';
            footer.appendChild(wc);
        }

        body.appendChild(footer);
    }

    wrapper.appendChild(avatar);
    wrapper.appendChild(body);
    messagesEl.appendChild(wrapper);

    // 滚动到底部
    scrollToBottom();
}

// ── 流式消息辅助函数 ──
// 流式输出的核心机制：
//   1. 收到第一个 stream_content/stream_reasoning 时，创建空的消息气泡
//   2. 后续 token 通过 textContent 追加到消息中（避免 innerHTML 的 XSS 风险）
//   3. stream_end 时添加 footer（时间戳、播放按钮等）

/** 移除打字指示器（三个跳动的点） */
function removeTypingIndicator() {
    const typing = messagesEl.querySelector('.typing-indicator');
    if (typing) typing.parentElement.remove();
}

/**
 * 创建流式 assistant 消息气泡（空内容）。
 * 在收到第一个 stream_content token 时调用，
 * 后续 token 通过 .message-content.textContent 追加。
 */
function createStreamingMessage() {
    // 移除欢迎消息
    const welcome = messagesEl.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const wrapper = document.createElement('div');
    wrapper.className = 'message assistant';

    // 头像
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.innerHTML = `<svg viewBox="0 0 64 64" width="36" height="36" fill="none">
        <circle cx="32" cy="32" r="28" fill="#4A90D9"/>
        <ellipse cx="32" cy="28" rx="10" ry="11" fill="#FDE8D0"/>
        <path d="M22 24c0-6 4-12 10-12s10 6 10 12" fill="#4A6FA5"/>
        <circle cx="28" cy="27" r="1.5" fill="#2C3E50"/>
        <circle cx="36" cy="27" r="1.5" fill="#2C3E50"/>
        <path d="M30 32c1 1 3 1 4 0" stroke="#E74C3C" stroke-width="1" fill="none"/>
    </svg>`;

    // 消息体容器
    const messageBody = document.createElement('div');
    messageBody.className = 'message-body';

    const content = document.createElement('div');
    content.className = 'message-content';
    content.textContent = '';

    messageBody.appendChild(content);
    wrapper.appendChild(avatar);
    wrapper.appendChild(messageBody);
    messagesEl.appendChild(wrapper);
    scrollToBottom();

    return wrapper;
}

/**
 * 在消息气泡中创建可折叠的"思考过程"块。
 * 插入位置：.message-content 之前（即思考内容显示在正文上方）。
 * 点击 header 可展开/折叠，流结束后自动折叠。
 */
function createThinkingBlock(msgEl) {
    const body = msgEl.querySelector('.message-content').parentElement;
    const content = msgEl.querySelector('.message-content');

    const block = document.createElement('div');
    block.className = 'thinking-block';
    block.innerHTML = `
        <div class="thinking-header">
            <span class="arrow">▼</span>
            <span>💭 思考过程</span>
        </div>
        <div class="thinking-body"></div>
    `;
    block.querySelector('.thinking-header').addEventListener('click', () => {
        block.classList.toggle('collapsed');
    });

    body.insertBefore(block, content);
    return block;
}

/** 为流式消息添加底部信息栏（时间戳），在 stream_end 时调用 */
function addMessageFooter(msgEl, text) {
    const body = msgEl.querySelector('.message-body')
              || msgEl.querySelector('.message-content').parentElement;
    // 避免重复添加
    if (body.querySelector('.message-footer')) return;

    const showTimestamp = SettingsManager.get('showTimestamp');
    const showWordCount = SettingsManager.get('showWordCount');
    if (!showTimestamp && !showWordCount) return;

    const footer = document.createElement('div');
    footer.className = 'message-footer';

    if (showTimestamp) {
        const time = document.createElement('span');
        time.className = 'message-time';
        time.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        footer.appendChild(time);
    }

    if (showWordCount && text) {
        const wc = document.createElement('span');
        wc.className = 'message-word-count';
        wc.textContent = text.length + ' 字';
        footer.appendChild(wc);
    }

    body.appendChild(footer);
}

/**
 * 获取或创建工具块容器。
 * 插入到 .message-content 之后，作为工具调用/结果的存放位置。
 */
function getOrCreateToolContainer(msgEl) {
    let container = msgEl.querySelector('.tool-blocks-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'tool-blocks-container';
        const body = msgEl.querySelector('.message-body')
                  || msgEl.querySelector('.message-content').parentElement;
        body.appendChild(container);
    }
    return container;
}

/**
 * 创建一个工具调用可折叠块。
 * 默认折叠状态，点击 header 展开查看结果。
 */
function createToolBlock(msgEl, name, args) {
    const container = getOrCreateToolContainer(msgEl);
    const block = document.createElement('div');
    block.className = 'tool-block collapsed';
    block.dataset.toolArgs = JSON.stringify(args);

    const header = document.createElement('div');
    header.className = 'tool-header';

    const arrow = document.createElement('span');
    arrow.className = 'arrow';
    arrow.textContent = '▶';

    const label = document.createElement('span');
    label.textContent = `🔧 ${name}`;

    const status = document.createElement('span');
    status.className = 'tool-status';
    status.textContent = '调用中...';

    header.appendChild(arrow);
    header.appendChild(label);
    header.appendChild(status);

    const body = document.createElement('div');
    body.className = 'tool-body';

    block.appendChild(header);
    block.appendChild(body);
    container.appendChild(block);

    header.addEventListener('click', () => block.classList.toggle('collapsed'));
    scrollToBottom();
    return block;
}

/**
 * 向已存在的工具块追加新的调用参数。
 * 用于合并连续相同的工具调用。
 */
function appendToToolBlock(block, args) {
    const header = block.querySelector('.tool-header');
    const label = header.querySelector('span:nth-child(2)');

    // 更新标签，显示调用次数
    const currentText = label.textContent;
    if (currentText.includes('(×')) {
        // 已有计数，增加
        const match = currentText.match(/\(×(\d+)\)/);
        if (match) {
            const count = parseInt(match[1]) + 1;
            label.textContent = currentText.replace(/\(×\d+\)/, `(×${count})`);
        }
    } else {
        // 第一次合并，添加计数
        label.textContent = currentText + ' (×2)';
    }

    // 保存新参数供后续 result 追加
    const prevArgs = block.dataset.toolArgs || '';
    block.dataset.toolArgs = prevArgs + '\n---\n' + JSON.stringify(args);

    scrollToBottom();
}

/**
 * 工具结果到达时，更新工具块：header 补充参数摘要、移除"调用中..."，body 填入结果。
 */
function updateToolBlock(block, result) {
    const header = block.querySelector('.tool-header');
    const label = header.querySelector('span:nth-child(2)');
    const status = header.querySelector('.tool-status');
    const body = block.querySelector('.tool-body');

    // 在 label 末尾追加参数摘要
    const argsStr = block.dataset.toolArgs || '';
    const firstArgs = argsStr.split('\n---\n')[0];
    const short = firstArgs.length > 50 ? firstArgs.slice(0, 50) + '...' : firstArgs;
    label.textContent = label.textContent + ': ' + short;

    // 移除"调用中..."状态
    if (status) status.remove();

    // 填入结果（如有合并调用，先显示参数再显示结果）
    if (argsStr.includes('\n---\n')) {
        body.textContent = argsStr + '\n\n' + result;
    } else {
        body.textContent = result;
    }
}

function createPlayButton(audioBase64) {
    const btn = document.createElement('button');
    btn.className = 'play-btn';
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
    btn.title = '播放语音';

    btn.addEventListener('click', () => {
        if (btn.classList.contains('playing')) {
            stopAudio();
            btn.classList.remove('playing');
            btn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
        } else {
            playAudio(audioBase64, () => {
                btn.classList.remove('playing');
                btn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
            });
            btn.classList.add('playing');
        }
    });

    return btn;
}

function showTypingIndicator() {
    const wrapper = document.createElement('div');
    wrapper.className = 'message assistant';

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.innerHTML = `<svg viewBox="0 0 64 64" width="36" height="36" fill="none">
        <circle cx="32" cy="32" r="28" fill="#4A90D9"/>
        <ellipse cx="32" cy="28" rx="10" ry="11" fill="#FDE8D0"/>
        <path d="M22 24c0-6 4-12 10-12s10 6 10 12" fill="#4A6FA5"/>
    </svg>`;

    const content = document.createElement('div');
    content.className = 'message-content typing-indicator';
    content.innerHTML = '<span></span><span></span><span></span>';

    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    messagesEl.appendChild(wrapper);
    scrollToBottom();
}
