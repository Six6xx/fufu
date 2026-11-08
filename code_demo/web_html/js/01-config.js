/* 01-config.js — 常量 + 全局状态 + DOM 引用 */
/**
 * 芙芙 - 前端逻辑
 *
 * 整体架构：
 *   前端通过 WebSocket 与后端实时通信，支持以下功能：
 *   1. 流式文本显示 — LLM 生成的 token 通过 stream_content 消息逐字推送，
 *      前端动态更新消息气泡内容，实现打字机效果
 *   2. 思考模式 — 可选开启，开启后显示可折叠的"💭 思考过程"块，
 *      通过 stream_reasoning 消息流式展示模型的推理链
 *   3. 流式 TTS — 后端按句子分段合成语音，通过 stream_audio 推送音频片段，
 *      前端使用音频队列顺序播放，实现边出文字边出声音
 *   4. ASR 实时语音识别 — MediaRecorder 录音，每 3 秒发送音频块
 *   5. TTS 语音合成 — 后端 CosyVoice3 模型合成，支持语速控制
 *
 * WebSocket 消息格式：
 *   Client → Server: { type: 'text'|'audio'|'toggle_asr'|'toggle_tts', content, ...extra }
 *   Server → Client: { type: 'stream_content'|'stream_reasoning'|'stream_end'|
 *                           'stream_audio'|'audio'|'asr_partial'|'asr_final'|'asr_auto_send'|'status'|'error', content }
 */

// ── 配置 ──
const WS_RECONNECT_DELAY = 3000;    // WebSocket 重连延迟 (ms)
const SILENCE_THRESHOLD = 1500;      // VAD 静音阈值 (ms)

// ── 状态 ──
const state = {
    ws: null,
    isConnected: false,
    asrEnabled: false,
    ttsEnabled: false,
    thinkingEnabled: false,
    isRecording: false,
    mediaRecorder: null,
    audioChunks: [],
    currentAudio: null,
    silenceTimer: null,
    asrLockedText: '',           // 已确认的句子（稳定，累积）
    asrPartialText: '',          // 当前正在识别的句子（频繁变化）
    asrAutoSendTimer: null,      // 自动发送定时器
    asrLastSentText: '',         // 上次发送给 Agent 的文本（去重）
    asrLastSentTime: 0,          // 上次发送音频的时间戳
    asrEditing: false,           // 是否处于 ASR 编辑模式
    asrEditBackup: null,         // 编辑模式下保存的原始 div 元素引用
    // 流式输出状态
    streamActive: false,
    streamEl: null,          // 当前流式消息元素
    streamContent: '',       // 累积的正文内容
    thinkingEl: null,        // 思考块元素
    thinkingContent: '',     // 累积的思考内容
    // 任务进度状态
    taskRunning: false,          // 是否有后台任务正在运行
    // 伴侣对话流式状态
    companionStreamActive: false,
    companionStreamEl: null,
    companionStreamContent: '',
    // 工具调用状态
    currentToolBlock: null,  // 当前正在构建的工具块
    lastToolName: null,      // 上一个工具名称（用于合并连续相同工具）
    // 会话管理状态
    sessionId: null,         // 当前会话 ID
    sessionName: '新会话',    // 当前会话名称（完整）
    sessions: [],            // 会话列表
    sidebarOpen: false,      // 侧边栏是否打开
    nameCustomized: false,   // 用户是否自定义了会话名称
    // MCP 状态
    mcpPopupOpen: false,     // MCP 弹出面板是否打开
    mcpServers: [],          // [{name, type, connected, disabled, tool_count, error}]
    mcpModalType: 'remote',  // 模态窗口类型选择: 'remote' | 'local'
    mcpExpanded: null,       // 当前展开工具列表的服务器名（同时只展开一个）
    mcpTools: {},            // {server_name: [{name, description, enabled}]}
    // 设置页面
    settingsOpen: false,
    settingsCategory: 'general',  // 'general' | 'mcp' | 'skills' | 'providers'
    settingsSkills: [],       // [{name, description, body}]
    skillEditOldName: null,   // 编辑模式下保存原名称（null=新增模式）
    // 删除目标（确认对话框用）
    deleteTarget: null,       // {type: 'mcp'|'skill', name: 'xxx'}
    // 批量选择
    batchMode: false,
    batchCategory: null,      // 'mcp' | 'skills'
    batchSelected: [],        // [name, ...] 按选择顺序排列
    // 管理模式（Skills 专用）
    manageMode: false,       // 是否处于管理模式
    // Skills 文件夹视图模式
    skillsViewMode: 'main',  // 'main' | 'auto_folder'
    // 情绪与 Live2D
    emotion: null,            // 当前情绪状态
    live2dReady: false,       // Live2D 是否加载完成
    // 工作空间
    workspace: '',            // 当前工作空间路径
    defaultWorkspace: '',     // 服务器默认工作空间
    // 模型供应商
    providerTab: 'llm',       // 供应商页子标签: 'llm' | 'tts' | 'asr'
    currentModel: '',         // 当前模型名称
    providerId: null,         // 当前供应商 ID
    providerName: '',         // 当前供应商名称
    providerModels: [],       // 当前供应商模型列表 [{name, type}]
};

