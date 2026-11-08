/* 06-file-manager.js — 文件上传/预览/类型图标 */

// ── 文件管理器 ──

const FileManager = (() => {
    let pendingFiles = [];  // [{id, file, name, type, status, serverId, size}]
    let nextId = 0;

    const TEXT_EXTS = ['txt', 'md', 'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'html', 'css',
                       'csv', 'log', 'yaml', 'yml', 'xml', 'toml', 'ini', 'cfg', 'sh', 'bash',
                       'zsh', 'sql', 'r', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'hpp', 'rb'];
    const CODE_EXTS = ['js', 'ts', 'jsx', 'tsx', 'py', 'html', 'css', 'json', 'yaml', 'yml',
                       'xml', 'sql', 'sh', 'bash', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'rb',
                       'toml', 'ini', 'cfg'];
    const DOC_EXTS = ['doc', 'docx'];
    const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB

    function getFileTypeName(mimeType, name) {
        const ext = (name || '').split('.').pop().toLowerCase();
        if (DOC_EXTS.includes(ext) || (mimeType && mimeType.includes('msword')) ||
            (mimeType && mimeType.includes('wordprocessingml'))) return 'doc';
        if (ext === 'pdf' || (mimeType && mimeType.includes('pdf'))) return 'pdf';
        if ((mimeType && mimeType.startsWith('image/')) || ['png','jpg','jpeg','gif','svg','webp','bmp','ico'].includes(ext)) return 'image';
        if (CODE_EXTS.includes(ext)) return 'code';
        if ((mimeType && mimeType.startsWith('text/')) || TEXT_EXTS.includes(ext)) return 'text';
        return 'default';
    }

    function getFileIcon(typeName) {
        const icons = { doc: 'DOC', pdf: 'PDF', image: 'IMG', code: '</>', text: 'TXT', default: 'FILE' };
        return icons[typeName] || 'FILE';
    }
    function getFileEmoji(typeName) {
        const emoji = { doc: '📄', pdf: '📕', image: '🖼', code: '💻', text: '📝', default: '📎' };
        return emoji[typeName] || '📎';
    }

    function truncateName(name) {
        if (!name) return '';
        // 仅对自动生成的长文本文件（message_日期.txt）用日期替代显示
        // 用户上传的文件始终保留原始文件名
        if (name.startsWith('message_')) {
            const ext = name.lastIndexOf('.') > 0 ? name.substring(name.lastIndexOf('.')) : '';
            const d = new Date();
            const dateStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
            return dateStr + ext;
        }
        return name;
    }

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function addFiles(fileList) {
        for (const file of fileList) {
            if (file.size > MAX_FILE_SIZE) {
                setStatus(`文件 ${file.name} 超过 50MB 限制`);
                continue;
            }
            pendingFiles.push({
                id: ++nextId,
                file,
                name: file.name,
                type: file.type || 'application/octet-stream',
                status: 'pending',
                serverId: null,
                size: file.size,
            });
        }
        renderPendingFiles();
    }

    function removeFile(id) {
        pendingFiles = pendingFiles.filter(f => f.id !== id);
        renderPendingFiles();
    }

    function renderPendingFiles() {
        if (!pendingFilesEl) return;
        pendingFilesEl.innerHTML = '';
        for (const pf of pendingFiles) {
            const typeName = getFileTypeName(pf.type, pf.name);
            const card = document.createElement('div');
            card.className = 'pending-file' + (pf.status === 'uploading' ? ' uploading' : '') +
                             (pf.status === 'error' ? ' error' : '');
            card.dataset.id = pf.id;

            const icon = document.createElement('div');
            icon.className = 'pending-file-icon type-' + typeName;
            if (pf.status === 'uploading') {
                const spinner = document.createElement('div');
                spinner.className = 'pending-file-spinner';
                icon.appendChild(spinner);
            } else {
                icon.textContent = getFileEmoji(typeName);
                const label = document.createElement('span');
                label.className = 'pending-file-label';
                label.textContent = getFileIcon(typeName);
                icon.appendChild(label);
            }

            const nameEl = document.createElement('div');
            nameEl.className = 'pending-file-name';
            nameEl.textContent = truncateName(pf.name);
            nameEl.title = pf.name;

            const removeBtn = document.createElement('button');
            removeBtn.className = 'pending-file-remove';
            removeBtn.textContent = '×';
            removeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                removeFile(pf.id);
            });

            card.addEventListener('click', () => previewFile(pf.id));

            card.appendChild(removeBtn);
            card.appendChild(icon);
            card.appendChild(nameEl);
            pendingFilesEl.appendChild(card);
        }
    }

    function previewFile(id) {
        const pf = pendingFiles.find(f => f.id === id);
        if (!pf || !filePreviewOverlay || !filePreviewBody || !filePreviewTitle) return;

        filePreviewTitle.textContent = pf.name;
        filePreviewBody.innerHTML = '';

        const typeName = getFileTypeName(pf.type, pf.name);
        if (typeName === 'image') {
            const url = URL.createObjectURL(pf.file);
            const img = document.createElement('img');
            img.src = url;
            img.onload = () => URL.revokeObjectURL(url);
            filePreviewBody.appendChild(img);
        } else if (['text', 'code'].includes(typeName) || pf.type.startsWith('text/') ||
                   TEXT_EXTS.includes((pf.name.split('.').pop() || '').toLowerCase())) {
            const reader = new FileReader();
            reader.onload = (e) => {
                const pre = document.createElement('pre');
                pre.textContent = e.target.result;
                filePreviewBody.appendChild(pre);
            };
            reader.readAsText(pf.file);
        } else {
            const info = document.createElement('div');
            info.className = 'file-preview-info';
            info.innerHTML = `
                <div class="file-preview-info-row"><span class="file-preview-info-label">文件名</span><span>${pf.name}</span></div>
                <div class="file-preview-info-row"><span class="file-preview-info-label">大小</span><span>${formatSize(pf.size)}</span></div>
                <div class="file-preview-info-row"><span class="file-preview-info-label">类型</span><span>${pf.type || '未知'}</span></div>
            `;
            filePreviewBody.appendChild(info);
        }

        filePreviewOverlay.classList.add('active');
    }

    function closePreview() {
        if (filePreviewOverlay) filePreviewOverlay.classList.remove('active');
    }

    async function uploadAll() {
        const results = [];
        for (const pf of pendingFiles) {
            pf.status = 'uploading';
            renderPendingFiles();
            try {
                const formData = new FormData();
                formData.append('file', pf.file);
                const resp = await fetch('/api/upload', { method: 'POST', body: formData });
                const data = await resp.json();
                if (data.ok) {
                    pf.status = 'done';
                    pf.serverId = data.id;
                    results.push({ id: data.id, name: data.name, type: data.type });
                } else {
                    pf.status = 'error';
                    setStatus('上传失败: ' + (data.error || pf.name));
                }
            } catch (e) {
                pf.status = 'error';
                setStatus('上传请求失败: ' + pf.name);
            }
            renderPendingFiles();
        }
        return results;
    }

    function clear() {
        pendingFiles = [];
        renderPendingFiles();
    }

    function hasFiles() { return pendingFiles.length > 0; }

    return { addFiles, removeFile, renderPendingFiles, previewFile,
             closePreview, uploadAll, clear, hasFiles };
})();
