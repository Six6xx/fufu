/* 03-dir-picker.js — 目录选择弹窗 */
// ── 目录选择器 ──

const DirPicker = (() => {
    let currentPath = '';
    let onConfirm = null;

    async function open(initialPath, callback) {
        onConfirm = callback;
        if (dirPickerOverlay) dirPickerOverlay.classList.add('active');
        await navigate(initialPath || state.workspace || '');
    }

    async function navigate(path) {
        try {
            const resp = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
            const data = await resp.json();

            if (!data.ok) {
                // 路径不存在或无权限 → 清空列表，但保留输入框内容
                currentPath = path;
                if (dirPickerPathInput) dirPickerPathInput.value = path;
                if (dirPickerBody) dirPickerBody.innerHTML = '';
                return;
            }

            currentPath = data.current;
            if (dirPickerPathInput) dirPickerPathInput.value = currentPath;
            if (dirPickerBody) dirPickerBody.innerHTML = '';

            // 返回上级
            if (data.parent && dirPickerBody) {
                const up = createEntry('⬆', '返回上级', data.parent);
                up.classList.add('up');
                dirPickerBody.appendChild(up);
            }

            // 目录列表
            if (dirPickerBody) {
                for (const entry of data.entries) {
                    dirPickerBody.appendChild(createEntry('📁', entry.name, entry.path));
                }
            }
        } catch (e) {
            setStatus('浏览目录失败: ' + e.message);
        }
    }

    function createEntry(icon, name, targetPath) {
        const el = document.createElement('div');
        el.className = 'dir-picker-entry';
        el.innerHTML = `<span class="dir-picker-entry-icon">${icon}</span><span class="dir-picker-entry-name"></span>`;
        el.querySelector('.dir-picker-entry-name').textContent = name;
        el.addEventListener('click', () => navigate(targetPath));
        return el;
    }

    function close() {
        if (dirPickerOverlay) dirPickerOverlay.classList.remove('active');
    }

    function confirm() {
        if (onConfirm) onConfirm(currentPath);
        close();
    }

    return { open, close, confirm, navigate };
})();
