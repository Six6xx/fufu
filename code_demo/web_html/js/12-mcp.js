/* 12-mcp.js — MCP 服务器管理 + 工具展开 + 模态窗口 */

// ── MCP 服务器管理 ──

async function fetchMcpStatus() {
    try {
        const resp = await fetch('/api/mcp-status');
        const data = await resp.json();
        state.mcpServers = data.servers || [];
        renderMcpList();
        updateMcpIndicator();
    } catch (e) {
        console.warn('MCP 状态获取失败:', e);
    }
}

function updateMcpIndicator() {
    if (!mcpReady) return;
    const anyConnected = state.mcpServers.some(s => s.connected);
    mcpIndicator.classList.toggle('on', anyConnected);
}

function renderMcpList() {
    if (!mcpReady) return;
    mcpList.innerHTML = '';
    if (state.mcpServers.length === 0) {
        mcpList.innerHTML = '<div class="mcp-empty">未配置 MCP 服务器</div>';
    } else {
        for (const srv of state.mcpServers) {
            const item = document.createElement('div');
            item.className = 'mcp-item';
            item.dataset.name = srv.name;

            const nameSpan = document.createElement('span');
            nameSpan.className = 'mcp-item-name';
            nameSpan.textContent = srv.name;

            const typeSpan = document.createElement('span');
            typeSpan.className = 'mcp-item-type';
            typeSpan.textContent = srv.type;

            const toolsSpan = document.createElement('span');
            toolsSpan.className = 'mcp-item-tools mcp-tools-expandable';
            const isExpanded = state.mcpExpanded === srv.name;
            const arrow = isExpanded ? '▾' : '▸';
            // 计算启用工具数（如有缓存数据）
            const cachedTools = state.mcpTools[srv.name];
            let toolCountText = `${srv.tool_count} tools`;
            if (cachedTools) {
                const enabled = cachedTools.filter(t => t.enabled).length;
                if (enabled !== srv.tool_count) toolCountText = `${enabled}/${srv.tool_count} tools`;
            }
            toolsSpan.textContent = srv.connected ? `${arrow} ${toolCountText}` : '';
            if (srv.connected && srv.tool_count > 0) {
                toolsSpan.addEventListener('click', (e) => {
                    e.stopPropagation();
                    toggleMcpExpand(srv.name, item);
                });
            }

            const toggleLabel = document.createElement('label');
            toggleLabel.className = 'mcp-toggle';
            const toggleInput = document.createElement('input');
            toggleInput.type = 'checkbox';
            toggleInput.checked = srv.connected;
            toggleInput.addEventListener('change', () => {
                toggleMcp(srv.name, toggleInput.checked, item);
            });
            const toggleSlider = document.createElement('span');
            toggleSlider.className = 'mcp-slider';
            toggleLabel.appendChild(toggleInput);
            toggleLabel.appendChild(toggleSlider);

            item.appendChild(nameSpan);
            item.appendChild(typeSpan);
            item.appendChild(toolsSpan);
            item.appendChild(toggleLabel);

            mcpList.appendChild(item);

            // 展开区域（默认隐藏）
            const expandArea = document.createElement('div');
            expandArea.className = 'mcp-tools-expand';
            expandArea.dataset.server = srv.name;
            expandArea.style.display = 'none';
            mcpList.appendChild(expandArea);

            // 恢复展开状态
            if (isExpanded) {
                expandArea.style.display = '';
                loadMcpTools(srv.name, expandArea);
            }

            // 如果已有错误，显示气泡（延迟渲染以获取准确位置）
            if (srv.error && !srv.connected) {
                requestAnimationFrame(() => showMcpError(srv.name, srv.error));
            }
        }
    }

    // 固定行：新增 MCP 服务
    const addRow = document.createElement('div');
    addRow.className = 'mcp-item mcp-add-row';
    addRow.innerHTML = '<span class="mcp-add-icon">＋</span><span class="mcp-item-name">新增 MCP 服务</span>';
    addRow.addEventListener('click', openMcpModal);
    mcpList.appendChild(addRow);

    updateMcpCount();
}

