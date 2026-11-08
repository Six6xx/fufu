"""
code-demo/core/config.py — 全局配置常量

所有全局配置的单一入口点。
来源：19_mcp-plugin.py L62–L151, _extract_constants.py
"""

from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=True)

# === Core Directories & Paths ===
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_WS = _PROJECT_ROOT / "code_demo"
DEFAULT_WORKDIR = _DEFAULT_WS if _DEFAULT_WS.is_dir() else _PROJECT_ROOT
WORKDIR = DEFAULT_WORKDIR  # backward-compat alias
AI_CONFIG_DIR = Path(__file__).resolve().parent.parent / "ai_configuration"
COMPANION_PROFILE_FILE = AI_CONFIG_DIR / "companion_profile.json"
TRUST_MARKER = AI_CONFIG_DIR / ".trusted"
MEMORY_DIR = AI_CONFIG_DIR / ".memory"
TEAM_DIR = AI_CONFIG_DIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
REQUESTS_DIR = TEAM_DIR / "requests"
TASKS_DIR = AI_CONFIG_DIR / ".tasks"
CLAIM_EVENTS_PATH = TASKS_DIR / "claim_events.jsonl"
TRANSCRIPT_DIR = AI_CONFIG_DIR / ".transcripts"
TOOL_RESULTS_DIR = AI_CONFIG_DIR / ".task_outputs" / "tool-results"
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
INTERNAL_SKILLS_DIR = SKILLS_DIR / "__internal"
LEARNED_SKILLS_DIR = SKILLS_DIR / "__learned"
RETIRED_SKILLS_DIR = SKILLS_DIR / "__retired"
SESSIONS_DIR = AI_CONFIG_DIR / ".sessions"
MODEL_PROVIDERS_FILE = AI_CONFIG_DIR / "model_providers.json"
VOICE_PROVIDERS_FILE = AI_CONFIG_DIR / "voice_providers.json"
VLM_CONFIG_FILE = AI_CONFIG_DIR / "vlm_config.json"
MCP_CONFIG_FILE = _PROJECT_ROOT / "mcp_config.json"
GRAPH_DB_PATH = MEMORY_DIR / "unified_graph.db"

# === Model & Runtime ===
MODEL = os.getenv("MODEL")
MODEL_CHAT = os.getenv("MODEL_CHAT")
PLAN_REMINDER_INTERVAL = 3

# === Permissions & Hooks ===
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 60
MODES = ("default", "plan", "auto")
PERMISSION_MODES = ("default", "plan", "auto")
READ_ONLY_TOOLS = {"read_file", "bash_readonly"}
WRITE_TOOLS = {"write_file", "edit_file", "bash"}

# === Memory & Persistence ===
CONTEXT_LIMIT = 500000
KEEP_RECENT_TOOL_RESULTS = 5
KEEP_RECENT_MESSAGES = 30           # 压缩时保留的最近消息数
PERSIST_THRESHOLD = 30000
PREVIEW_CHARS = 2000
MAX_OUTPUT_CHARS = 500000           # 工具输出截断字符数
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ("user", "feedback", "project", "reference", "skill_learned", "mistake")
MAX_INDEX_LINES = 200

# === Task & Scheduling ===
STALL_THRESHOLD_S = 600
STALLED_TASK_CHECK_INTERVAL_S = 60  # explicitly exported alias for STALL_THRESHOLD_S
SCHEDULED_TASKS_FILE = AI_CONFIG_DIR / ".scheduled_tasks.json"
CRON_LOCK_FILE = AI_CONFIG_DIR / ".cron_lock"
AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = [0, 30]
JITTER_OFFSET_MAX = 4

# === Messaging & Protocol ===
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval",
    "plan_approval_response",
}

# === Companion Agent ===
COMPANION_MODEL = os.getenv("COMPANION_MODEL", "")  # 空则复用 agent.current_model
COMPANION_SHOW_AGENT_TEAM = False  # AgentTeam 链路默认可见性：隐藏
COMPANION_PERSONA_PROMPT = """你是用户正在设置的 AI 陪伴者。你的名字、用户称呼、兴趣和互动人格以用户明确提供的资料为准。
资料尚未设置时不要假定用户姓名、关系、喜好或陪伴者身份；先遵循首次设置技能，用自然、一次一组的问题完成设置。
表达真诚、自然、有边界感；不声称真实人类身份，不编造共同经历，不夸大能力。"""

# === TTS (CosyVoice3) ===
TTS_MODEL_PATH = os.getenv("TTS_MODEL_PATH", "")
TTS_PROMPT_WAV = os.getenv("TTS_PROMPT_WAV", "")
TTS_PROMPT_TEXT = os.getenv("TTS_PROMPT_TEXT", "You are a helpful assistant.<|endofprompt|>")

# === Server ===
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# === Tools ===
BASH_TIMEOUT = 120                  # bash 命令超时（秒）

