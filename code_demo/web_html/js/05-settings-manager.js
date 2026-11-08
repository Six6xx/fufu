/* 05-settings-manager.js — localStorage 设置管理 */

// ── 设置管理器 ──

const SettingsManager = (() => {
    const STORAGE_KEY = 'funingna_settings';
    const DEFAULTS = {
        fontSize: 14,
        markdownEnabled: true,
        longTextThreshold: 2000,
        showWordCount: false,
        showTimestamp: true,
        contextCompression: 50,
    };
    let settings = { ...DEFAULTS };

    function load() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (raw) {
                const parsed = JSON.parse(raw);
                settings = { ...DEFAULTS, ...parsed };
            }
        } catch (e) {
            settings = { ...DEFAULTS };
        }
    }

    function save() {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
        } catch (e) { /* quota exceeded, ignore */ }
    }

    function get(key) { return settings[key]; }

    function set(key, value) {
        settings[key] = value;
        save();
        apply(key);
    }

    function apply(key) {
        if (key === 'fontSize' || key === undefined) {
            document.documentElement.style.setProperty('--msg-font-size', settings.fontSize + 'px');
        }
    }

    function applyAll() { apply(); }

    return { load, save, get, set, applyAll, DEFAULTS };
})();

SettingsManager.load();
SettingsManager.applyAll();