async function toggleMcp(name, enable, itemEl) {
    // 移除已有错误气泡
    const existingBubble = itemEl.querySelector('.mcp-error-bubble');
    if (existingBubble) existingBubble.remove();

    const input = itemEl.querySelector('input[type=checkbox]');

    try {
        const resp = await fetch('/api/mcp-toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, action: enable ? 'enable' : 'disable' }),
        });
        const data = await resp.json();

        if (enable && !data.ok) {
            // 连接失败：回退开关 + 显示错误气泡
            if (input) input.checked = false;
            showMcpError(name, data.error || '连接失败');
        } else {
            // 成功：就地更新工具数量 + 刷新指示器
            const toolsSpan = itemEl.querySelector('.mcp-tools-expandable');
            if (toolsSpan) {
                if (enable) {
                    toolsSpan.textContent = `▸ ${data.tool_count || 0} tools`;
                } else {
                    toolsSpan.textContent = '';
                    // 断开时收起展开区域并清理缓存
                    if (state.mcpExpanded === name) {
                        state.mcpExpanded = null;
                    }
                    delete state.mcpTools[name];
                    const expandArea = mcpList.querySelector(`.mcp-tools-expand[data-server="${name}"]`);
                    if (expandArea) expandArea.style.display = 'none';
                }
            }
            // 更新 state 缓存
            const srv = state.mcpServers.find(s => s.name === name);
            if (srv) {
                srv.connected = data.connected;
                srv.tool_count = data.tool_count || 0;
                srv.error = null;
            }
            updateMcpIndicator();
            updateMcpCount();
        }
    } catch (e) {
        console.warn('MCP 切换失败:', e);
        if (input) input.checked = !enable;
    }
}

function updateMcpCount() {
    if (!mcpReady) return;
    const connected = state.mcpServers.filter(s => s.connected).length;
    mcpCount.textContent = `${connected}/${state.mcpServers.length}`;
}

function showMcpError(name, errorMsg) {
    const itemEl = mcpList.querySelector(`[data-name="${name}"]`);
    if (!itemEl) return;

    // 移除已有气泡
    removeMcpErrorBubble(name);

    const bubble = document.createElement('div');
    bubble.className = 'mcp-error-bubble';
    bubble.dataset.serverName = name;
    bubble.textContent = errorMsg;

    // 计算宽度：128字符/行，monospace ~6.5px/字符
    const maxCharsPerLine = 128;
    const charWidth = 6.6;  // 11px monospace 每字符约 6.6px
    const padding = 20;
    const textLen = Math.min(errorMsg.length, maxCharsPerLine);
    const bubbleWidth = Math.ceil(textLen * charWidth + padding);

    // 渲染到 mcpPopup（overflow: visible），用 fixed 定位避开滚动裁剪
    const rect = itemEl.getBoundingClientRect();
    bubble.style.left = (rect.right + 10) + 'px';
    bubble.style.top = (rect.top + rect.height / 2) + 'px';
    bubble.style.maxWidth = bubbleWidth + 'px';
    bubble.style.position = 'fixed';

    mcpPopup.appendChild(bubble);

    // 5 秒后自动消失
    setTimeout(() => {
        if (bubble.parentNode) bubble.remove();
    }, 5000);
}

function removeMcpErrorBubble(name) {
    const existing = mcpPopup.querySelector(`.mcp-error-bubble[data-server-name="${name}"]`);
    if (existing) existing.remove();
}

function toggleMcpPopup() {
    if (!mcpReady) return;
    state.mcpPopupOpen = !state.mcpPopupOpen;
    mcpPopup.classList.toggle('active', state.mcpPopupOpen);
    if (state.mcpPopupOpen) {
        fetchMcpStatus();
    }
}

// ── MCP 工具展开/收起 ──

