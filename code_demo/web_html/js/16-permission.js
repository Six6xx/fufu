/* 16-permission.js — 权限审批 UI + 权限模式选择器 (PermissionUI) */

// ── 权限审批卡片 ──
// 当 Agent 需要用户确认操作时（risk=write/high），
// 在聊天流中插入审批卡片。用户点击允许/拒绝/始终允许后，
// 通过 WebSocket 回传 permission_response。

// ── 权限模式选择器 ──
// 在输入框模型栏右侧的下拉菜单，支持 auto/default/plan 三种模式。
// 切换后通过 permission_mode_set 消息通知后端。

const PermissionUI = (function() {

    // ── 审批卡片 ──

    /**
     * 显示权限审批卡片
     * @param {object} msg - permission_request 消息
     */
    function show(msg) {
        // 移除打字指示器
        const typing = messagesEl.querySelector('.typing-indicator');
        if (typing) typing.parentElement.remove();

        const el = document.createElement('div');
        el.className = 'message assistant permission-request';
        el.dataset.requestId = msg.request_id;

        const riskClass = `risk-${msg.risk_level || 'write'}`;
        const riskLabel = {
            'read': '只读',
            'write': '写入',
            'high': '高风险',
        }[msg.risk_level] || msg.risk_level || '操作';

        const toolArgs = typeof msg.tool_args === 'string'
            ? msg.tool_args
            : JSON.stringify(msg.tool_args, null, 2);

        el.innerHTML = `
            <div class="permission-card">
                <div class="permission-header">
                    <span class="permission-icon">&#9888;</span>
                    <span class="permission-title">需要授权</span>
                    <span class="permission-risk ${riskClass}">${riskLabel}</span>
                </div>
                <div class="permission-body">
                    <div class="permission-tool-name">${escapeHtml(msg.tool_name || '')}</div>
                    <pre class="permission-args">${escapeHtml(toolArgs)}</pre>
                </div>
                <div class="permission-actions">
                    <button class="perm-btn perm-approve" data-action="approve">允许</button>
                    <button class="perm-btn perm-deny" data-action="deny">拒绝</button>
                    <button class="perm-btn perm-always" data-action="always">始终允许</button>
                </div>
                <div class="permission-status" style="display:none"></div>
            </div>
        `;

        // 绑定按钮事件
        el.querySelectorAll('.perm-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                respond(msg.request_id, this.dataset.action, el);
            });
        });

        messagesEl.appendChild(el);
        scrollToBottom();
    }

    /**
     * 发送审批响应并禁用按钮
     */
    function respond(requestId, action, cardEl) {
        sendWS('permission_response', '', {
            request_id: requestId,
            action: action,
        });

        const btns = cardEl.querySelectorAll('.perm-btn');
        btns.forEach(btn => {
            btn.disabled = true;
            btn.classList.add('disabled');
            if (btn.dataset.action === action) {
                btn.classList.add('selected');
            }
        });

        const statusEl = cardEl.querySelector('.permission-status');
        if (statusEl) {
            statusEl.style.display = 'block';
            const labels = {
                'approve': '已允许',
                'deny': '已拒绝',
                'always': '已设为始终允许',
            };
            statusEl.textContent = labels[action] || action;
            statusEl.className = `permission-status status-${action}`;
        }
    }

    // ── 权限模式选择器 ──

    let _dropdownOpen = false;

    function initModeSelector() {
        const btn = document.getElementById('permModeBtn');
        const dropdown = document.getElementById('permModeDropdown');
        const arrow = document.getElementById('permModeArrow');
        if (!btn || !dropdown) return;

        // 点击按钮切换下拉（与 model-bar 完全一致的模式）
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            _dropdownOpen = !_dropdownOpen;
            dropdown.classList.toggle('active', _dropdownOpen);
            if (arrow) arrow.classList.toggle('open', _dropdownOpen);
        });

        // 点击模式项
        dropdown.querySelectorAll('.perm-mode-item').forEach(item => {
            item.addEventListener('click', function(e) {
                e.stopPropagation();
                const mode = this.dataset.mode;
                if (mode) {
                    sendWS('permission_mode_set', '', { mode });
                    closeDropdown();
                }
            });
        });

        // 点击其他地方关闭（与 model-bar 相同的逻辑）
        document.addEventListener('click', function(e) {
            const bar = document.getElementById('permModeBar');
            if (bar && !bar.contains(e.target)) {
                closeDropdown();
            }
        });
    }

    function closeDropdown() {
        _dropdownOpen = false;
        const dropdown = document.getElementById('permModeDropdown');
        const arrow = document.getElementById('permModeArrow');
        if (dropdown) dropdown.classList.remove('active');
        if (arrow) arrow.classList.remove('open');
    }

    /**
     * 更新权限模式显示（从 health 或 permission_mode_updated 调用）
     */
    function updateMode(mode) {
        const label = document.getElementById('permissionModeLabel');
        if (label) label.textContent = mode;

        document.querySelectorAll('.perm-mode-item').forEach(item => {
            item.classList.toggle('active', item.dataset.mode === mode);
        });
    }

    // ── 初始化 ──

    function init() {
        initModeSelector();
    }

    // DOMContentLoaded 时初始化
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    return { show, respond, updateMode, initModeSelector };
})();
