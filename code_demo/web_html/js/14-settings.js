/* 14-settings.js — 设置页 + Skills + 删除确认 + 批量删除 */

// ── 默认模型选择器辅助函数 ──

let _providersCache = null;

async function _loadDefaultModels() {
    try {
        const resp = await fetch('/api/default-models');
        return await resp.json();
    } catch (e) {
        return { companion_model: null, agent_team_model: null, ocr_model: null };
    }
}

async function _loadProviders() {
    if (_providersCache) return _providersCache;
    try {
        const resp = await fetch('/api/providers');
        const data = await resp.json();
        _providersCache = data.providers || [];
        return _providersCache;
    } catch (e) {
        return [];
    }
}

async function _saveDefaultModel(key, providerId, modelName) {
    try {
        const current = await _loadDefaultModels();
        current[key] = { provider_id: providerId, model: modelName };
        await fetch('/api/default-models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(current),
        });
    } catch (e) {
        console.error('保存默认模型失败:', e);
    }
}

function _createModelSelectRow(label, key, typeFilter, currentValue) {
    const row = document.createElement('div');
    row.className = 'model-select-row';

    const labelEl = document.createElement('span');
    labelEl.className = 'model-select-label';
    labelEl.textContent = label;
    row.appendChild(labelEl);

    const btnWrap = document.createElement('div');
    btnWrap.style.position = 'relative';

    const btn = document.createElement('button');
    btn.className = 'model-select-btn';
    btn.type = 'button';

    const btnText = document.createElement('span');
    btnText.className = 'model-select-btn-text';
    if (currentValue && currentValue.provider_id && currentValue.model) {
        btnText.textContent = currentValue.provider_id + '/' + currentValue.model;
    } else {
        btnText.textContent = '未选择';
    }

    const btnArrow = document.createElement('span');
    btnArrow.className = 'model-select-btn-arrow';
    btnArrow.textContent = '▼';

    btn.appendChild(btnText);
    btn.appendChild(btnArrow);
    btnWrap.appendChild(btn);

    // 下拉面板（挂载到 body 避免 overflow 裁剪）
    const dropdown = document.createElement('div');
    dropdown.className = 'model-dropdown';
    document.body.appendChild(dropdown);

    btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const isOpen = dropdown.classList.contains('open');
        // 关闭其他所有打开的面板
        document.querySelectorAll('.model-dropdown.open').forEach(d => {
            d.classList.remove('open');
        });
        document.querySelectorAll('.model-select-btn.active').forEach(b => {
            b.classList.remove('active');
        });
        if (!isOpen) {
            btn.classList.add('active');
            // 计算按钮位置，定位 fixed 下拉面板
            const rect = btn.getBoundingClientRect();
            dropdown.style.top = (rect.bottom + 4) + 'px';
            dropdown.style.left = (rect.right - 320) + 'px';
            dropdown.classList.add('open');
            await _renderDropdown(dropdown, key, typeFilter, currentValue, btn, btnText);
        }
    });

    row.appendChild(btnWrap);
    return row;
}

async function _renderDropdown(dropdown, key, typeFilter, currentValue, btn, btnText) {
    dropdown.innerHTML = '';
    const providers = await _loadProviders();

    let hasModels = false;
    for (const provider of providers) {
        const models = (provider.models || []).filter(m => {
            if (!typeFilter) return true;
            return m.type === typeFilter;
        });
        if (models.length === 0) continue;
        hasModels = true;

        const group = document.createElement('div');
        group.className = 'model-provider-group expanded';

        const header = document.createElement('div');
        header.className = 'model-provider-header';

        const headerLeft = document.createElement('div');
        headerLeft.className = 'model-provider-header-left';

        const arrow = document.createElement('span');
        arrow.className = 'model-provider-arrow';
        arrow.textContent = '▸';
        headerLeft.appendChild(arrow);

        const nameSpan = document.createElement('span');
        nameSpan.textContent = provider.name;
        headerLeft.appendChild(nameSpan);

        header.appendChild(headerLeft);

        const countBadge = document.createElement('span');
        countBadge.className = 'model-provider-count';
        countBadge.textContent = models.length;
        header.appendChild(countBadge);

        header.addEventListener('click', (e) => {
            e.stopPropagation();
            group.classList.toggle('expanded');
        });

        group.appendChild(header);

        const items = document.createElement('div');
        items.className = 'model-provider-items';

        for (const model of models) {
            const item = document.createElement('div');
            item.className = 'model-item';
            if (currentValue && currentValue.provider_id === provider.name && currentValue.model === model.name) {
                item.classList.add('selected');
            }

            const itemName = document.createElement('span');
            itemName.className = 'model-item-name';
            itemName.textContent = model.name;
            item.appendChild(itemName);

            const badge = document.createElement('span');
            badge.className = 'model-item-badge ' + (model.type || 'llm');
            badge.textContent = (model.type || 'llm').toUpperCase();
            item.appendChild(badge);

            item.addEventListener('click', async (e) => {
                e.stopPropagation();
                currentValue = { provider_id: provider.name, model: model.name };
                btnText.textContent = provider.name + '/' + model.name;
                dropdown.classList.remove('open');
                btn.classList.remove('active');
                await _saveDefaultModel(key, provider.name, model.name);
            });

            items.appendChild(item);
        }
        group.appendChild(items);
        dropdown.appendChild(group);
    }

    if (!hasModels) {
        const empty = document.createElement('div');
        empty.className = 'model-dropdown-empty';
        empty.textContent = '暂无可用模型';
        dropdown.appendChild(empty);
    }
}

// 全局点击关闭下拉面板
document.addEventListener('click', (e) => {
    if (!e.target.closest('.model-select-btn') && !e.target.closest('.model-dropdown')) {
        document.querySelectorAll('.model-dropdown.open').forEach(d => {
            d.classList.remove('open');
        });
        document.querySelectorAll('.model-select-btn.active').forEach(b => {
            b.classList.remove('active');
        });
    }
});

// ── 设置页面 ──

function openSettings(category) {
    state.settingsOpen = true;
    state.settingsCategory = category || 'general';
    state.skillsViewMode = 'main';
    settingsOverlay.classList.add('active');
    if (state.sidebarOpen) toggleSidebar();
    updateSettingsNav();
    renderSettingsContent();
}

function closeSettings() {
    state.settingsOpen = false;
    state.skillsViewMode = 'main';
    exitBatchMode();
    exitManageMode();
    settingsOverlay.classList.remove('active');
}

function updateSettingsNav() {
    settingsNav.querySelectorAll('.settings-nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.category === state.settingsCategory);
    });
}

async function renderSettingsContent() {
    settingsContent.innerHTML = '';
    const inBatchOrManage = state.batchMode || state.manageMode;
    // 控制"管理Skills"按钮仅在 Skills 分类下显示
    if (manageSkillsBar) manageSkillsBar.style.display = (state.settingsCategory === 'skills') ? '' : 'none';
    // 控制"添加 Skills / Markdown 导入"按钮仅在 Skills 分类下且非批量/管理模式时显示
    if (skillActionBtns) skillActionBtns.style.display = (state.settingsCategory === 'skills' && !inBatchOrManage) ? '' : 'none';
    // 控制"新增 MCP 服务"按钮仅在 MCP 分类下且非批量模式时显示
    if (mcpActionBtns) mcpActionBtns.style.display = (state.settingsCategory === 'mcp' && !state.batchMode) ? '' : 'none';
    if (state.settingsCategory === 'general') {
        renderSettingsGeneral();
    } else if (state.settingsCategory === 'mcp') {
        await renderSettingsMcp();
    } else if (state.settingsCategory === 'skills') {
        await renderSettingsSkills();
    } else if (state.settingsCategory === 'providers') {
        await renderSettingsProviders();
    }
    // 管理模式下重新添加 manage-mode 类（renderSettingsContent 会清空 innerHTML）
    if (state.manageMode && state.settingsCategory === 'skills') {
        const grid = settingsContent.querySelector('.settings-grid');
        if (grid) grid.classList.add('manage-mode');
    }
    updateBatchBar();
}

// ── 基础设置渲染 ──

function _createToggleRow(label, desc, key) {
    const row = document.createElement('div');
    row.className = 'setting-row';

    const labelDiv = document.createElement('div');
    labelDiv.className = 'setting-label';
    labelDiv.innerHTML = `<span class="setting-label-text">${label}</span>${desc ? `<span class="setting-desc">${desc}</span>` : ''}`;

    const control = document.createElement('div');
    control.className = 'setting-control';

    const toggle = document.createElement('label');
    toggle.className = 'mcp-toggle';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!SettingsManager.get(key);
    cb.addEventListener('change', () => SettingsManager.set(key, cb.checked));
    const slider = document.createElement('span');
    slider.className = 'mcp-slider';
    toggle.appendChild(cb);
    toggle.appendChild(slider);
    control.appendChild(toggle);

    row.appendChild(labelDiv);
    row.appendChild(control);
    return row;
}

function _createNumberRow(label, desc, key, min, max, step, unit) {
    const row = document.createElement('div');
    row.className = 'setting-row';

    const labelDiv = document.createElement('div');
    labelDiv.className = 'setting-label';
    labelDiv.innerHTML = `<span class="setting-label-text">${label}</span>${desc ? `<span class="setting-desc">${desc}</span>` : ''}`;

    const control = document.createElement('div');
    control.className = 'setting-control';

    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'setting-number';
    input.min = min;
    input.max = max;
    input.step = step || 1;
    input.value = SettingsManager.get(key);
    input.addEventListener('change', () => {
        let v = parseInt(input.value, 10);
        if (isNaN(v)) v = SettingsManager.DEFAULTS[key];
        v = Math.max(min, Math.min(max, v));
        input.value = v;
        SettingsManager.set(key, v);
    });
    control.appendChild(input);
    if (unit) {
        const unitSpan = document.createElement('span');
        unitSpan.style.fontSize = '12px';
        unitSpan.style.color = 'var(--text-light)';
        unitSpan.textContent = unit;
        control.appendChild(unitSpan);
    }

    row.appendChild(labelDiv);
    row.appendChild(control);
    return row;
}