async function toggleMcpExpand(serverName, itemEl) {
    const wasExpanded = state.mcpExpanded === serverName;

    // 收起之前展开的
    if (state.mcpExpanded) {
        const prevArea = mcpList.querySelector(`.mcp-tools-expand[data-server="${state.mcpExpanded}"]`);
        if (prevArea) prevArea.style.display = 'none';
        const prevToolsSpan = mcpList.querySelector(`[data-name="${state.mcpExpanded}"] .mcp-tools-expandable`);
        const prevSrv = state.mcpServers.find(s => s.name === state.mcpExpanded);
        if (prevToolsSpan && prevSrv) {
            const cachedTools = state.mcpTools[state.mcpExpanded];
            let countText = `${prevSrv.tool_count} tools`;
            if (cachedTools) {
                const enabled = cachedTools.filter(t => t.enabled).length;
                if (enabled !== prevSrv.tool_count) countText = `${enabled}/${prevSrv.tool_count} tools`;
            }
            prevToolsSpan.textContent = `▸ ${countText}`;
        }
    }

    if (wasExpanded) {
        state.mcpExpanded = null;
        return;
    }

    // 展开新的
    state.mcpExpanded = serverName;
    const expandArea = mcpList.querySelector(`.mcp-tools-expand[data-server="${serverName}"]`);
    if (expandArea) {
        expandArea.style.display = '';
        const srv = state.mcpServers.find(s => s.name === serverName);
        if (srv) {
            const toolsSpan = itemEl.querySelector('.mcp-tools-expandable');
            if (toolsSpan) {
                const cachedTools = state.mcpTools[serverName];
                let countText = `${srv.tool_count} tools`;
                if (cachedTools) {
                    const enabled = cachedTools.filter(t => t.enabled).length;
                    if (enabled !== srv.tool_count) countText = `${enabled}/${srv.tool_count} tools`;
                }
                toolsSpan.textContent = `▾ ${countText}`;
            }
        }
        await loadMcpTools(serverName, expandArea);
    }
}

async function loadMcpTools(serverName, container) {
    try {
        const resp = await fetch(`/api/mcp-tools/${serverName}`);
        const data = await resp.json();
        state.mcpTools[serverName] = data.tools || [];
        renderMcpTools(serverName, container);
    } catch (e) {
        container.innerHTML = '<div class="mcp-tool-error">获取工具列表失败</div>';
    }
}

function renderMcpTools(serverName, container) {
    container.innerHTML = '';
    const tools = state.mcpTools[serverName] || [];
    for (const tool of tools) {
        const row = document.createElement('div');
        row.className = 'mcp-tool-sub-row';

        const info = document.createElement('div');
        info.className = 'mcp-tool-info';

        const nameDiv = document.createElement('div');
        nameDiv.className = 'mcp-tool-name';
        nameDiv.textContent = tool.name;

        const descDiv = document.createElement('div');
        descDiv.className = 'mcp-tool-desc';
        descDiv.textContent = tool.description || '(无描述)';

        info.appendChild(nameDiv);
        info.appendChild(descDiv);

        // 小号开关
        const toggleLabel = document.createElement('label');
        toggleLabel.className = 'mcp-toggle mcp-toggle-sm';
        const toggleInput = document.createElement('input');
        toggleInput.type = 'checkbox';
        toggleInput.checked = tool.enabled;
        toggleInput.addEventListener('change', () => {
            toggleMcpTool(serverName, tool.name, toggleInput.checked);
        });
        const toggleSlider = document.createElement('span');
        toggleSlider.className = 'mcp-slider';
        toggleLabel.appendChild(toggleInput);
        toggleLabel.appendChild(toggleSlider);

        row.appendChild(info);
        row.appendChild(toggleLabel);
        container.appendChild(row);
    }
}

async function toggleMcpTool(serverName, toolName, enable) {
    try {
        await fetch('/api/mcp-tool-toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ server: serverName, tool: toolName, enable }),
        });
        // 更新本地状态
        const tools = state.mcpTools[serverName];
        if (tools) {
            const t = tools.find(t => t.name === toolName);
            if (t) t.enabled = enable;
        }
        // 更新主行的工具数（启用数量）
        updateMcpToolCount(serverName);
    } catch (e) {
        console.warn('MCP 工具切换失败:', e);
    }
}

