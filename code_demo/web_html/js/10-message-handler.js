/* 10-message-handler.js — 服务器消息分发 (handleServerMessage) */

// ── 服务器消息处理 ──
// 根据消息类型分发处理逻辑：
//   stream_content  → 流式正文 token，逐字追加到消息气泡
//   stream_reasoning → 流式思考 token，追加到可折叠的思考块
//   stream_end      → 文本流结束，折叠思考块，添加时间戳 footer
//   stream_audio    → 流式 TTS 音频片段，加入播放队列
//   audio           → 非流式完整音频（备用）
//   asr_partial     → ASR 中间识别结果（可能变化）
//   asr_final       → ASR 最终句子（已确认）
//   asr_auto_send   → 静默后自动发送文本给 Agent
//   status / error  → 状态栏显示

function handleServerMessage(msg) {
    const { type, content } = msg;

    switch (type) {
        case 'text':
            // 非流式完整文本（备用）
            addMessage('assistant', content, msg.audio);
            break;

        case 'stream_content':
            // 流式正文 token
            if (!state.streamActive) {
                removeTypingIndicator();
                state.streamEl = createStreamingMessage();
                state.streamContent = '';
                state.streamActive = true;
            }
            state.streamContent += content;
            setMessageContent(state.streamEl.querySelector('.message-content'), state.streamContent);
            scrollToBottom();
            break;

        case 'stream_reasoning':
            // 流式思考 token
            if (!state.streamActive) {
                removeTypingIndicator();
                state.streamEl = createStreamingMessage();
                state.streamContent = '';
                state.streamActive = true;
            }
            if (!state.thinkingEl) {
                state.thinkingEl = createThinkingBlock(state.streamEl);
                state.thinkingContent = '';
            }
            state.thinkingContent += content;
            state.thinkingEl.querySelector('.thinking-body').textContent = state.thinkingContent;
            scrollToBottom();
            break;

        case 'stream_tool_call':
            // 工具调用开始 — 创建或合并可折叠块
            if (!state.streamActive) {
                removeTypingIndicator();
                state.streamEl = createStreamingMessage();
                state.streamContent = '';
                state.streamActive = true;
            }
            // 如果与上一个工具相同，合并到现有块
            if (state.lastToolName === msg.tool_name && state.currentToolBlock) {
                appendToToolBlock(state.currentToolBlock, msg.tool_args);
            } else {
                state.currentToolBlock = createToolBlock(state.streamEl, msg.tool_name, msg.tool_args);
                state.lastToolName = msg.tool_name;
            }
            break;

        case 'stream_tool_result':
            // 工具执行结果 — 更新工具块
            if (state.currentToolBlock) {
                updateToolBlock(state.currentToolBlock, msg.tool_result);
            }
            break;

        case 'stream_end':
            // 流式传输结束
            // 确保 typing indicator 被移除（即使没有任何内容被流式推送）
            removeTypingIndicator();
            if (state.streamEl) {
                // 折叠思考块
                const block = state.streamEl.querySelector('.thinking-block');
                if (block) block.classList.add('collapsed');
                // 折叠所有工具块
                state.streamEl.querySelectorAll('.tool-block').forEach(b => b.classList.add('collapsed'));
                // 添加 footer（时间戳）
                addMessageFooter(state.streamEl, state.streamContent);
            }
            state.streamActive = false;
            state.streamEl = null;
            state.streamContent = '';
            state.thinkingEl = null;
            state.thinkingContent = '';
            state.currentToolBlock = null;
            state.lastToolName = null;
            break;

        case 'audio':
            // base64 编码的 WAV 音频（非流式）
            playAudio(content);
            break;

        case 'stream_audio':
            // 流式 TTS 音频片段 — 排队顺序播放
            enqueueAudio(content);
            break;

        case 'asr_partial':
            // ASR 中间识别结果（可能变化）
            // 编辑模式下不更新显示，避免干扰用户正在编辑的内容
            if (state.asrEditing) break;
            if (content) {
                state.asrPartialText = content;
                asrLiveText.textContent = state.asrLockedText + state.asrPartialText;
                asrLiveText.classList.remove('listening');
            }
            break;

        case 'asr_final':
            // ASR 最终句子（已确认，不再变化）
            if (content && content.trim()) {
                state.asrLockedText += content;
                state.asrPartialText = '';

                if (state.asrEditing) {
                    // 编辑模式：不更新显示文本，不启动自动发送
                    // 新识别的句子累积到 asrLockedText，等用户退出编辑后一起处理
                } else {
                    asrLiveText.textContent = state.asrLockedText;
                    // 显示操作按钮
                    asrSendBtn.style.display = '';
                    asrEditBtn.style.display = '';
                    asrCancelBtn.style.display = '';

                    // 启动 2 秒自动发送定时器
                    clearTimeout(state.asrAutoSendTimer);
                    state.asrAutoSendTimer = setTimeout(() => {
                        const text = state.asrLockedText.trim();
                        if (text && text !== state.asrLastSentText) {
                            // 如果在倒计时期间进入了编辑模式，发送编辑后的文本
                            const sendText = state.asrEditing
                                ? (getASRDisplayText() || text)
                                : text;
                            state.asrLastSentText = sendText;
                            addMessage('user', sendText);
                            sendWS('text', sendText, { thinking: state.thinkingEnabled, context_limit: _getContextLimitFromPct(SettingsManager.get('contextCompression')) });
                            showTypingIndicator();
                            if (state.asrEditing) exitEditMode();
                        }
                        // 重置弹窗，继续监听
                        state.asrLockedText = '';
                        state.asrPartialText = '';
                        asrLiveText.textContent = '等待语音输入...';
                        asrLiveText.classList.add('listening');
                        asrSendBtn.style.display = 'none';
                        asrEditBtn.style.display = 'none';
                        asrCancelBtn.style.display = 'none';
                    }, 2000);
                }
            }
            break;

        case 'asr_auto_send':
            // 服务端备用自动发送（前端定时器未触发时的兜底）
            if (content && content.trim()) {
                clearTimeout(state.asrAutoSendTimer);
                const text = content.trim();
                if (text !== state.asrLastSentText) {
                    const sendText = state.asrEditing
                        ? (getASRDisplayText() || text)
                        : text;
                    state.asrLastSentText = sendText;
                    addMessage('user', sendText);
                    sendWS('text', sendText, { thinking: state.thinkingEnabled, context_limit: _getContextLimitFromPct(SettingsManager.get('contextCompression')) });
                    showTypingIndicator();
                    if (state.asrEditing) exitEditMode();
                }
                state.asrLockedText = '';
                state.asrPartialText = '';
                asrLiveText.textContent = '等待语音输入...';
                asrLiveText.classList.add('listening');
                asrSendBtn.style.display = 'none';
                asrEditBtn.style.display = 'none';
                asrCancelBtn.style.display = 'none';
            }
            break;

        case 'status':
            setStatus(content);
            break;

        case 'error':
            setStatus(`错误: ${content}`);
            addMessage('assistant', `⚠ ${content}`);
            break;

        // ── 会话管理消息 ──

        case 'session_list':
            state.sessions = msg.sessions || [];
            renderSessionList(state.sessions, msg.active_session_id);
            break;

        case 'session_loaded':
            state.sessionId = msg.session_id;
            state.sessionName = msg.name || '新会话';
            state.nameCustomized = true;
            updateSessionNameDisplay();
            replayHistory(msg.history || []);
            // 恢复会话工作空间
            if (msg.workspace) {
                state.workspace = msg.workspace;
                if (workspaceInput) workspaceInput.value = msg.workspace;
            }
            // 恢复会话模型
            ModelSelector.update(msg.model, msg.provider_models, msg.provider_id, msg.provider_name);
            // 更新侧边栏活跃状态
            renderSessionList(state.sessions, msg.session_id);
            break;

        case 'session_created':
            state.sessionId = msg.session_id;
            state.sessionName = msg.name || '新会话';
            updateSessionNameDisplay();
            // 恢复工作空间
            if (msg.workspace) {
                state.workspace = msg.workspace;
                if (workspaceInput) workspaceInput.value = msg.workspace;
            }
            // 恢复模型
            ModelSelector.update(msg.model, msg.provider_models, msg.provider_id, msg.provider_name);
            // 刷新侧边栏列表
            sendWS('session_list', '');
            break;

        case 'session_renamed':
            state.sessionName = msg.name || '新会话';
            state.nameCustomized = true;
            updateSessionNameDisplay();
            sendWS('session_list', '');
            break;

        case 'session_deleted':
            if (msg.session_id === state.sessionId) {
                state.sessionId = null;
                state.sessionName = '新会话';
                state.nameCustomized = false;
                updateSessionNameDisplay();
                clearMessages();
            }
            sendWS('session_list', '');
            break;

        case 'workspace_updated':
            if (msg.success) {
                state.workspace = msg.workspace;
                if (workspaceInput) workspaceInput.value = msg.workspace;
            } else {
                if (workspaceInput) workspaceInput.value = state.workspace;
            }
            if (msg.message) {
                setStatus(msg.message);
            }
            break;

        case 'model_updated':
            if (msg.success) {
                ModelSelector.update(
                    msg.model,
                    msg.models,
                    msg.provider_id,
                    msg.provider_name,
                );
            }
            if (msg.message) {
                setStatus(msg.message);
            }
            break;

        case 'permission_request': {
            // 后端 _send_json 将数据包装在 content 字段中；
            // 兼容 content 为 JSON 字符串或缺失（回退 msg 本身）的情况
            let permData = msg.content;
            if (typeof permData === 'string') {
                try { permData = JSON.parse(permData); } catch (e) { permData = null; }
            }
            if (!permData || typeof permData !== 'object' || !permData.request_id) {
                permData = msg;
            }
            PermissionUI.show(permData);
            break;
        }

        case 'permission_mode_updated':
            if (msg.ok !== false) {
                PermissionUI.updateMode(msg.mode);
            }
            if (msg.message) {
                setStatus(msg.message);
            }
            break;

        // ── 技能相关消息 ──

        case 'skill_created':
            setStatus('✨ 新技能已创建：' + (msg.data && msg.data.name ? msg.data.name : ''));
            refreshSkillList();
            break;

        case 'merge_suggestion':
            setStatus('💡 发现可合并技能：' + (msg.data && msg.data.skill_a ? msg.data.skill_a : '') + ' ↔ ' + (msg.data && msg.data.skill_b ? msg.data.skill_b : ''));
            break;

        case 'skill_merged':
            setStatus('🔄 技能已合并：' + (msg.data && msg.data.new_skill ? msg.data.new_skill : ''));
            refreshSkillList();
            break;

        case 'skill_restored':
            setStatus('↩️ 技能已恢复：' + (msg.data && msg.data.name ? msg.data.name : ''));
            refreshSkillList();
            break;

        case 'skills_restored':
            const restoredList = msg.data && msg.data.restored ? msg.data.restored : [];
            setStatus(`↩️ 已回滚恢复 ${restoredList.length} 个技能`);
            refreshSkillList();
            break;

        // ── 任务进度消息 ──

        case 'task_started':
            // 显示进度条区域
            showTaskProgressBar(msg.description || '执行中...');
            state.taskRunning = true;
            break;

        case 'task_progress':
            // 更新进度条
            updateTaskProgress(msg.step, msg.tool, msg.status, msg.preview);
            break;

        case 'task_completed':
            // 隐藏进度条
            hideTaskProgressBar();
            state.taskRunning = false;
            break;

        case 'task_cancelled':
            // 隐藏进度条 + 通知
            hideTaskProgressBar();
            state.taskRunning = false;
            addMessage('assistant', msg.reason === 'requirement_change'
                ? '任务已中断，正在处理新需求...'
                : '任务已取消');
            break;

        // ── 伴侣对话消息 ──

        case 'companion_chat':
            // 伴侣AI在任务执行期间的独立回复（完整消息）
            addMessage('assistant', msg.content);
            if (msg.change_detected) {
                showChangeConfirmHint();
            }
            break;

        case 'companion_token':
            // 伴侣对话的流式 token
            if (!state.companionStreamActive) {
                removeTypingIndicator();
                state.companionStreamEl = createStreamingMessage();
                state.companionStreamContent = '';
                state.companionStreamActive = true;
            }
            state.companionStreamContent += msg.content;
            setMessageContent(state.companionStreamEl.querySelector('.message-content'), state.companionStreamContent);
            scrollToBottom();
            break;

        case 'companion_end':
            // 伴侣流式传输结束
            if (state.companionStreamEl) {
                addMessageFooter(state.companionStreamEl, state.companionStreamContent);
            }
            state.companionStreamActive = false;
            state.companionStreamEl = null;
             state.companionStreamContent = '';
             break;

        case 'change_hint':
            // 显示变更需求提示
            showChangeConfirmHint();
            break;

        case 'cancel_ack':
            // 取消确认
            setStatus('正在取消任务...');
            break;

        case 'companion_segment':
            // 伴侣分段推送 — 多段连续消息聚合到同一气泡
            showCompanionSegment(msg.text, msg.emotion, msg.remaining, msg.source);
            break;

        case 'companion_murmur':
            // 伴侣碎碎念 — 轻量浮动提示，不混入主对话流
            // source: 'progress'（任务进度） / 'idle'（idle LLM 生成），兼容旧消息默认 progress
            showCompanionMurmur(msg.text || content, msg.source || 'progress');
            break;

        case 'cognitive_thought':
            // 认知思维气泡 — 三种模式视觉区分
            showCognitiveThought(msg.text, msg.mode);
            // TTS 集成：当后端标记需要语音合成时，通过 WebSocket 请求 TTS
            if (msg.tts) {
                sendWS('tts_speak', msg.text);
            }
            break;

        case 'cognitive_nudge':
            // AI 主动请求外显的轻提示
            showCognitiveNudge(msg.hint);
            break;

        case 'emotion':
            // 情绪推送 — 驱动 Live2D 表情变化
            if (typeof EmotionRenderer !== 'undefined') {
                EmotionRenderer.apply(msg.data);
            }
            break;

    default:
            console.log('未知消息类型:', type, content);
    }
}

