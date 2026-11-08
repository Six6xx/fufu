/* 04-model-selector.js — 聊天模型下拉选择器 */

// ── 模型选择器 ──

const ModelSelector = (() => {
    let isOpen = false;

    function init(model, providerModels, providerId, providerName) {
        state.currentModel = model || '';
        state.providerModels = providerModels || [];
        state.providerId = providerId || null;
        state.providerName = providerName || '';
        _updateDisplay();
    }

    function update(model, providerModels, providerId, providerName) {
        if (model !== undefined) state.currentModel = model;
        if (providerModels !== undefined) state.providerModels = providerModels;
        if (providerId !== undefined) state.providerId = providerId;
        if (providerName !== undefined) state.providerName = providerName;
        _updateDisplay();
    }

    function _updateDisplay() {
        if (modelBarName) {
            modelBarName.textContent = state.currentModel || '--';
            modelBarName.title = state.currentModel || '--';
        }
        if (modelBarProvider) {
            modelBarProvider.textContent = state.providerName || '';
            modelBarProvider.title = state.providerName || '';
            modelBarProvider.style.display = state.providerName ? '' : 'none';
        }
    }

    function toggle() {
        isOpen = !isOpen;
        if (modelDropdown) modelDropdown.classList.toggle('active', isOpen);
        if (modelBarArrow) modelBarArrow.classList.toggle('open', isOpen);
        if (isOpen) _render();
    }

    function close() {
        if (!isOpen) return;
        isOpen = false;
        if (modelDropdown) modelDropdown.classList.remove('active');
        if (modelBarArrow) modelBarArrow.classList.remove('open');
    }

    function _render() {
        if (!modelDropdown) return;
        modelDropdown.innerHTML = '';
        const models = state.providerModels || [];
        if (models.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'model-dropdown-empty';
            empty.textContent = state.providerId ? '暂无可用模型' : '未配置供应商，请在设置中添加';
            modelDropdown.appendChild(empty);
            return;
        }
        for (const m of models) {
            const item = document.createElement('div');
            item.className = 'model-item' + (m.name === state.currentModel ? ' active' : '');
            const nameSpan = document.createElement('span');
            nameSpan.className = 'model-item-name';
            nameSpan.textContent = m.name;
            const badge = document.createElement('span');
            badge.className = 'model-item-badge ' + (m.type || 'llm');
            badge.textContent = (m.type || 'llm').toUpperCase();
            item.appendChild(nameSpan);
            item.appendChild(badge);
            item.addEventListener('click', () => select(m.name));
            modelDropdown.appendChild(item);
        }
    }

    function select(modelName) {
        if (modelName === state.currentModel) {
            close();
            return;
        }
        if (state.ws && state.ws.readyState === WebSocket.OPEN && state.providerId) {
            sendWS('model_set', '', {
                provider_id: state.providerId,
                model: modelName,
            });
        }
        close();
    }

    return { init, update, toggle, close };
})();