function updateMcpToolCount(serverName) {
    const tools = state.mcpTools[serverName] || [];
    const enabled = tools.filter(t => t.enabled).length;
    const srv = state.mcpServers.find(s => s.name === serverName);
    if (!srv) return;
    const toolsSpan = mcpList.querySelector(`[data-name="${serverName}"] .mcp-tools-expandable`);
    if (toolsSpan) {
        const arrow = state.mcpExpanded === serverName ? '▾' : '▸';
        const countText = enabled !== srv.tool_count ? `${enabled}/${srv.tool_count} tools` : `${srv.tool_count} tools`;
        toolsSpan.textContent = `${arrow} ${countText}`;
    }
}

// ── MCP 新增模态窗口 ──

function openMcpModal() {
    if (!mcpModal) return;
    mcpModal.classList.add('active');
    // 重置表单
    if (mcpNameInput) mcpNameInput.value = '';
    if (mcpUrlInput) mcpUrlInput.value = '';
    if (mcpHeadersInput) mcpHeadersInput.value = '';
    if (mcpCommandInput) mcpCommandInput.value = '';
    if (mcpEnvInput) mcpEnvInput.value = '';
    if (mcpJsonInput) mcpJsonInput.value = '';
    switchMcpTab('manual');
    switchMcpType('remote');
}

function closeMcpModal() {
    if (!mcpModal) return;
    mcpModal.classList.remove('active');
}

function switchMcpTab(tab) {
    document.querySelectorAll('.mcp-tab').forEach(t =>
        t.classList.toggle('active', t.dataset.tab === tab));
    const manualTab = $('mcpTabManual');
    const jsonTab = $('mcpTabJson');
    if (manualTab) manualTab.classList.toggle('active', tab === 'manual');
    if (jsonTab) jsonTab.classList.toggle('active', tab === 'json');
}

function switchMcpType(type) {
    state.mcpModalType = type;
    document.querySelectorAll('.mcp-type-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.type === type));
    const remoteFields = $('mcpRemoteFields');
    const localFields = $('mcpLocalFields');
    if (remoteFields) remoteFields.style.display = type === 'remote' ? '' : 'none';
    if (localFields) localFields.style.display = type === 'local' ? '' : 'none';
}

async function submitMcpAdd() {
    const isJsonTab = $('mcpTabJson') && $('mcpTabJson').classList.contains('active');

    if (isJsonTab) {
        const raw = mcpJsonInput ? mcpJsonInput.value.trim() : '';
        if (!raw) { setStatus('请输入 JSON 配置'); return; }
        try {
            const parsed = JSON.parse(raw);
            const servers = parsed.mcpServers || parsed;
            let added = 0, failed = 0;
            for (const [name, cfg] of Object.entries(servers)) {
                const resp = await fetch('/api/mcp-add', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, config: cfg }),
                });
                const data = await resp.json();
                if (data.ok) added++; else failed++;
            }
            setStatus(`导入完成: ${added} 成功, ${failed} 失败`);
        } catch (e) {
            setStatus('JSON 解析失败: ' + e.message);
            return;
        }
    } else {
        const name = mcpNameInput ? mcpNameInput.value.trim() : '';
        if (!name) { setStatus('请输入服务器名称'); return; }

        let cfg = {};
        if (state.mcpModalType === 'remote') {
            const url = mcpUrlInput ? mcpUrlInput.value.trim() : '';
            if (!url) { setStatus('请输入 URL'); return; }
            cfg.type = 'streamableHttp';
            cfg.url = url;
            const headersRaw = mcpHeadersInput ? mcpHeadersInput.value.trim() : '';
            if (headersRaw) {
                try { cfg.headers = JSON.parse(headersRaw); }
                catch { setStatus('HTTP Headers 格式错误，需为 JSON'); return; }
            }
        } else {
            const cmdRaw = mcpCommandInput ? mcpCommandInput.value.trim() : '';
            if (!cmdRaw) { setStatus('请输入命令'); return; }
            const parts = cmdRaw.split(/\s+/);
            cfg.type = 'stdio';
            cfg.command = parts[0];
            if (parts.length > 1) cfg.args = parts.slice(1);
            const envRaw = mcpEnvInput ? mcpEnvInput.value.trim() : '';
            if (envRaw) {
                const env = {};
                for (const line of envRaw.split('\n')) {
                    const eq = line.indexOf('=');
                    if (eq > 0) env[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
                }
                if (Object.keys(env).length) cfg.env = env;
            }
        }

        const resp = await fetch('/api/mcp-add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, config: cfg }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            setStatus('添加失败: ' + (data.error || '未知错误'));
            return;
        }
        setStatus(data.ok ? `MCP ${name} 已添加并连接` : `MCP ${name} 已添加，但连接失败: ${data.error}`);
    }

    closeMcpModal();
    await fetchMcpStatus();
}