// ── 任务进度条 UI ──

function showTaskProgressBar(description) {
    let bar = document.getElementById('task-progress-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'task-progress-bar';
        bar.className = 'task-progress-bar';
        bar.innerHTML = `
            <div class="task-progress-info">
                <span class="task-progress-desc">${description}</span>
                <span class="task-progress-step">准备中...</span>
            </div>
            <div class="task-progress-track">
                <div class="task-progress-fill"></div>
            </div>
            <div class="task-progress-actions">
                <button class="task-cancel-btn" onclick="sendCancel()">取消任务</button>
                <button class="task-change-btn" onclick="promptChangeRequest()">变更需求</button>
            </div>
        `;
        // 插入到输入区上方（workspace-bar 与 input-area 之间）
        const inputArea = document.querySelector('.input-area');
        if (inputArea) {
            inputArea.parentNode.insertBefore(bar, inputArea);
        } else {
            document.body.appendChild(bar);
        }
    }
    // 更新描述文本
    const descEl = bar.querySelector('.task-progress-desc');
    if (descEl) descEl.textContent = description;
    bar.style.display = 'flex';
}

function updateTaskProgress(step, tool, status, preview) {
    const bar = document.getElementById('task-progress-bar');
    if (!bar) return;
    const stepEl = bar.querySelector('.task-progress-step');
    const fillEl = bar.querySelector('.task-progress-fill');
    if (stepEl) {
        const toolLabel = tool || '—';
        const statusLabel = status === 'done' ? '完成' : '执行中';
        stepEl.textContent = `步骤 ${step}: ${toolLabel} (${statusLabel})`;
    }
    if (fillEl) {
        // 简单的进度动画（不知道总步数时用渐进填充）
        const currentWidth = parseFloat(fillEl.style.width) || 5;
        if (status === 'done') {
            fillEl.style.width = Math.min(step * 15, 90) + '%';
        } else {
            fillEl.style.width = Math.max(currentWidth, 5) + '%';
        }
    }
    // 如果有 preview 信息，在进度条下方显示
    if (preview) {
        let previewEl = bar.querySelector('.task-progress-preview');
        if (!previewEl) {
            previewEl = document.createElement('div');
            previewEl.className = 'task-progress-preview';
            bar.insertBefore(previewEl, bar.querySelector('.task-progress-track'));
        }
        previewEl.textContent = preview;
    }
}

