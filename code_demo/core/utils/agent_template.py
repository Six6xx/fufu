"""
code_demo/core/utils/agent_template.py — Agent 定义解析器

从 Markdown 前置元数据（frontmatter）中解析代理定义。
来源：19_mcp-plugin.py L2605–L2644 (AgentTemplate)
"""

import re
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class PlanItem:
    """计划条目。"""
    content: str  # 计划步骤内容
    status: str = "pending"  # 计划步骤当前状态


@dataclass
class CompactState:
    """上下文压缩状态。"""
    has_compacted: bool = False  # 是否已经完成上下文压缩
    last_summary: str = ""  # 最近一次压缩生成的摘要
    recent_files: list = field(default_factory=list)  # 最近访问的文件列表


class AgentTemplate:
    """
    从 Markdown 前置元数据（frontmatter）中解析代理定义。

    真实的 Claude Code 从 .claude/agents/*.md 文件加载代理定义。
    前置元数据字段包括：
    - name: 名称
    - tools: 工具列表
    - disallowedTools: 禁用工具
    - skills: 技能
    - hooks: 钩子
    - model: 模型
    - effort: 努力程度
    - permissionMode: 权限模式
    - maxTurns: 最大轮次
    - memory: 记忆
    - isolation: 隔离级别
    - color: 颜色
    - background: 后台运行
    - initialPrompt: 初始提示词
    - mcpServers: MCP 服务器
    """

    def __init__(self, path):
        self.path = Path(path)  # Agent 模板源文件路径
        self.name = self.path.stem  # 默认名称取自文件名
        self.config = {}            # frontmatter 配置
        self.system_prompt = ""     # 系统提示词
        self._parse()

    def _parse(self):
        """解析 frontmatter 和正文。"""
        text = self.path.read_text(encoding="utf-8")

        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            self.system_prompt = text
            return

        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                self.config[key.strip()] = value.strip()
        self.system_prompt = match.group(2).strip()
        self.name = self.config.get("name", self.name)

    def __repr__(self):
        return f"AgentTemplate(name={self.name!r}, config={self.config})"