function _createSliderRow(label, desc, key, min, max, leftLabel, rightLabel) {
    const row = document.createElement('div');
    row.className = 'setting-row';

    const labelDiv = document.createElement('div');
    labelDiv.className = 'setting-label';
    labelDiv.innerHTML = `<span class="setting-label-text">${label}</span>${desc ? `<span class="setting-desc">${desc}</span>` : ''}`;

    const control = document.createElement('div');
    control.className = 'setting-control';

    const wrap = document.createElement('div');
    wrap.className = 'setting-slider-wrap';

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.className = 'setting-slider';
    slider.min = min;
    slider.max = max;
    slider.value = SettingsManager.get(key);

    // 更新滑块背景渐变
    function updateSliderBg() {
        const pct = ((slider.value - min) / (max - min)) * 100;
        slider.style.background = `linear-gradient(to right, var(--primary) 0%, var(--primary) ${pct}%, var(--bg-dark) ${pct}%, var(--bg-dark) 100%)`;
    }
    updateSliderBg();

    slider.addEventListener('input', () => {
        updateSliderBg();
        SettingsManager.set(key, parseInt(slider.value, 10));
    });

    const labels = document.createElement('div');
    labels.className = 'slider-labels';
    labels.innerHTML = `<span>${leftLabel}</span><span>${rightLabel}</span>`;

    wrap.appendChild(slider);
    wrap.appendChild(labels);
    control.appendChild(wrap);

    row.appendChild(labelDiv);
    row.appendChild(control);
    return row;
}

// 压缩阈值预设
const COMPRESSION_PRESETS = [
    { name: '轻量', value: 25, desc: '~100k tokens，频繁压缩以节约成本' },
    { name: '中等', value: 50, desc: '~500k tokens，平衡成本与上下文' },
    { name: '大量', value: 75, desc: '~750k tokens，保留更多上下文' },
    { name: '极限', value: 100, desc: '上下文超出模型限制后再压缩' },
];

function _getContextLimitFromPct(pct) {
    // pct 0-100 → 字符阈值
    // 0% → 200k chars, 50% → 500k chars (~170k tokens), 100% → 极限模式
    if (pct >= 100) return 99999999; // 极限模式，基本不压缩
    return Math.round(200000 + (pct / 100) * 600000); // 200k ~ 800k chars
}

function _formatCharsLimit(n) {
    if (n >= 99999999) return '极限';
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    return Math.round(n / 1000) + 'k';
}

function _createCompressionRow() {
    const row = document.createElement('div');
    row.className = 'setting-row setting-row-column';

    const labelDiv = document.createElement('div');
    labelDiv.className = 'setting-label';
    labelDiv.innerHTML = `<span class="setting-label-text">压缩阈值</span><span class="setting-desc">控制对话历史的上下文窗口大小，触发压缩的时机</span>`;
    row.appendChild(labelDiv);

    const control = document.createElement('div');
    control.className = 'setting-control setting-control-full';

    // 预设按钮组
    const presetGroup = document.createElement('div');
    presetGroup.className = 'compression-presets';

    const currentPct = SettingsManager.get('contextCompression');

    for (const preset of COMPRESSION_PRESETS) {
        const btn = document.createElement('button');
        btn.className = 'compression-preset-btn' + (currentPct === preset.value ? ' active' : '');
        btn.textContent = preset.name;
        btn.title = preset.desc;
        btn.dataset.value = preset.value;
        btn.addEventListener('click', () => {
            SettingsManager.set('contextCompression', preset.value);
            presetGroup.querySelectorAll('.compression-preset-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            updateSlider(preset.value);
            updateDisplay(preset.value);
        });
        presetGroup.appendChild(btn);
    }
    control.appendChild(presetGroup);

    // 滑块
    const sliderWrap = document.createElement('div');
    sliderWrap.className = 'compression-slider-wrap';

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.className = 'setting-slider';
    slider.min = 0;
    slider.max = 100;
    slider.step = 5;
    slider.value = currentPct;

    function updateSliderBg(val) {
        slider.style.background = `linear-gradient(to right, var(--primary) 0%, var(--primary) ${val}%, var(--bg-dark) ${val}%, var(--bg-dark) 100%)`;
    }
    updateSliderBg(currentPct);

    function updateSlider(val) {
        slider.value = val;
        updateSliderBg(val);
    }

    slider.addEventListener('input', () => {
        const val = parseInt(slider.value, 10);
        updateSliderBg(val);
        SettingsManager.set('contextCompression', val);
        // 更新预设按钮高亮
        presetGroup.querySelectorAll('.compression-preset-btn').forEach(b => {
            b.classList.toggle('active', parseInt(b.dataset.value) === val);
        });
        updateDisplay(val);
    });

    sliderWrap.appendChild(slider);
    control.appendChild(sliderWrap);

    // 显示当前值
    const display = document.createElement('div');
    display.className = 'compression-display';

    function updateDisplay(pct) {
        const limit = _getContextLimitFromPct(pct);
        const presetInfo = COMPRESSION_PRESETS.find(p => p.value === pct);
        const levelName = presetInfo ? presetInfo.name : '自定义';
        display.innerHTML = `<span class="compression-pct">${pct}%</span><span class="compression-level">${levelName}</span><span class="compression-limit">触发阈值: ${_formatCharsLimit(limit)} 字符</span>`;
    }
    updateDisplay(currentPct);

    control.appendChild(display);
    row.appendChild(control);
    return row;
}

function renderSettingsGeneral() {
    const form = document.createElement('div');
    form.className = 'settings-form';

    // ── 默认模型 ──
    const modelGroup = document.createElement('div');
    modelGroup.className = 'setting-group';
    const modelTitle = document.createElement('div');
    modelTitle.className = 'setting-group-title';
    modelTitle.textContent = '默认模型';
    modelGroup.appendChild(modelTitle);

    // 异步加载模型配置并渲染选择器
    _loadDefaultModels().then(config => {
        const selectors = [
            { key: 'companion_model', label: '伴侣AI模型', filter: null },
            { key: 'agent_team_model', label: 'Agent Team模型', filter: 'llm' },
            { key: 'ocr_model', label: 'OCR模型', filter: 'vlm' },
        ];
        selectors.forEach(sel => {
            const row = _createModelSelectRow(sel.label, sel.key, sel.filter, config[sel.key]);
            modelGroup.appendChild(row);
        });
    });

    form.appendChild(modelGroup);

    // ── 显示设置 ──
    const displayGroup = document.createElement('div');
    displayGroup.className = 'setting-group';
    const displayTitle = document.createElement('div');
    displayTitle.className = 'setting-group-title';
    displayTitle.textContent = '显示设置';
    displayGroup.appendChild(displayTitle);

    displayGroup.appendChild(_createNumberRow('字体大小', '消息气泡中的文字大小（12-24px）', 'fontSize', 12, 24, 1, 'px'));
    displayGroup.appendChild(_createToggleRow('Markdown 渲染', '开启后消息内容将渲染为 Markdown 格式', 'markdownEnabled'));
    displayGroup.appendChild(_createToggleRow('显示字数统计', '在消息底部显示文字数量', 'showWordCount'));
    displayGroup.appendChild(_createToggleRow('显示时间戳', '在消息底部显示发送时间', 'showTimestamp'));

    form.appendChild(displayGroup);

    // ── 发送设置 ──
    const sendGroup = document.createElement('div');
    sendGroup.className = 'setting-group';
    const sendTitle = document.createElement('div');
    sendTitle.className = 'setting-group-title';
    sendTitle.textContent = '发送设置';
    sendGroup.appendChild(sendTitle);

    sendGroup.appendChild(_createNumberRow('长文本阈值', '超过此字数将自动转为文件发送（500-10000）', 'longTextThreshold', 500, 10000, 100, '字'));

    form.appendChild(sendGroup);

    // ── 上下文管理 ──
    const ctxGroup = document.createElement('div');
    ctxGroup.className = 'setting-group';
    const ctxTitle = document.createElement('div');
    ctxTitle.className = 'setting-group-title';
    ctxTitle.textContent = '上下文管理';
    ctxGroup.appendChild(ctxTitle);

    ctxGroup.appendChild(_createCompressionRow());

    form.appendChild(ctxGroup);

    settingsContent.appendChild(form);
}

async function renderSettingsMcp() {
    try {
        const resp = await fetch('/api/mcp-status');
        const data = await resp.json();
        const grid = document.createElement('div');
        grid.className = 'settings-grid';
        for (const srv of (data.servers || [])) {
            const card = createSettingsCard(srv.name, srv.type, srv.connected, srv.tool_count);
            card.style.cursor = 'pointer';
            card.addEventListener('click', () => {
                if (state.batchMode) {
                    toggleCardSelect(srv.name, card);
                } else {
                    openMcpDetailModal(srv);
                }
            });
            grid.appendChild(card);
        }
        if (!data.servers || data.servers.length === 0) {
            grid.innerHTML = '<div class="settings-empty">未配置 MCP 服务器</div>';
        }
        settingsContent.appendChild(grid);
    } catch (e) {
        settingsContent.innerHTML = '<div class="settings-empty">获取 MCP 状态失败</div>';
    }
}

async function renderSettingsSkills() {
    try {
        const [skillsResp, retiredResp] = await Promise.all([
            fetch('/api/skills'),
            fetch('/api/skills/retired'),
        ]);
        const skillsData = await skillsResp.json();
        const retiredData = await retiredResp.json();
        state.settingsSkills = skillsData.skills || [];
        const retiredSkills = retiredData.retired_skills || [];

        // 分组：用户技能 vs 自动生成技能
        const userSkills = state.settingsSkills.filter(s => s.source === 'manual');
        const autoSkills = state.settingsSkills.filter(s => s.source === 'auto' || s.source === 'merged');

        if (state.skillsViewMode === 'auto_folder') {
            // ── 自学习技能子视图 ──
            const backBtn = document.createElement('button');
            backBtn.className = 'skill-folder-back';
            backBtn.innerHTML = '← 返回';
            backBtn.addEventListener('click', () => {
                state.skillsViewMode = 'main';
                renderSettingsContent();
            });
            settingsContent.appendChild(backBtn);

            const autoHeader = document.createElement('h3');
            autoHeader.className = 'skills-section-title';
            autoHeader.textContent = `自学习技能 (${autoSkills.length})`;
            settingsContent.appendChild(autoHeader);

            if (autoSkills.length > 0) {
                const grid = document.createElement('div');
                grid.className = 'settings-grid';
                for (const skill of autoSkills) {
                    grid.appendChild(_createSkillCard(skill));
                }
                settingsContent.appendChild(grid);
            } else {
                const empty = document.createElement('div');
                empty.className = 'settings-empty';
                empty.textContent = '暂无自学习技能';
                settingsContent.appendChild(empty);
            }
        } else {
            // ── 主视图 ──
            if (userSkills.length > 0) {
                const activeHeader = document.createElement('h3');
                activeHeader.className = 'skills-section-title';
                activeHeader.textContent = `活跃技能 (${userSkills.length})`;
                settingsContent.appendChild(activeHeader);

                const grid = document.createElement('div');
                grid.className = 'settings-grid';
                for (const skill of userSkills) {
                    grid.appendChild(_createSkillCard(skill));
                }
                settingsContent.appendChild(grid);
            }

            // 自学习技能文件夹入口
            if (autoSkills.length > 0) {
                const folderCard = document.createElement('div');
                folderCard.className = 'skill-folder-card';
                folderCard.innerHTML = `
                    <div class="folder-icon">📁</div>
                    <div class="folder-info">
                        <span class="folder-name">自学习技能</span>
                        <span class="folder-count">${autoSkills.length} 个技能</span>
                    </div>
                    <div class="folder-arrow">›</div>
                `;
                folderCard.addEventListener('click', () => {
                    state.skillsViewMode = 'auto_folder';
                    renderSettingsContent();
                });
                settingsContent.appendChild(folderCard);
            }

            // 空状态
            if (userSkills.length === 0 && autoSkills.length === 0 && retiredSkills.length === 0) {
                const empty = document.createElement('div');
                empty.className = 'settings-empty';
                empty.textContent = '未找到 Skills（请将 SKILL.md 放入 skills 目录）';
                settingsContent.appendChild(empty);
            }
        }

        // 已被学习的技能（已下线）折叠文件夹
        if (retiredSkills.length > 0) {
            const folder = document.createElement('details');
            folder.className = 'retired-folder';

            const folderHeader = document.createElement('summary');
            folderHeader.className = 'retired-folder-header';
            folderHeader.innerHTML = `<span class="retired-folder-icon">📁</span><span>已被学习的技能 (${retiredSkills.length})</span><span class="retired-folder-arrow">▸</span>`;
            folder.appendChild(folderHeader);

            const grid = document.createElement('div');
            grid.className = 'settings-grid retired-folder-grid';
            for (const skill of retiredSkills) {
                const card = _createRetiredSkillCard(skill);
                grid.appendChild(card);
            }
            folder.appendChild(grid);
            settingsContent.appendChild(folder);
        }

        // 合并建议面板（仅主视图显示）
        if (state.skillsViewMode === 'main') {
            _appendMergePanel(settingsContent);
            _appendConflictPanel(settingsContent);
        }
    } catch (e) {
        settingsContent.innerHTML = '<div class="settings-empty">获取 Skills 列表失败</div>';
    }
}

/**
 * 在容器末尾追加合并建议面板
 */
function _appendMergePanel(container) {
    const panel = document.createElement('div');
    panel.className = 'merge-panel';
    panel.id = 'merge-suggestions-panel';

    const panelHeader = document.createElement('div');
    panelHeader.className = 'merge-panel-header';

    const panelTitle = document.createElement('span');
    panelTitle.className = 'merge-panel-title';
    panelTitle.textContent = '💡 技能合并建议';
    panelHeader.appendChild(panelTitle);

    const suggestBtn = document.createElement('button');
    suggestBtn.className = 'merge-suggest-btn';
    suggestBtn.textContent = '分析相似技能';
    suggestBtn.addEventListener('click', () => showMergeSuggestions(suggestBtn, panel));
    panelHeader.appendChild(suggestBtn);

    panel.appendChild(panelHeader);

    const panelBody = document.createElement('div');
    panelBody.className = 'merge-panel-body';
    panelBody.innerHTML = '<div class="merge-hint">点击「分析相似技能」以发现可合并的重复技能</div>';
    panel.appendChild(panelBody);

    container.appendChild(panel);
}

async function showMergeSuggestions(btn, panel) {
    btn.disabled = true;
    btn.textContent = '分析中...';
    const body = panel.querySelector('.merge-panel-body');
    try {
        const res = await fetch('/api/skills/merge-suggestions', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({threshold: 0.5}),
        });
        const data = await res.json();
        renderMergeSuggestions(body, data.suggestions || []);
    } catch (e) {
        if (body) body.innerHTML = '<div class="merge-hint">获取建议失败: ' + e.message + '</div>';
    }
    btn.disabled = false;
    btn.textContent = '分析相似技能';
}