// ── DOM 元素 ──
const $ = (id) => document.getElementById(id);
const messagesEl = $('messages');
const textInput = $('textInput');
const sendBtn = $('sendBtn');
const micBtn = $('micBtn');
const ttsBtn = $('ttsBtn');
const asrIndicator = $('asrIndicator');
const ttsIndicator = $('ttsIndicator');
const mcpBtn = $('mcpBtn');
const mcpIndicator = $('mcpIndicator');
const mcpPopup = $('mcpPopup');
const mcpList = $('mcpList');
const mcpCount = $('mcpCount');
const mcpReady = !!(mcpBtn && mcpPopup && mcpList && mcpCount);
const mcpModal = $('mcpModal');
const mcpNameInput = $('mcpNameInput');
const mcpUrlInput = $('mcpUrlInput');
const mcpHeadersInput = $('mcpHeadersInput');
const mcpCommandInput = $('mcpCommandInput');
const mcpEnvInput = $('mcpEnvInput');
const mcpJsonInput = $('mcpJsonInput');
const statusBar = $('statusBar');
const connectionStatus = $('connectionStatus');
const asrPopup = $('asrPopup');
const asrPopupClose = $('asrPopupClose');
let asrLiveText = $('asrLiveText');
const asrSendBtn = $('asrSendBtn');
const asrEditBtn = $('asrEditBtn');
const asrCancelBtn = $('asrCancelBtn');
const thinkingToggle = $('thinkingToggle');
const sidebar = $('sidebar');
const sessionList = $('sessionList');
const newSessionBtn = $('newSessionBtn');
const hamburgerBtn = $('hamburgerBtn');
const sessionNameEl = $('sessionName');
const settingsBtn = $('settingsBtn');
const settingsOverlay = $('settingsOverlay');
const settingsClose = $('settingsClose');
const settingsNav = $('settingsNav');
const settingsContent = $('settingsContent');
const addSkillBtn = $('addSkillBtn');
const importSkillBtn = $('importSkillBtn');
const skillActionBtns = $('skillActionBtns');
const mcpActionBtns = $('mcpActionBtns');
const addMcpFromSettingsBtn = $('addMcpFromSettingsBtn');
const skillModal = $('skillModal');
const skillImportModal = $('skillImportModal');
const skillNameInput = $('skillNameInput');
const skillDescInput = $('skillDescInput');
const skillBodyInput = $('skillBodyInput');
const skillFileInput = $('skillFileInput');
const skillFilePickBtn = $('skillFilePickBtn');
const skillFileName = $('skillFileName');
const skillImportContent = $('skillImportContent');
const skillImportError = $('skillImportError');
const skillImportErrorMsg = $('skillImportErrorMsg');
const skillImportErrorExample = $('skillImportErrorExample');
const skillModalDelete = $('skillModalDelete');
const mcpDetailModal = $('mcpDetailModal');
const mcpDetailName = $('mcpDetailName');
const mcpDetailType = $('mcpDetailType');
const mcpDetailStatus = $('mcpDetailStatus');
const mcpDetailTools = $('mcpDetailTools');
const deleteConfirmModal = $('deleteConfirmModal');
const deleteConfirmText = $('deleteConfirmText');
const batchBar = $('batchBar');
const batchDeleteBtn = $('batchDeleteBtn');
const batchRetireBtn = $('batchRetireBtn');
const batchRight = $('batchRight');
const batchCancelBtn = $('batchCancelBtn');
const batchConfirmBtn = $('batchConfirmBtn');
const manageSkillsBar = $('manageSkillsBar');
const manageSkillsBtn = $('manageSkillsBtn');
const batchModal = $('batchModal');
const batchModalText = $('batchModalText');
const batchModalList = $('batchModalList');
const batchModalToggle = $('batchModalToggle');
const pendingFilesEl = $('pendingFiles');
const uploadBtn = $('uploadBtn');
const uploadTooltip = $('uploadTooltip');
const fileInput = $('fileInput');
const filePreviewOverlay = $('filePreviewOverlay');
const filePreviewTitle = $('filePreviewTitle');
const filePreviewBody = $('filePreviewBody');
const workspaceInput = $('workspaceInput');
const workspaceApplyBtn = $('workspaceApplyBtn');
const workspaceResetBtn = $('workspaceResetBtn');
const workspaceBrowseBtn = $('workspaceBrowseBtn');
const dirPickerOverlay = $('dirPickerOverlay');
const dirPickerClose = $('dirPickerClose');
const dirPickerCancel = $('dirPickerCancel');
const dirPickerConfirm = $('dirPickerConfirm');
const dirPickerPathInput = $('dirPickerPathInput');
const dirPickerBody = $('dirPickerBody');
const modelBarBtn = $('modelBarBtn');
const modelBarName = $('modelBarName');
const modelBarProvider = $('modelBarProvider');
const modelBarArrow = $('modelBarArrow');
const modelDropdown = $('modelDropdown');