function hideTaskProgressBar() {
    const bar = document.getElementById('task-progress-bar');
    if (bar) {
        bar.style.display = 'none';
        // 重置进度条内部状态
        const fillEl = bar.querySelector('.task-progress-fill');
        const stepEl = bar.querySelector('.task-progress-step');
        const previewEl = bar.querySelector('.task-progress-preview');
        if (fillEl) fillEl.style.width = '5%';
        if (stepEl) stepEl.textContent = '准备中...';
        if (previewEl) previewEl.remove();
    }
}

function showChangeConfirmHint() {
    // 高亮变更按钮
    const btn = document.querySelector('.task-change-btn');
    if (btn) {
        btn.classList.add('highlight');
        setTimeout(() => btn.classList.remove('highlight'), 3000);
    }
}

// ── 伴侣分段推送 ──

function showCompanionSegment(text, emotion, remaining, source) {
    // 查找或创建分段消息容器
    let container = document.getElementById('companion-segment-container');

    if (!container) {
        // 首次：创建新的消息气泡容器
        const avatar = (source === 'review') ? '💭' : '🎭';
        container = document.createElement('div');
        container.id = 'companion-segment-container';
        container.className = 'message assistant-message';
        container.innerHTML = `
            <div class="message-avatar">${avatar}</div>
            <div class="message-body">
                <div class="companion-segments"></div>
            </div>
        `;
        const messagesEl = document.getElementById('messages');
        if (messagesEl) {
            messagesEl.appendChild(container);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
    }

    // 追加新段落
    const segmentsEl = container.querySelector('.companion-segments');
    const segmentEl = document.createElement('div');
    segmentEl.className = 'companion-segment';
    if (source === 'resume') {
        segmentEl.classList.add('segment-resume');
    }
    if (source === 'review') {
        segmentEl.classList.add('segment-review');
    }
    segmentEl.textContent = text;
    segmentsEl.appendChild(segmentEl);

    // 滚动到底部
    const messagesEl = document.getElementById('messages');
    if (messagesEl) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // 如果没有剩余段了，3秒后移除 container ID（允许下次创建新容器）
    if (remaining === 0) {
        setTimeout(() => {
            if (container) {
                container.removeAttribute('id');
            }
        }, 3000);
    }

    // 触发情绪事件（如果有 emotion 字段）
    if (emotion && window.dispatchEvent) {
        window.dispatchEvent(new CustomEvent('companion-emotion', {
            detail: { emotion: emotion, intensity: 0.7 }
        }));
    }
}

// ── 认知思维气泡 ──

let _cognitiveThoughtTimer = null;

function positionCognitiveThoughtBubble() {
    const bubble = document.querySelector('#cognitive-thought-container .cognitive-thought');
    if (!bubble) return;
    const bounds = typeof Live2DManager !== 'undefined' ? Live2DManager.getBounds?.() : null;
    if (!bounds) {
        bubble.style.left = '16px';
        bubble.style.top = '16px';
        return;
    }

    const margin = 12;
    const gap = 14;
    const width = bubble.offsetWidth;
    const height = bubble.offsetHeight;
    const rightSpace = window.innerWidth - (bounds.x + bounds.width);
    const leftSpace = bounds.x;
    let left;
    let top;

    // 优先放在模型旁边，先选空间更充足的一侧，避免遮住人物。
    if (leftSpace >= width + gap + margin || leftSpace >= rightSpace) {
        left = bounds.x - width - gap;
    } else {
        left = bounds.x + bounds.width + gap;
    }
    if (left < margin || left + width > window.innerWidth - margin) {
        left = Math.max(margin, Math.min(window.innerWidth - width - margin, bounds.x + bounds.width / 2 - width / 2));
        top = bounds.y - height - gap;
        if (top < margin) top = bounds.y + bounds.height + gap;
    } else {
        top = bounds.y + Math.min(28, Math.max(8, bounds.height * 0.08));
    }
    top = Math.max(margin, Math.min(window.innerHeight - height - margin, top));
    bubble.style.left = `${Math.round(left)}px`;
    bubble.style.top = `${Math.round(top)}px`;
}

window.addEventListener('live2d:layout', positionCognitiveThoughtBubble);
window.addEventListener('resize', positionCognitiveThoughtBubble);

function showCognitiveThought(text, mode) {
    if (!text) return;
    const modeConfig = {
        deepen: 'thought-deepen',
        diverge: 'thought-diverge',
        idle: 'thought-idle',
        act: 'thought-act',
    };
    const safeText = String(text).trim();
    const displayText = safeText.length > 220 ? `${safeText.slice(0, 219)}…` : safeText;

    let container = document.getElementById('cognitive-thought-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'cognitive-thought-container';
        container.className = 'cognitive-thought-container';
        document.body.appendChild(container);
    }

    let bubble = container.querySelector('.cognitive-thought');
    if (!bubble) {
        bubble = document.createElement('div');
        bubble.className = 'cognitive-thought';
        const icon = document.createElement('span');
        icon.className = 'thought-icon';
        icon.textContent = '💭';
        const body = document.createElement('span');
        body.className = 'thought-text';
        bubble.append(icon, body);
        container.appendChild(bubble);
    }
    bubble.className = `cognitive-thought ${modeConfig[mode] || modeConfig.idle}`;
    bubble.querySelector('.thought-text').textContent = displayText;
    bubble.style.opacity = '0';
    positionCognitiveThoughtBubble();
    requestAnimationFrame(() => {
        positionCognitiveThoughtBubble();
        bubble.style.opacity = '1';
    });

    if (_cognitiveThoughtTimer) clearTimeout(_cognitiveThoughtTimer);
    _cognitiveThoughtTimer = setTimeout(() => {
        bubble.style.opacity = '0';
        setTimeout(() => {
            if (bubble.isConnected && bubble.style.opacity === '0') bubble.remove();
        }, 450);
    }, 12000);
}

// ── 伴侣碎碎念浮动提示 ──

let _murmurTimer = null;

function showCompanionMurmur(text, source) {
    if (!text) return;
    source = source || 'progress';

    let el = document.getElementById('companion-murmur');
    if (!el) {
        el = document.createElement('div');
        el.id = 'companion-murmur';
        el.className = 'companion-murmur';
        // 插入到消息列表内部顶部，使用 sticky 定位
        const chatMessages = document.getElementById('messages');
        if (chatMessages) {
            chatMessages.prepend(el);
        } else {
            document.body.appendChild(el);
        }
    }

    // 后一条替换前一条
    el.textContent = text;
    el.classList.remove('fade-out');
    el.classList.add('visible');

    // 清除所有 source 样式
    el.classList.remove('murmur-idle', 'murmur-proactive');
    // 添加对应样式
    if (source === 'idle') {
        el.classList.add('murmur-idle');
    } else if (source === 'proactive') {
        el.classList.add('murmur-proactive');
    }

    // 清除上一次定时器
    if (_murmurTimer) clearTimeout(_murmurTimer);

    // progress 3.5s，idle 5s，proactive 8s 后淡出
    const duration = { 'progress': 3500, 'idle': 5000, 'proactive': 8000 }[source] || 3500;
    _murmurTimer = setTimeout(() => {
        el.classList.add('fade-out');
        el.classList.remove('visible');
    }, duration);
}

// ── 认知轻提示 (Cognitive Nudge) ──

function showCognitiveNudge(hint) {
    // 移除已有的 nudge
    const existing = document.getElementById('cognitive-nudge');
    if (existing) existing.remove();

    const nudge = document.createElement('div');
    nudge.id = 'cognitive-nudge';
    nudge.className = 'cognitive-nudge';
    nudge.innerHTML = `
        <span class="nudge-icon">💡</span>
        <span class="nudge-text">${hint || '我想到了点什么...'}</span>
    `;

    // 点击后自动开启 externalize
    nudge.addEventListener('click', () => {
        const checkbox = document.getElementById('externalize-checkbox');
        if (checkbox && !checkbox.checked) {
            checkbox.checked = true;
            checkbox.dispatchEvent(new Event('change'));
        }
        nudge.style.opacity = '0';
        nudge.style.transform = 'translateY(-10px)';
        setTimeout(() => nudge.remove(), 400);
    });

    document.body.appendChild(nudge);

    // 8 秒后自动淡出
    setTimeout(() => {
        if (nudge.parentNode) {
            nudge.style.opacity = '0';
            nudge.style.transform = 'translateY(-10px)';
            setTimeout(() => nudge.remove(), 400);
        }
    }, 8000);
}

// ── 外显开关 (Externalize Toggle) ──

(function initExternalizeToggle() {
    const checkbox = document.getElementById('externalize-checkbox');
    if (!checkbox) return;
    checkbox.addEventListener('change', function() {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            sendWS('toggle_externalize', '', { enabled: this.checked });
        }
    });
})();