function renderMergeSuggestions(container, suggestions) {
    if (!container) return;
    if (!suggestions || suggestions.length === 0) {
        container.innerHTML = '<div class="merge-hint">✅ 未发现可合并的重复技能</div>';
        return;
    }
    container.innerHTML = '';
    for (const s of suggestions) {
        const item = document.createElement('div');
        item.className = 'merge-item';

        const names = document.createElement('div');
        names.className = 'merge-item-names';
        const nameA = document.createElement('span');
        nameA.className = 'merge-skill-name';
        nameA.textContent = s.skill_a;
        const arrow = document.createElement('span');
        arrow.className = 'merge-arrow';
        arrow.textContent = ' ↔ ';
        const nameB = document.createElement('span');
        nameB.className = 'merge-skill-name';
        nameB.textContent = s.skill_b;
        names.appendChild(nameA);
        names.appendChild(arrow);
        names.appendChild(nameB);
        item.appendChild(names);

        const right = document.createElement('div');
        right.className = 'merge-item-right';

        const sim = document.createElement('span');
        sim.className = 'merge-similarity';
        sim.textContent = Math.round((s.similarity || 0) * 100) + '%';
        right.appendChild(sim);

        const mergeBtn = document.createElement('button');
        mergeBtn.className = 'merge-action-btn';
        mergeBtn.textContent = '合并';
        mergeBtn.addEventListener('click', () => confirmMerge(s.skill_a, s.skill_b, mergeBtn));
        right.appendChild(mergeBtn);

        item.appendChild(right);
        container.appendChild(item);
    }
}

async function confirmMerge(nameA, nameB, btn) {
    if (!confirm('确认将技能 "' + nameA + '" 与 "' + nameB + '" 合并？\n合并后将保留一个技能，另一个将被删除。')) return;
    if (btn) { btn.disabled = true; btn.textContent = '合并中...'; }
    try {
        const res = await fetch('/api/skills/merge', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name_a: nameA, name_b: nameB, merged_body: ''}),
        });
        if (res.ok) {
            setStatus('✅ 技能 "' + nameA + '" 与 "' + nameB + '" 已合并');
            refreshSkillList();
        } else {
            const data = await res.json().catch(() => ({}));
            setStatus('合并失败: ' + (data.error || res.statusText));
            if (btn) { btn.disabled = false; btn.textContent = '合并'; }
        }
    } catch (e) {
        setStatus('合并失败: ' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = '合并'; }
    }
}

/**
 * 刷新技能列表（仅在 skills 分类时生效）
 */
function refreshSkillList() {
    if (state.settingsCategory === 'skills' && state.settingsOpen) {
        renderSettingsContent();
    }
}

/**
 * 创建单个技能卡片
 */