// ── MCP 详情模态窗口 ──

function openMcpDetailModal(srv) {
    if (!mcpDetailModal) return;
    if (mcpDetailName) mcpDetailName.textContent = srv.name;
    if (mcpDetailType) mcpDetailType.textContent = srv.type || '—';
    if (mcpDetailStatus) {
        mcpDetailStatus.textContent = srv.connected ? '已连接' : '未连接';
        mcpDetailStatus.className = 'detail-value ' + (srv.connected ? 'connected' : 'disconnected');
    }
    if (mcpDetailTools) mcpDetailTools.textContent = srv.connected ? `${srv.tool_count || 0} 个` : '—';
    // 保存当前名称供删除使用
    mcpDetailModal.dataset.name = srv.name;
    mcpDetailModal.classList.add('active');
}

function closeMcpDetailModal() {
    if (mcpDetailModal) mcpDetailModal.classList.remove('active');
}

// ── 删除确认对话框 ──

function showDeleteConfirm(type, name) {
    state.deleteTarget = { type, name };
    if (deleteConfirmText) deleteConfirmText.textContent = `是否删除 ${name} ？`;
    if (deleteConfirmModal) deleteConfirmModal.classList.add('active');
}

function closeDeleteConfirm() {
    state.deleteTarget = null;
    if (deleteConfirmModal) deleteConfirmModal.classList.remove('active');
}

async function confirmDelete() {
    const target = state.deleteTarget;
    closeDeleteConfirm();
    if (!target) return;

    if (target.type === 'mcp') {
        try {
            const resp = await fetch('/api/mcp/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: target.name }),
            });
            const data = await resp.json();
            if (data.ok) {
                setStatus(`MCP ${target.name} 已删除`);
                // 刷新 MCP 指示器
                await fetchMcpStatus();
            } else {
                setStatus('删除失败: ' + (data.error || '未知错误'));
            }
        } catch (e) {
            setStatus('删除请求失败: ' + e.message);
        }
        closeMcpDetailModal();
        await renderSettingsContent();

    } else if (target.type === 'skill') {
        try {
            const resp = await fetch('/api/skills/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: target.name }),
            });
            const data = await resp.json();
            if (data.ok) {
                setStatus(`Skill ${target.name} 已删除`);
            } else {
                setStatus('删除失败: ' + (data.error || '未知错误'));
            }
        } catch (e) {
            setStatus('删除请求失败: ' + e.message);
        }
        closeSkillModal();
        await renderSettingsContent();
    }
}

// ── MCP 详情模态事件绑定 ──
if ($('mcpDetailClose')) $('mcpDetailClose').addEventListener('click', closeMcpDetailModal);
if ($('mcpDetailCancel')) $('mcpDetailCancel').addEventListener('click', closeMcpDetailModal);
if ($('mcpDetailDelete')) $('mcpDetailDelete').addEventListener('click', () => {
    const name = mcpDetailModal?.dataset.name || '';
    if (name) showDeleteConfirm('mcp', name);
});
if (mcpDetailModal) {
    mcpDetailModal.addEventListener('click', (e) => {
        if (e.target === mcpDetailModal) closeMcpDetailModal();
    });
}