# === Reasoning / Extended Thinking ===
ENABLE_REASONING = os.getenv("ENABLE_REASONING", "true").lower() == "true"  # 默认开启
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low")  # "low", "medium", "high"

# === Self-Evolution ===
SELF_CHECK_INTERVAL = 15             # AgentTeam 自检步数阈值
AUTO_SKILL_MIN_STEPS = 5             # 自动创建技能的最小工具调用步数

INTERNAL_LAYER_MAP = {
    "always": ["__persona", "__universal-rules"],
    "memory": ["__memory-guide", "__conversation-style"],
    "structure": ["__graph-guide", "__code-analysis"],
    "knowledge": ["__rag-guide"],
    "skill": ["__skill-guide"],
    "hybrid": ["__conversation-style"],
}

CONFLICT_THRESHOLD_KEYWORD = 0.6
CONFLICT_THRESHOLD_TOOL_SEQ = 0.7

# === Export list ===
__all__ = [
    "WORKDIR",
    "DEFAULT_WORKDIR",
    "AI_CONFIG_DIR",
    "MODEL",
    "MODEL_CHAT",
    "PLAN_REMINDER_INTERVAL",
    "HOOK_EVENTS",
    "HOOK_TIMEOUT",
    "MODES",
    "PERMISSION_MODES",
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
    "TRUST_MARKER",
    "MEMORY_DIR",
    "MEMORY_INDEX",
    "MEMORY_TYPES",
    "MAX_INDEX_LINES",
    "CONTEXT_LIMIT",
    "KEEP_RECENT_TOOL_RESULTS",
    "KEEP_RECENT_MESSAGES",
    "PERSIST_THRESHOLD",
    "PREVIEW_CHARS",
    "MAX_OUTPUT_CHARS",
    "STALL_THRESHOLD_S",
    "STALLED_TASK_CHECK_INTERVAL_S",
    "SCHEDULED_TASKS_FILE",
    "CRON_LOCK_FILE",
    "AUTO_EXPIRY_DAYS",
    "JITTER_MINUTES",
    "JITTER_OFFSET_MAX",
    "TEAM_DIR",
    "INBOX_DIR",
    "REQUESTS_DIR",
    "TASKS_DIR",
    "CLAIM_EVENTS_PATH",
    "TRANSCRIPT_DIR",
    "TOOL_RESULTS_DIR",
    "SKILLS_DIR",
    "INTERNAL_SKILLS_DIR",
    "LEARNED_SKILLS_DIR",
    "RETIRED_SKILLS_DIR",
    "SESSIONS_DIR",
    "MODEL_PROVIDERS_FILE",
    "VOICE_PROVIDERS_FILE",
    "VLM_CONFIG_FILE",
    "MCP_CONFIG_FILE",
    "GRAPH_DB_PATH",
    "VALID_MSG_TYPES",
    "BASH_TIMEOUT",
    "COMPANION_MODEL",
    "COMPANION_SHOW_AGENT_TEAM",
    "COMPANION_PERSONA_PROMPT",
    "TTS_MODEL_PATH",
    "TTS_PROMPT_WAV",
    "TTS_PROMPT_TEXT",
    "SERVER_HOST",
    "SERVER_PORT",
    "SELF_CHECK_INTERVAL",
    "AUTO_SKILL_MIN_STEPS",
    "INTERNAL_LAYER_MAP",
    "CONFLICT_THRESHOLD_KEYWORD",
    "CONFLICT_THRESHOLD_TOOL_SEQ",
    "ENABLE_REASONING",
    "REASONING_EFFORT",
    "EPISODIC_DIR",
    "EPISODIC_HOT_DURATION",
    "EPISODIC_TRASH_TTL",
    "EPISODIC_SKELETON_MISS_LIMIT",
    "EPISODIC_EMPHASIS_THRESHOLD",
    "EPISODIC_HOT_MAX_EVENTS",
    "EPISODIC_DECAY_CHECK_INTERVAL",
    "EPISODIC_CROSS_DATE_MERGE_DAYS",
]

# === 情景记忆（Episodic Memory）配置 ===
EPISODIC_DIR = AI_CONFIG_DIR / ".episodic"
EPISODIC_HOT_DURATION = 4 * 3600        # Hot 层保留时长：4小时
EPISODIC_TRASH_TTL = 3 * 24 * 3600      # Trash 层保留时长：3天
EPISODIC_SKELETON_MISS_LIMIT = 25       # 骨架层未命中阈值
EPISODIC_EMPHASIS_THRESHOLD = 2         # 升级 Core 所需强调次数
EPISODIC_HOT_MAX_EVENTS = 200           # Hot 层最大事件数
EPISODIC_DECAY_CHECK_INTERVAL = 300     # 衰减检查间隔：5分钟
EPISODIC_CROSS_DATE_MERGE_DAYS = 7      # 跨日期合并最大天数