function _createSkillCard(skill) {
    const tagInfo = _getSkillTagInfo(skill.source);
    const card = createSettingsCard(
        skill.name,
        'skill',
        true,
        null,
        skill.description,
    );
    card.style.cursor = 'pointer';
    card.classList.add('skill-card');

    // 来源标签
    const tag = document.createElement('span');
    tag.className = 'skill-tag ' + tagInfo.className;
    tag.textContent = tagInfo.text;
    card.appendChild(tag);

    // 使用次数
    const usageSpan = document.createElement('span');
    usageSpan.className = 'skill-usage';
    usageSpan.textContent = `使用 ${skill.usage_count || 0} 次`;
    card.appendChild(usageSpan);

    // 锁定/解锁图标（SVG，管理模式下显示在左上角）
    const lockIcon = document.createElement('div');
    lockIcon.className = 'skill-lock-icon' + (skill.locked ? '' : ' unlocked');
    lockIcon.title = skill.locked ? '点击解锁' : '点击锁定';
    lockIcon.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect class="lock-body" x="5" y="11" width="14" height="10" rx="2" fill="rgba(40,40,40,0.15)"/>
            <path class="lock-shackle" d="M8 11V7a4 4 0 0 1 8 0v4"/>
        </svg>`;
    lockIcon.addEventListener('click', async (e) => {
        e.stopPropagation();
        const prevLocked = skill.locked;
        const newLocked = !skill.locked;
        lockIcon.style.pointerEvents = 'none';
        lockIcon.style.opacity = '0.5';
        try {
            const res = await fetch('/api/skills/set-locked', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: skill.name, locked: newLocked}),
            });
            if (res.ok) {
                skill.locked = newLocked;
                lockIcon.title = newLocked ? '点击解锁' : '点击锁定';
                lockIcon.classList.toggle('unlocked', !newLocked);
                setStatus(newLocked ? `🔒 ${skill.name} 已锁定` : `🔓 ${skill.name} 已解锁`);
            } else {
                skill.locked = prevLocked;
                setStatus('操作失败，状态已回滚');
            }
        } catch (err) {
            skill.locked = prevLocked;
            setStatus('操作失败: ' + err.message);
        } finally {
            lockIcon.style.pointerEvents = '';
            lockIcon.style.opacity = '1';
        }
    });
    card.appendChild(lockIcon);

    // 主动学习技能：谱系 + 删除
    if (skill.source === 'merged') {
        const actionRow = document.createElement('div');
        actionRow.className = 'skill-action-row';

        const lineageBtn = document.createElement('button');
        lineageBtn.className = 'skill-lineage-btn';
        lineageBtn.title = '查看谱系';
        lineageBtn.innerHTML = '🧬';
        lineageBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            _toggleLineagePanel(card, skill.name, lineageBtn);
        });
        actionRow.appendChild(lineageBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'skill-delete-btn';
        deleteBtn.title = '删除';
        deleteBtn.innerHTML = '🗑️';
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            _deleteLearnedSkill(skill.name, deleteBtn);
        });
        actionRow.appendChild(deleteBtn);

        card.appendChild(actionRow);

        const lineagePanel = document.createElement('div');
        lineagePanel.className = 'lineage-tree';
        lineagePanel.style.display = 'none';
        lineagePanel.addEventListener('click', (e) => e.stopPropagation());
        card.appendChild(lineagePanel);
    }

    card.addEventListener('click', () => {
        if (state.manageMode) {
            toggleCardSelect(skill.name, card);
        } else if (state.batchMode) {
            toggleCardSelect(skill.name, card);
        } else {
            openSkillModalForEdit(skill);
        }
    });
    return card;
}

// ── 模型供应商渲染 ──

async function renderSettingsProviders() {
    // 子标签页容器（LLM | TTS | ASR）
    const wrapper = document.createElement('div');
    wrapper.className = 'provider-tabs-wrapper';

    // 标签页按钮
    const tabBar = document.createElement('div');
    tabBar.className = 'provider-tabs';
    const tabs = [
        ['llm', 'LLM'],
        ['tts', 'TTS'],
        ['asr', 'ASR'],
    ];
    const activeTab = state.providerTab || 'llm';
    for (const [key, label] of tabs) {
        const tab = document.createElement('button');
        tab.className = 'provider-tab' + (key === activeTab ? ' active' : '');
        tab.textContent = label;
        tab.addEventListener('click', () => {
            state.providerTab = key;
            renderSettingsContent();
        });
        tabBar.appendChild(tab);
    }
    wrapper.appendChild(tabBar);

    // 标签页内容
    const content = document.createElement('div');
    content.className = 'provider-tab-content';
    wrapper.appendChild(content);

    settingsContent.appendChild(wrapper);

    // 根据当前标签渲染
    if (activeTab === 'llm') {
        await _renderLLMTab(content);
    } else if (activeTab === 'tts') {
        await _renderTTSTab(content);
    } else {
        _renderASRTab(content);
    }
}

async function _renderLLMTab(container) {
    try {
        const resp = await fetch('/api/providers');
        const data = await resp.json();
        const providers = data.providers || [];
        const activeId = data.active_provider_id || null;

        // 添加按钮
        const addBtn = document.createElement('button');
        addBtn.className = 'provider-add-btn';
        addBtn.textContent = '+ 添加供应商';
        addBtn.addEventListener('click', () => _showProviderForm(container, null, activeId));
        container.appendChild(addBtn);

        if (providers.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'provider-empty';
            empty.textContent = '暂无模型供应商，点击上方按钮添加';
            container.appendChild(empty);
        }

        for (const p of providers) {
            const card = _createProviderCard(p, p.id === activeId, activeId);
            container.appendChild(card);
        }
    } catch (e) {
        container.innerHTML = '<div class="settings-empty">获取供应商列表失败</div>';
    }
}

async function _renderTTSTab(container) {
    try {
        const resp = await fetch('/api/voice-providers');
        const data = await resp.json();
        const providers = data.tts_providers || [];
        const activeId = data.active_tts_provider_id || null;
        const fallbackEnabled = data.tts_fallback_to_local !== false;

        // 回退开关
        const fallbackRow = document.createElement('div');
        fallbackRow.className = 'provider-fallback-row';
        const fallbackLabel = document.createElement('div');
        fallbackLabel.className = 'provider-fallback-label';
        fallbackLabel.innerHTML = '<strong>远程失败时回退本地</strong><br><span style="font-size:12px;color:var(--text-light,#888)">远程 TTS 服务不可用时自动使用本地引擎</span>';
        fallbackRow.appendChild(fallbackLabel);
        const fallbackToggle = document.createElement('label');
        fallbackToggle.className = 'toggle-switch';
        const fallbackInput = document.createElement('input');
        fallbackInput.type = 'checkbox';
        fallbackInput.checked = fallbackEnabled;
        fallbackInput.addEventListener('change', async () => {
            await fetch('/api/voice-providers/tts/set-fallback', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: fallbackInput.checked}),
            });
        });
        const slider = document.createElement('span');
        slider.className = 'toggle-slider';
        fallbackToggle.appendChild(fallbackInput);
        fallbackToggle.appendChild(slider);
        fallbackRow.appendChild(fallbackToggle);
        container.appendChild(fallbackRow);

        // 添加按钮
        const addBtn = document.createElement('button');
        addBtn.className = 'provider-add-btn';
        addBtn.textContent = '+ 添加 TTS 服务';
        addBtn.addEventListener('click', () => _showTTSProviderForm(container, null, activeId));
        container.appendChild(addBtn);

        if (providers.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'provider-empty';
            empty.innerHTML = '暂无远程 TTS 服务<br><span style="font-size:12px;color:var(--text-light,#888)">添加远程 CosyVoice 服务以获得更好的语音质量</span>';
            container.appendChild(empty);
        }

        for (const p of providers) {
            const card = _createTTSProviderCard(p, p.id === activeId, activeId);
            container.appendChild(card);
        }
    } catch (e) {
        container.innerHTML = '<div class="settings-empty">获取 TTS 供应商列表失败</div>';
    }
}

function _renderASRTab(container) {
    // ASR 预留
    const info = document.createElement('div');
    info.className = 'provider-empty';
    info.innerHTML = '<div style="text-align:center;padding:40px 20px">' +
        '<div style="font-size:36px;margin-bottom:12px">🎙️</div>' +
        '<strong>语音识别 (ASR)</strong>' +
        '<p style="margin-top:8px;color:var(--text-light,#888);font-size:13px">' +
        '当前使用 dashscope 云 API (paraformer-realtime-v2)<br>' +
        '未来可在此添加自部署的 ASR 服务（如 FunASR、Whisper）</p>' +
        '</div>';
    container.appendChild(info);
}

function _createTTSProviderCard(provider, isActive, activeId) {
    const card = document.createElement('div');
    card.className = 'provider-card' + (isActive ? ' active' : '');

    // Header
    const header = document.createElement('div');
    header.className = 'provider-card-header';
    const nameSpan = document.createElement('span');
    nameSpan.className = 'provider-card-name';
    nameSpan.textContent = provider.name;
    header.appendChild(nameSpan);
    if (isActive) {
        const tag = document.createElement('span');
        tag.className = 'provider-active-tag';
        tag.textContent = '当前';
        header.appendChild(tag);
    }
    card.appendChild(header);

    // URL
    const urlDiv = document.createElement('div');
    urlDiv.className = 'provider-card-url';
    urlDiv.textContent = provider.url;
    card.appendChild(urlDiv);

    // API Key (masked, if set)
    if (provider.api_key && provider.api_key !== '****') {
        const keyDiv = document.createElement('div');
        keyDiv.className = 'provider-card-key';
        keyDiv.textContent = 'Key: ' + provider.api_key;
        card.appendChild(keyDiv);
    }

    // Type badge
    const typeDiv = document.createElement('div');
    typeDiv.className = 'provider-card-models';
    const badge = document.createElement('span');
    badge.className = 'provider-badge llm';
    badge.textContent = provider.provider_type || 'remote';
    typeDiv.appendChild(badge);
    card.appendChild(typeDiv);

    // Actions
    const actions = document.createElement('div');
    actions.className = 'provider-card-actions';

    // 编辑
    const editBtn = document.createElement('button');
    editBtn.className = 'provider-action-btn';
    editBtn.textContent = '编辑';
    editBtn.addEventListener('click', () => _showTTSProviderForm(card.parentElement, provider, activeId));
    actions.appendChild(editBtn);

    // 测试连接
    const testBtn = document.createElement('button');
    testBtn.className = 'provider-action-btn';
    testBtn.textContent = '🔗 测试连接';
    testBtn.addEventListener('click', async () => {
        testBtn.disabled = true;
        testBtn.textContent = '测试中...';
        try {
            const resp = await fetch('/api/voice-providers/tts/test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: provider.id}),
            });
            const data = await resp.json();
            if (data.ok) {
                const h = data.health || {};
                setStatus(`连接成功 (设备: ${h.device || '?'}, 采样率: ${h.sample_rate || '?'})`);
            } else {
                setStatus('连接失败: ' + (data.error || ''));
            }
        } catch (e) {
            setStatus('测试失败: ' + e.message);
        }
        testBtn.disabled = false;
        testBtn.textContent = '🔗 测试连接';
    });
    actions.appendChild(testBtn);

    // 设为当前
    if (!isActive) {
        const setActiveBtn = document.createElement('button');
        setActiveBtn.className = 'provider-action-btn primary';
        setActiveBtn.textContent = '设为当前';
        setActiveBtn.addEventListener('click', async () => {
            try {
                await fetch('/api/voice-providers/tts/set-active', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: provider.id}),
                });
                await renderSettingsContent();
            } catch (e) {
                setStatus('设置失败: ' + e.message);
            }
        });
        actions.appendChild(setActiveBtn);
    } else {
        // 取消激活（回退到仅本地）
        const deactivateBtn = document.createElement('button');
        deactivateBtn.className = 'provider-action-btn';
        deactivateBtn.textContent = '取消激活';
        deactivateBtn.addEventListener('click', async () => {
            try {
                await fetch('/api/voice-providers/tts/set-active', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: null}),
                });
                await renderSettingsContent();
            } catch (e) {
                setStatus('操作失败: ' + e.message);
            }
        });
        actions.appendChild(deactivateBtn);
    }

    // 删除
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'provider-action-btn danger';
    deleteBtn.textContent = '删除';
    deleteBtn.addEventListener('click', async () => {
        if (!confirm(`确定删除 TTS 服务 "${provider.name}" ？`)) return;
        try {
            const resp = await fetch('/api/voice-providers/tts/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: provider.id}),
            });
            const data = await resp.json();
            if (data.ok) {
                await renderSettingsContent();
            } else {
                setStatus('删除失败: ' + (data.error || ''));
            }
        } catch (e) {
            setStatus('删除失败: ' + e.message);
        }
    });
    actions.appendChild(deleteBtn);

    card.appendChild(actions);
    return card;
}

function _showTTSProviderForm(container, existingProvider, activeId) {
    const isEdit = !!existingProvider;
    const form = document.createElement('div');
    form.className = 'provider-form';

    const title = document.createElement('div');
    title.className = 'provider-form-title';
    title.textContent = isEdit ? '编辑 TTS 服务' : '添加 TTS 服务';
    form.appendChild(title);

    // 名称
    const nameRow = _createProviderFormRow('名称', '如：GPU 服务器、本地 Docker', existingProvider?.name || '');
    form.appendChild(nameRow.div);

    // URL
    const urlRow = _createProviderFormRow('服务 URL', '如：http://192.168.1.100:9876', existingProvider?.url || '');
    form.appendChild(urlRow.div);

    // API Key (可选) — 脱敏值不预填
    const _ttsKey = existingProvider?.api_key || '';
    const _ttsDisplayKey = (!isEdit || _ttsKey.includes('****')) ? '' : _ttsKey;
    const keyRow = _createProviderFormRow('API Key（可选）', isEdit ? '留空则保留原密钥' : '如无需认证可留空', _ttsDisplayKey, isEdit ? 'password' : 'text');
    form.appendChild(keyRow.div);

    // 高级配置: 默认参考文本
    const config = existingProvider?.config || {};
    const promptRow = _createProviderFormRow('默认参考文本（可选）', '参考音频对应的文本', config.default_prompt_text || '');
    form.appendChild(promptRow.div);

    // 操作按钮
    const formActions = document.createElement('div');
    formActions.className = 'provider-form-actions';

    const saveBtn = document.createElement('button');
    saveBtn.className = 'provider-form-save';
    saveBtn.textContent = '保存';
    saveBtn.type = 'button';
    saveBtn.addEventListener('click', async () => {
        const name = nameRow.input.value.trim();
        const url = urlRow.input.value.trim();
        const apiKey = keyRow.input.value;
        const promptText = promptRow.input.value.trim();
        if (!name || !url) {
            setStatus('名称和 URL 不能为空');
            return;
        }

        const cfg = {};
        if (promptText) cfg.default_prompt_text = promptText;

        try {
            let resp;
            if (isEdit) {
                resp = await fetch('/api/voice-providers/tts/update', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: existingProvider.id, name, url, api_key: apiKey, config: cfg}),
                });
            } else {
                resp = await fetch('/api/voice-providers/tts/add', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name, url, api_key: apiKey.trim(), config: cfg}),
                });
            }
            const data = await resp.json();
            if (data.ok) {
                setStatus(isEdit ? 'TTS 服务已更新' : 'TTS 服务已添加');
                await renderSettingsContent();
            } else {
                setStatus('保存失败: ' + (data.error || ''));
            }
        } catch (e) {
            setStatus('保存失败: ' + e.message);
        }
    });
    formActions.appendChild(saveBtn);

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'provider-form-cancel';
    cancelBtn.textContent = '取消';
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', async () => {
        await renderSettingsContent();
    });
    formActions.appendChild(cancelBtn);

    form.appendChild(formActions);

    container.innerHTML = '';
    container.appendChild(form);
}

function _createProviderCard(provider, isActive, activeId) {
    const card = document.createElement('div');
    card.className = 'provider-card' + (isActive ? ' active' : '');

    // Header: name + active tag
    const header = document.createElement('div');
    header.className = 'provider-card-header';
    const nameSpan = document.createElement('span');
    nameSpan.className = 'provider-card-name';
    nameSpan.textContent = provider.name;
    header.appendChild(nameSpan);
    if (isActive) {
        const tag = document.createElement('span');
        tag.className = 'provider-active-tag';
        tag.textContent = '当前';
        header.appendChild(tag);
    }
    card.appendChild(header);

    // URL
    const urlDiv = document.createElement('div');
    urlDiv.className = 'provider-card-url';
    urlDiv.textContent = provider.url;
    card.appendChild(urlDiv);

    // API Key (masked, if set)
    if (provider.api_key && provider.api_key !== '****' && !provider.api_key.includes('****')) {
        const keyDiv = document.createElement('div');
        keyDiv.className = 'provider-card-key';
        keyDiv.textContent = 'Key: ' + provider.api_key;
        card.appendChild(keyDiv);
    }

    // Models
    const modelsDiv = document.createElement('div');
    modelsDiv.className = 'provider-card-models';
    const models = provider.models || [];
    if (models.length > 0) {
        for (const m of models) {
            const badge = document.createElement('span');
            badge.className = 'provider-badge ' + (m.type || 'llm');
            badge.textContent = m.name;
            badge.title = (m.type || 'llm').toUpperCase();
            modelsDiv.appendChild(badge);
        }
    } else {
        modelsDiv.innerHTML = '<span style="font-size:12px;color:var(--text-light,#888)">暂无模型</span>';
    }
    card.appendChild(modelsDiv);

    // Actions
    const actions = document.createElement('div');
    actions.className = 'provider-card-actions';

    const editBtn = document.createElement('button');
    editBtn.className = 'provider-action-btn';
    editBtn.textContent = '编辑';
    editBtn.addEventListener('click', () => _showProviderForm(card.parentElement, provider, activeId));
    actions.appendChild(editBtn);

    const discoverBtn = document.createElement('button');
    discoverBtn.className = 'provider-action-btn';
    discoverBtn.textContent = '🔍 发现模型';
    discoverBtn.addEventListener('click', async () => {
        discoverBtn.disabled = true;
        discoverBtn.textContent = '发现中...';
        try {
            const resp = await fetch('/api/providers/discover', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: provider.id}),
            });
            const data = await resp.json();
            if (data.ok) {
                setStatus(`发现 ${data.models.length} 个模型`);
                await renderSettingsContent();
            } else {
                setStatus('发现失败: ' + (data.error || ''));
            }
        } catch (e) {
            setStatus('发现失败: ' + e.message);
        }
    });
    actions.appendChild(discoverBtn);

    if (!isActive) {
        const setActiveBtn = document.createElement('button');
        setActiveBtn.className = 'provider-action-btn primary';
        setActiveBtn.textContent = '设为当前';
        setActiveBtn.addEventListener('click', async () => {
            try {
                await fetch('/api/providers/set-active', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: provider.id}),
                });
                // 切换当前模型到该供应商的第一个模型
                const models = provider.models || [];
                if (models.length > 0 && state.ws && state.ws.readyState === WebSocket.OPEN) {
                    sendWS('model_set', '', {
                        provider_id: provider.id,
                        model: models[0].name,
                    });
                }
                await renderSettingsContent();
            } catch (e) {
                setStatus('设置失败: ' + e.message);
            }
        });
        actions.appendChild(setActiveBtn);
    }

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'provider-action-btn danger';
    deleteBtn.textContent = '删除';
    deleteBtn.addEventListener('click', async () => {
        if (!confirm(`确定删除供应商 "${provider.name}" ？`)) return;
        try {
            const resp = await fetch('/api/providers/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: provider.id}),
            });
            const data = await resp.json();
            if (data.ok) {
                await renderSettingsContent();
            } else {
                setStatus('删除失败: ' + (data.error || ''));
            }
        } catch (e) {
            setStatus('删除失败: ' + e.message);
        }
    });
    actions.appendChild(deleteBtn);

    card.appendChild(actions);
    return card;
}

function _showProviderForm(container, existingProvider, activeId) {
    const isEdit = !!existingProvider;
    const form = document.createElement('div');
    form.className = 'provider-form';

    const title = document.createElement('div');
    title.className = 'provider-form-title';
    title.textContent = isEdit ? '编辑供应商' : '添加供应商';
    form.appendChild(title);

    // 名称
    const nameRow = _createProviderFormRow('名称', '如：DashScope、OpenAI、Ollama', existingProvider?.name || '');
    form.appendChild(nameRow.div);

    // URL
    const urlRow = _createProviderFormRow('API Base URL', '如：https://dashscope.aliyuncs.com/compatible-mode/v1', existingProvider?.url || '');
    form.appendChild(urlRow.div);

    // API Key — 脱敏值不预填
    const _llmKey = existingProvider?.api_key || '';
    const _llmDisplayKey = (!isEdit || _llmKey.includes('****')) ? '' : _llmKey;
    const keyRow = _createProviderFormRow('API Key (SK)', isEdit ? '留空则保留原密钥' : '', _llmDisplayKey, isEdit ? 'password' : 'text');
    form.appendChild(keyRow.div);

    // 模型列表
    const modelList = document.createElement('div');
    modelList.className = 'provider-model-list';
    const modelListTitle = document.createElement('div');
    modelListTitle.className = 'provider-model-list-title';
    modelListTitle.textContent = '模型列表';
    modelList.appendChild(modelListTitle);

    const modelRows = document.createElement('div');
    const existingModels = existingProvider?.models || [];
    for (const m of existingModels) {
        modelRows.appendChild(_createModelRow(m.name, m.type || 'llm'));
    }
    modelList.appendChild(modelRows);

    // 添加模型行按钮
    const addModelRow = document.createElement('div');
    addModelRow.className = 'provider-model-add-row';
    const addModelBtn = document.createElement('button');
    addModelBtn.className = 'provider-model-add-btn';
    addModelBtn.textContent = '+ 添加模型';
    addModelBtn.type = 'button';
    addModelBtn.addEventListener('click', () => {
        modelRows.appendChild(_createModelRow('', 'llm'));
    });
    addModelRow.appendChild(addModelBtn);
    modelList.appendChild(addModelRow);

    // 自动发现按钮（仅编辑模式）
    if (isEdit) {
        const discoverBtn = document.createElement('button');
        discoverBtn.className = 'provider-discover-btn';
        discoverBtn.type = 'button';
        discoverBtn.textContent = '🔍 自动发现模型';
        discoverBtn.addEventListener('click', async () => {
            discoverBtn.disabled = true;
            discoverBtn.textContent = '发现中...';
            try {
                const resp = await fetch('/api/providers/discover', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: existingProvider.id}),
                });
                const data = await resp.json();
                if (data.ok) {
                    // 将发现的模型添加到表单
                    for (const m of data.models) {
                        modelRows.appendChild(_createModelRow(m.name, m.type || 'llm'));
                    }
                    setStatus(`发现 ${data.models.length} 个模型`);
                } else {
                    setStatus('发现失败: ' + (data.error || ''));
                }
            } catch (e) {
                setStatus('发现失败: ' + e.message);
            }
            discoverBtn.disabled = false;
            discoverBtn.textContent = '🔍 自动发现模型';
        });
        modelList.appendChild(discoverBtn);
    }

    form.appendChild(modelList);

    // 操作按钮
    const formActions = document.createElement('div');
    formActions.className = 'provider-form-actions';

    const saveBtn = document.createElement('button');
    saveBtn.className = 'provider-form-save';
    saveBtn.textContent = '保存';
    saveBtn.type = 'button';
    saveBtn.addEventListener('click', async () => {
        const name = nameRow.input.value.trim();
        const url = urlRow.input.value.trim();
        const apiKey = keyRow.input.value;
        if (!name || !url) {
            setStatus('名称和 URL 不能为空');
            return;
        }
        // 收集模型列表
        const models = [];
        for (const row of modelRows.children) {
            const nameInput = row.querySelector('.provider-model-name');
            const typeSelect = row.querySelector('.provider-model-type');
            if (nameInput && nameInput.value.trim()) {
                models.push({name: nameInput.value.trim(), type: typeSelect.value});
            }
        }

        try {
            let resp;
            if (isEdit) {
                resp = await fetch('/api/providers/update', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: existingProvider.id, name, url, api_key: apiKey, models}),
                });
            } else {
                if (!apiKey.trim()) {
                    setStatus('API Key 不能为空');
                    return;
                }
                resp = await fetch('/api/providers/add', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name, url, api_key: apiKey.trim(), models}),
                });
            }
            const data = await resp.json();
            if (data.ok) {
                setStatus(isEdit ? '供应商已更新' : '供应商已添加');
                await renderSettingsContent();
            } else {
                setStatus('保存失败: ' + (data.error || ''));
            }
        } catch (e) {
            setStatus('保存失败: ' + e.message);
        }
    });
    formActions.appendChild(saveBtn);

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'provider-form-cancel';
    cancelBtn.textContent = '取消';
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', async () => {
        await renderSettingsContent();
    });
    formActions.appendChild(cancelBtn);

    form.appendChild(formActions);

    // 替换内容
    container.innerHTML = '';
    container.appendChild(form);
}

function _createProviderFormRow(label, placeholder, value, type) {
    const div = document.createElement('div');
    div.className = 'provider-form-row';
    const labelEl = document.createElement('label');
    labelEl.className = 'provider-form-label';
    labelEl.textContent = label;
    div.appendChild(labelEl);
    const input = document.createElement('input');
    input.className = 'provider-form-input';
    input.type = type || 'text';
    input.placeholder = placeholder;
    input.value = value || '';
    div.appendChild(input);
    return {div, input};
}

function _createModelRow(name, type) {
    const row = document.createElement('div');
    row.className = 'provider-model-row';

    const nameInput = document.createElement('input');
    nameInput.className = 'provider-model-name';
    nameInput.placeholder = '模型名称';
    nameInput.value = name || '';

    const typeSelect = document.createElement('select');
    typeSelect.className = 'provider-model-type';
    const llmOpt = document.createElement('option');
    llmOpt.value = 'llm';
    llmOpt.textContent = 'LLM';
    const vlmOpt = document.createElement('option');
    vlmOpt.value = 'vlm';
    vlmOpt.textContent = 'VLM';
    typeSelect.appendChild(llmOpt);
    typeSelect.appendChild(vlmOpt);
    typeSelect.value = type || 'llm';

    const removeBtn = document.createElement('button');
    removeBtn.className = 'provider-model-remove';
    removeBtn.type = 'button';
    removeBtn.textContent = '×';
    removeBtn.addEventListener('click', () => row.remove());

    row.appendChild(nameInput);
    row.appendChild(typeSelect);
    row.appendChild(removeBtn);
    return row;
}

function createSettingsCard(name, type, active, toolCount, description) {
    const card = document.createElement('div');
    card.className = 'settings-card' + (active ? ' active' : '');
    card.dataset.name = name;

    const icon = document.createElement('div');
    icon.className = 'settings-card-icon';
    icon.textContent = type === 'skill' ? '📄' : '🔧';
    const letterSpan = document.createElement('span');
    letterSpan.className = 'settings-card-letter';
    letterSpan.textContent = name.charAt(0).toUpperCase();
    icon.appendChild(letterSpan);

    const nameDiv = document.createElement('div');
    nameDiv.className = 'settings-card-name';
    nameDiv.textContent = name;
    nameDiv.title = name;

    const metaDiv = document.createElement('div');
    metaDiv.className = 'settings-card-meta';
    if (type === 'skill') {
        metaDiv.textContent = description || '';
        metaDiv.title = description || '';
    } else {
        metaDiv.textContent = active ? `${type} · ${toolCount || 0} tools` : type;
    }

    const dot = document.createElement('span');
    dot.className = 'settings-card-dot' + (active ? ' on' : '');

    // 批量选择圆圈（默认隐藏，batch-selecting 时显示）
    const selectCircle = document.createElement('span');
    selectCircle.className = 'settings-card-select';

    card.appendChild(selectCircle);
    card.appendChild(dot);
    card.appendChild(icon);
    card.appendChild(nameDiv);
    card.appendChild(metaDiv);

    return card;
}

// 设置页面事件绑定
if (settingsBtn) settingsBtn.addEventListener('click', () => openSettings('general'));
if (settingsClose) settingsClose.addEventListener('click', closeSettings);
if (settingsOverlay) {
    settingsOverlay.addEventListener('click', (e) => {
        if (e.target === settingsOverlay) closeSettings();
    });
}
if (settingsNav) {
    settingsNav.querySelectorAll('.settings-nav-item').forEach(item => {
        item.addEventListener('click', () => {
            state.settingsCategory = item.dataset.category;
            state.skillsViewMode = 'main';
            exitBatchMode();
            exitManageMode();
            updateSettingsNav();
            renderSettingsContent();
        });
    });
}

// ── Skill 模态窗口 ──

function openSkillModal() {
    if (!skillModal) return;
    state.skillEditOldName = null;
    if (skillNameInput) skillNameInput.value = '';
    if (skillDescInput) skillDescInput.value = '';
    if (skillBodyInput) skillBodyInput.value = '';
    updateSkillModalTitle();
    skillModal.classList.add('active');
}

function openSkillModalForEdit(skill) {
    if (!skillModal) return;
    state.skillEditOldName = skill.name;
    if (skillNameInput) skillNameInput.value = skill.name;
    if (skillDescInput) skillDescInput.value = skill.description || '';
    if (skillBodyInput) skillBodyInput.value = skill.body || '';
    updateSkillModalTitle();
    skillModal.classList.add('active');
}

function updateSkillModalTitle() {
    const title = skillModal?.querySelector('.skill-modal-header h3');
    const confirmBtn = $('skillModalConfirm');
    if (state.skillEditOldName) {
        if (title) title.textContent = '编辑 Skill';
        if (confirmBtn) confirmBtn.textContent = '保存';
        if (skillModalDelete) skillModalDelete.style.display = '';
    } else {
        if (title) title.textContent = '添加 Skill';
        if (confirmBtn) confirmBtn.textContent = '添加';
        if (skillModalDelete) skillModalDelete.style.display = 'none';
    }
}

function closeSkillModal() {
    if (skillModal) skillModal.classList.remove('active');
}

async function submitSkill() {
    const name = (skillNameInput?.value || '').trim();
    const description = (skillDescInput?.value || '').trim();
    const body = (skillBodyInput?.value || '').trim();
    if (!name) {
        alert('Name 不能为空');
        return;
    }
    if (!body) {
        alert('内容不能为空');
        return;
    }

    const isEdit = !!state.skillEditOldName;
    const url = isEdit ? '/api/skills/update' : '/api/skills/add';
    const payload = isEdit
        ? { old_name: state.skillEditOldName, name, description, body }
        : { name, description, body };

    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.ok) {
            closeSkillModal();
            await renderSettingsContent();
        } else {
            alert(data.error || (isEdit ? '保存失败' : '添加失败'));
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    }
}

if (addSkillBtn) addSkillBtn.addEventListener('click', openSkillModal);
if (addMcpFromSettingsBtn) addMcpFromSettingsBtn.addEventListener('click', openMcpModal);
if ($('skillModalClose')) $('skillModalClose').addEventListener('click', closeSkillModal);
if ($('skillModalCancel')) $('skillModalCancel').addEventListener('click', closeSkillModal);
if ($('skillModalConfirm')) $('skillModalConfirm').addEventListener('click', submitSkill);
if (skillModal) {
    skillModal.addEventListener('click', (e) => {
        if (e.target === skillModal) closeSkillModal();
    });
}

// ── Skill Markdown 导入模态 ──

function openImportModal() {
    if (!skillImportModal) return;
    if (skillImportContent) skillImportContent.value = '';
    if (skillFileInput) skillFileInput.value = '';
    if (skillFileName) skillFileName.textContent = '';
    hideImportError();
    skillImportModal.classList.add('active');
}

function closeImportModal() {
    if (skillImportModal) skillImportModal.classList.remove('active');
    hideImportError();
}

function hideImportError() {
    if (skillImportError) skillImportError.style.display = 'none';
}

function showImportError(errorMsg, example) {
    if (!skillImportError) return;
    if (skillImportErrorMsg) skillImportErrorMsg.textContent = errorMsg || '';
    if (skillImportErrorExample) skillImportErrorExample.textContent = example || '';
    skillImportError.style.display = '';
}

async function handleFileSelect(e) {
    const file = e.target?.files?.[0];
    if (!file) return;
    if (skillFileName) skillFileName.textContent = file.name;
    try {
        const text = await file.text();
        if (skillImportContent) skillImportContent.value = text;
        hideImportError();
    } catch (err) {
        if (skillFileName) skillFileName.textContent = '读取失败: ' + err.message;
    }
}

async function submitImport() {
    const content = (skillImportContent?.value || '').trim();
    if (!content) {
        alert('内容不能为空');
        return;
    }
    hideImportError();
    try {
        const resp = await fetch('/api/skills/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });
        const data = await resp.json();
        if (data.ok) {
            closeImportModal();
            await renderSettingsContent();
        } else {
            showImportError(data.error, data.example || '');
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    }
}

if (importSkillBtn) importSkillBtn.addEventListener('click', openImportModal);
if (skillFilePickBtn && skillFileInput) {
    skillFilePickBtn.addEventListener('click', () => skillFileInput.click());
    skillFileInput.addEventListener('change', handleFileSelect);
}
if ($('skillImportModalClose')) $('skillImportModalClose').addEventListener('click', closeImportModal);
if ($('skillImportModalCancel')) $('skillImportModalCancel').addEventListener('click', closeImportModal);
if ($('skillImportModalConfirm')) $('skillImportModalConfirm').addEventListener('click', submitImport);
if (skillImportModal) {
    skillImportModal.addEventListener('click', (e) => {
        if (e.target === skillImportModal) closeImportModal();
    });
}

// ── Skill 模态删除按钮 ──
if (skillModalDelete) skillModalDelete.addEventListener('click', () => {
    if (state.skillEditOldName) showDeleteConfirm('skill', state.skillEditOldName);
});

// ── 删除确认对话框事件绑定 ──
if ($('deleteConfirmNo')) $('deleteConfirmNo').addEventListener('click', closeDeleteConfirm);
if ($('deleteConfirmYes')) $('deleteConfirmYes').addEventListener('click', confirmDelete);
if (deleteConfirmModal) {
    deleteConfirmModal.addEventListener('click', (e) => {
        if (e.target === deleteConfirmModal) closeDeleteConfirm();
    });
}

// ── 管理模式（Skills 专用） ──

function enterManageMode() {
    state.manageMode = true;
    state.batchSelected = [];
    state.batchCategory = 'skills';
    // 给 grid 加 manage-mode 类以显示锁图标和选择圆圈
    const grid = settingsContent.querySelector('.settings-grid');
    if (grid) grid.classList.add('manage-mode');
    // 隐藏添加/导入按钮
    if (skillActionBtns) skillActionBtns.style.display = 'none';
    // 切换按钮文字和样式
    if (manageSkillsBtn) {
        manageSkillsBtn.textContent = '退出管理';
        manageSkillsBtn.classList.add('active');
    }
    updateBatchBar();
}

function exitManageMode() {
    state.manageMode = false;
    state.batchSelected = [];
    state.batchCategory = null;
    // 移除 grid 的 manage-mode 类
    const grid = settingsContent.querySelector('.settings-grid');
    if (grid) grid.classList.remove('manage-mode');
    // 移除所有卡片的 selected 类
    settingsContent.querySelectorAll('.settings-card.selected').forEach(c => c.classList.remove('selected'));
    // 恢复按钮文字和样式
    if (manageSkillsBtn) {
        manageSkillsBtn.textContent = '管理Skills';
        manageSkillsBtn.classList.remove('active');
    }
    // 恢复 skillActionBtns
    if (skillActionBtns) skillActionBtns.style.display = state.settingsCategory === 'skills' ? '' : 'none';
    updateBatchBar();
}

// 管理按钮点击事件
if (manageSkillsBtn) manageSkillsBtn.addEventListener('click', () => {
    if (state.manageMode) {
        exitManageMode();
    } else {
        enterManageMode();
    }
});

// ── 批量删除功能 ──

function enterBatchMode() {
    state.batchMode = true;
    state.batchCategory = state.settingsCategory;
    state.batchSelected = [];
    // 给 grid 加 batch-selecting 类以显示圆圈
    const grid = settingsContent.querySelector('.settings-grid');
    if (grid) grid.classList.add('batch-selecting');
    updateBatchBar();
    if (skillActionBtns) skillActionBtns.style.display = 'none';
    if (mcpActionBtns) mcpActionBtns.style.display = 'none';
}

function exitBatchMode() {
    state.batchMode = false;
    state.batchCategory = null;
    state.batchSelected = [];
    // 移除 grid 的 batch-selecting 类
    const grid = settingsContent.querySelector('.settings-grid');
    if (grid) grid.classList.remove('batch-selecting');
    // 移除所有卡片的 selected 类
    settingsContent.querySelectorAll('.settings-card.selected').forEach(c => c.classList.remove('selected'));
    updateBatchBar();
    // 恢复 skillActionBtns / mcpActionBtns
    if (skillActionBtns) skillActionBtns.style.display = state.settingsCategory === 'skills' ? '' : 'none';
    if (mcpActionBtns) mcpActionBtns.style.display = state.settingsCategory === 'mcp' ? '' : 'none';
}

function toggleCardSelect(name, cardEl) {
    const idx = state.batchSelected.indexOf(name);
    if (idx >= 0) {
        state.batchSelected.splice(idx, 1);
        cardEl.classList.remove('selected');
    } else {
        state.batchSelected.push(name);
        cardEl.classList.add('selected');
    }
    updateBatchConfirm();
    updateManageButtonStates();
}

function updateBatchBar() {
    if (!batchBar) return;
    // 基础设置分类下隐藏批量栏
    if (state.settingsCategory === 'general' || state.settingsCategory === 'providers') {
        batchBar.style.display = 'none';
        return;
    }
    if (state.manageMode) {
        // 管理模式：显示底部操作栏（左下角批量删除 + 下线）
        batchBar.style.display = 'flex';
        if (batchDeleteBtn) {
            batchDeleteBtn.parentElement.style.display = '';
            batchDeleteBtn.style.display = '';
        }
        if (batchRetireBtn) batchRetireBtn.style.display = '';
        if (batchRight) batchRight.style.display = 'none';
        updateManageButtonStates();
    } else if (state.batchMode) {
        // 批量模式（MCP用）：隐藏"批量删除"，显示"取消"+"确认"
        batchBar.style.display = 'flex';
        if (batchDeleteBtn) {
            batchDeleteBtn.parentElement.style.display = 'none';
        }
        if (batchRetireBtn) batchRetireBtn.style.display = 'none';
        if (batchRight) batchRight.style.display = 'flex';
    } else {
        // 正常模式：Skills 分类下隐藏批量栏（由管理按钮统一入口）
        if (state.settingsCategory === 'skills') {
            batchBar.style.display = 'none';
        } else {
            // MCP 分类下显示"批量删除"按钮
            batchBar.style.display = 'flex';
            if (batchDeleteBtn) {
                batchDeleteBtn.parentElement.style.display = '';
                batchDeleteBtn.style.display = '';
                batchDeleteBtn.disabled = false;
            }
            if (batchRetireBtn) batchRetireBtn.style.display = 'none';
            if (batchRight) batchRight.style.display = 'none';
        }
    }
}

// 管理模式下批量按钮状态更新
function updateManageButtonStates() {
    if (!state.manageMode) return;
    const hasSelection = state.batchSelected.length > 0;
    if (batchDeleteBtn) {
        batchDeleteBtn.disabled = !hasSelection;
    }
    if (batchRetireBtn) {
        batchRetireBtn.disabled = !hasSelection;
    }
}

function updateBatchConfirm() {
    if (!batchConfirmBtn) return;
    const hasSelection = state.batchSelected.length > 0;
    batchConfirmBtn.disabled = !hasSelection;
    batchConfirmBtn.classList.toggle('disabled', !hasSelection);
}

function showBatchConfirmModal() {
    if (!batchModal || !batchModalText || !batchModalList) return;
    const count = state.batchSelected.length;
    const categoryLabel = state.batchCategory === 'mcp' ? 'MCP 服务' : 'Skill';
    batchModalText.textContent = `是否确认删除 ${count} 个${categoryLabel}？`;

    // 构建列表
    batchModalList.innerHTML = '';
    state.batchSelected.forEach((name, i) => {
        const item = document.createElement('div');
        item.className = 'batch-item' + (i > 0 ? ' collapsed' : '');
        item.textContent = name;
        batchModalList.appendChild(item);
    });

    // 展开/收起按钮
    if (batchModalToggle) {
        batchModalToggle.style.display = count > 1 ? '' : 'none';
        batchModalToggle.textContent = '展开';
        batchModalToggle.dataset.expanded = 'false';
    }

    batchModal.classList.add('active');
}

function toggleBatchList() {
    if (!batchModalList || !batchModalToggle) return;
    const expanded = batchModalToggle.dataset.expanded === 'true';
    batchModalList.querySelectorAll('.batch-item').forEach((item, i) => {
        if (i > 0) item.classList.toggle('collapsed', expanded);
    });
    batchModalToggle.textContent = expanded ? '展开' : '收起';
    batchModalToggle.dataset.expanded = expanded ? 'false' : 'true';
}

function closeBatchConfirmModal() {
    if (batchModal) batchModal.classList.remove('active');
}

async function confirmBatchDelete() {
    closeBatchConfirmModal();
    const names = [...state.batchSelected];
    const category = state.batchCategory || (state.manageMode ? 'skills' : null);
    if (!names.length) return;

    if (category === 'mcp') {
        try {
            const resp = await fetch('/api/mcp/batch-delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ names }),
            });
            const data = await resp.json();
            if (data.ok) {
                setStatus(`已删除 ${data.deleted.length} 个 MCP 服务`);
                await fetchMcpStatus();
            } else {
                setStatus('批量删除失败: ' + (data.error || '未知错误'));
            }
        } catch (e) {
            setStatus('批量删除请求失败: ' + e.message);
        }
    } else if (category === 'skills') {
        let deletedCount = 0;
        for (const name of names) {
            try {
                const resp = await fetch('/api/skills/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name }),
                });
                const data = await resp.json();
                if (data.ok) deletedCount++;
            } catch (e) {
                // 忽略单个失败，继续删除
            }
        }
        setStatus(`已删除 ${deletedCount} 个 Skill`);
    }

    if (state.manageMode) {
        // 管理模式：清除选中状态但留在管理模式
        state.batchSelected = [];
        settingsContent.querySelectorAll('.settings-card.selected').forEach(c => c.classList.remove('selected'));
        await renderSettingsContent();
    } else {
        exitBatchMode();
        await renderSettingsContent();
    }
}

// ── 批量操作事件绑定 ──
if (batchDeleteBtn) batchDeleteBtn.addEventListener('click', () => {
    if (state.manageMode) {
        // 管理模式：直接显示批量删除确认弹窗
        if (state.batchSelected.length > 0) {
            state.batchCategory = 'skills';
            showBatchConfirmModal();
        }
    } else {
        // 非管理模式：进入批量模式（MCP用）
        enterBatchMode();
    }
});
if (batchRetireBtn) batchRetireBtn.addEventListener('click', async () => {
    if (!state.manageMode || state.batchSelected.length === 0) return;
    // 下线选中的 skills（使用 delete API）
    const names = [...state.batchSelected];
    if (!confirm(`确定下线 ${names.length} 个 Skills？\n下线后将无法恢复。`)) return;
    let retiredCount = 0;
    for (const name of names) {
        try {
            const resp = await fetch('/api/skills/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name }),
            });
            const data = await resp.json();
            if (data.ok) retiredCount++;
        } catch (e) {
            // 忽略单个失败
        }
    }
    setStatus(`已下线 ${retiredCount} 个 Skill`);
    // 管理模式：清除选中状态但留在管理模式
    state.batchSelected = [];
    settingsContent.querySelectorAll('.settings-card.selected').forEach(c => c.classList.remove('selected'));
    await renderSettingsContent();
});
if (batchCancelBtn) batchCancelBtn.addEventListener('click', exitBatchMode);
if (batchConfirmBtn) batchConfirmBtn.addEventListener('click', () => {
    if (state.batchSelected.length > 0) showBatchConfirmModal();
});
if ($('batchModalClose')) $('batchModalClose').addEventListener('click', closeBatchConfirmModal);
if ($('batchModalNo')) $('batchModalNo').addEventListener('click', closeBatchConfirmModal);
if ($('batchModalYes')) $('batchModalYes').addEventListener('click', confirmBatchDelete);
if (batchModalToggle) batchModalToggle.addEventListener('click', toggleBatchList);
if (batchModal) {
    batchModal.addEventListener('click', (e) => {
        if (e.target === batchModal) closeBatchConfirmModal();
    });
}

// ── 技能标签与已下线卡片 ──

function _getSkillTagInfo(source) {
    if (source === 'merged') return {text: '已学习', className: 'skill-tag-learned'};
    if (source === 'auto') return {text: 'AI', className: 'skill-tag-auto'};
    return {text: '手动', className: 'skill-tag-manual'};
}

function _createRetiredSkillCard(skill) {
    const card = createSettingsCard(skill.name, 'skill', false, null, skill.description);
    card.classList.add('skill-card', 'retired');
    card.style.cursor = 'default';

    const tag = document.createElement('span');
    tag.className = 'skill-tag skill-tag-retired';
    tag.textContent = '已下线';
    card.appendChild(tag);

    const retiredAt = document.createElement('div');
    retiredAt.className = 'skill-retired-at';
    retiredAt.textContent = skill.retired_at ? `下线: ${skill.retired_at}` : '';
    card.appendChild(retiredAt);

    const restoreBtn = document.createElement('button');
    restoreBtn.className = 'skill-restore-btn';
    restoreBtn.textContent = '恢复';
    restoreBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _restoreRetiredSkill(skill.name, restoreBtn);
    });
    card.appendChild(restoreBtn);
    return card;
}

// ── 谱系树 ──

async function _toggleLineagePanel(card, name, btn) {
    const panel = card.querySelector('.lineage-tree');
    if (!panel) return;
    if (panel.style.display === 'none') {
        panel.style.display = '';
        btn.classList.add('active');
        if (!panel.dataset.loaded) {
            btn.disabled = true;
            try {
                const res = await fetch(`/api/skills/lineage/${encodeURIComponent(name)}`);
                const data = await res.json();
                if (res.ok && data.lineage_tree) {
                    panel.innerHTML = '';
                    _renderLineageTree(panel, data.lineage_tree, 0);
                    panel.dataset.loaded = 'true';
                } else {
                    panel.innerHTML = '<div class="lineage-node">未找到谱系</div>';
                }
            } catch (e) {
                panel.innerHTML = '<div class="lineage-node">加载失败</div>';
            }
            btn.disabled = false;
        }
    } else {
        panel.style.display = 'none';
        btn.classList.remove('active');
    }
}

function _renderLineageTree(container, node, level) {
    const nodeEl = document.createElement('div');
    nodeEl.className = 'lineage-node';
    nodeEl.style.paddingLeft = (level * 16) + 'px';
    let icon = '●';
    let statusText = '技能';
    if (node.type === 'internal_skill') {
        icon = '🔒';
        statusText = '内部';
    } else if (node.status === 'retired') {
        icon = '⚪';
        statusText = '已下线';
    } else if (node.type === 'merged' || node.source === 'merged') {
        icon = '🔵';
        statusText = '已学习';
    } else if (node.status === 'active') {
        icon = '🟢';
        statusText = '活跃';
    }
    nodeEl.innerHTML = `<span class="lineage-node-icon">${icon}</span><span class="lineage-node-name" title="${node.name}">${node.name}</span><span class="lineage-node-status">${statusText}</span>`;
    container.appendChild(nodeEl);
    for (const child of node.children || []) {
        _renderLineageTree(container, child, level + 1);
    }
}

// ── 删除 / 恢复 ──

async function _deleteLearnedSkill(name, btn) {
    if (!confirm(`确定删除主动学习技能 "${name}"？\n删除后将触发回滚，可能恢复被替代的技能。`)) return;
    btn.disabled = true;
    const originalHtml = btn.innerHTML;
    btn.innerHTML = '...';
    try {
        const res = await fetch(`/api/skills/learned/${encodeURIComponent(name)}`, {method: 'DELETE'});
        const data = await res.json();
        if (res.ok) {
            const restored = data.restored || [];
            setStatus(`已删除 ${name}${restored.length > 0 ? '，回滚恢复 ' + restored.length + ' 个技能' : ''}`);
            refreshSkillList();
        } else {
            setStatus('删除失败: ' + (data.error || res.statusText));
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    } catch (e) {
        setStatus('删除失败: ' + e.message);
        btn.disabled = false;
        btn.innerHTML = originalHtml;
    }
}

async function _restoreRetiredSkill(name, btn) {
    if (!confirm(`确定恢复已下线技能 "${name}"？`)) return;
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = '恢复中...';
    try {
        const res = await fetch('/api/skills/restore', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name}),
        });
        const data = await res.json();
        if (res.ok) {
            const restored = data.restored || [];
            setStatus(`已恢复 ${name}${restored.length > 0 ? '（回滚 ' + restored.length + ' 个）' : ''}`);
            refreshSkillList();
        } else {
            setStatus('恢复失败: ' + (data.error || res.statusText));
            btn.disabled = false;
            btn.textContent = originalText;
        }
    } catch (e) {
        setStatus('恢复失败: ' + e.message);
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// ── 冲突检测与合并 ──

function _appendConflictPanel(container) {
    const panel = document.createElement('div');
    panel.className = 'conflict-panel';
    panel.id = 'conflict-detection-panel';

    const header = document.createElement('div');
    header.className = 'conflict-panel-header';

    const title = document.createElement('span');
    title.className = 'conflict-panel-title';
    title.textContent = '⚠️ 技能冲突检测';
    header.appendChild(title);

    const body = document.createElement('div');
    body.className = 'conflict-panel-body';
    body.innerHTML = '<div class="conflict-hint">点击「检测冲突」以发现内部技能与主动学习技能之间的冲突</div>';

    const checkBtn = document.createElement('button');
    checkBtn.className = 'conflict-check-btn';
    checkBtn.textContent = '检测冲突';
    checkBtn.addEventListener('click', () => _checkConflicts(checkBtn, body));
    header.appendChild(checkBtn);

    panel.appendChild(header);
    panel.appendChild(body);

    container.appendChild(panel);
}

async function _checkConflicts(btn, body) {
    btn.disabled = true;
    btn.textContent = '检测中...';
    try {
        const res = await fetch('/api/skills/check-conflicts', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
        });
        const data = await res.json();
        renderConflicts(body, data.conflicts || []);
    } catch (e) {
        if (body) body.innerHTML = '<div class="conflict-hint">检测失败: ' + e.message + '</div>';
    }
    btn.disabled = false;
    btn.textContent = '检测冲突';
}

function renderConflicts(container, conflicts) {
    if (!container) return;
    if (!conflicts || conflicts.length === 0) {
        container.innerHTML = '<div class="conflict-hint">✅ 未发现冲突</div>';
        return;
    }
    container.innerHTML = '';
    for (const c of conflicts) {
        const item = document.createElement('div');
        item.className = 'conflict-item';

        const info = document.createElement('div');
        info.className = 'conflict-item-info';
        info.innerHTML = `<div class="conflict-names"><span>${c.internal_skill}</span><span>↔</span><span>${c.conflicting_skill}</span></div><div class="conflict-meta">${c.conflict_type || ''} · 重叠度 ${Math.round((c.overlap_score || 0) * 100)}%</div>`;
        item.appendChild(info);

        const mergeBtn = document.createElement('button');
        mergeBtn.className = 'conflict-merge-btn';
        mergeBtn.textContent = '合并';
        mergeBtn.addEventListener('click', () => _mergeConflict(c, mergeBtn));
        item.appendChild(mergeBtn);

        container.appendChild(item);
    }
}

async function _mergeConflict(conflict, btn) {
    btn.disabled = true;
    btn.textContent = '合并中...';
    try {
        const res = await fetch('/api/skills/merge-conflict', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                internal_skill: conflict.internal_skill,
                conflicting_skill: conflict.conflicting_skill,
            }),
        });
        const data = await res.json();
        if (res.ok) {
            setStatus('已合并冲突: ' + (data.new_skill || ''));
            refreshSkillList();
        } else {
            setStatus('合并失败: ' + (data.error || res.statusText));
            btn.disabled = false;
            btn.textContent = '合并';
        }
    } catch (e) {
        setStatus('合并失败: ' + e.message);
        btn.disabled = false;
        btn.textContent = '合并';
    }
}
