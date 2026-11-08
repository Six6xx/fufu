"""
code_demo/core/utils/security.py — Bash 命令安全性验证器

对 Bash 命令进行模式匹配，检测常见危险操作。
来源：19_mcp-plugin.py L2312–L2366 (BashSecurityValidator)
"""

import re


class BashSecurityValidator:
    """
    验证 Bash 命令是否存在明显危险的模式。

    先捕获少数高风险模式，再交由权限管道决定是直接拒绝还是询问用户。
    """

    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),          # Shell 元字符
        ("sudo", r"\bsudo\b"),                     # 权限提升
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),       # 递归删除
        ("cmd_substitution", r"\$\("),              # 命令替换
        ("ifs_injection", r"\bIFS\s*="),           # IFS 操纵
    ]

    def validate(self, command: str) -> list:
        """
        对 Bash 命令运行所有验证器进行检查。

        返回一个包含 (验证器名称, 匹配模式) 元组的列表，表示失败项。
        若返回空列表，则表示该命令通过了所有验证器。
        """
        failures = []
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))
        return failures

    def is_safe(self, command: str) -> bool:
        """仅当所有验证器均未触发时才返回 True。"""
        return len(self.validate(command)) == 0

    def describe_failures(self, command: str) -> str:
        """验证失败的摘要。"""
        failures = self.validate(command)
        if not failures:
            return "该命令未触发任何验证器。"
        parts = [f"{name} (pattern: {pattern})" for name, pattern in failures]
        return "该命令触发了以下验证器：\n" + "\n".join(parts)


# 模块级单例
bash_validator = BashSecurityValidator()
