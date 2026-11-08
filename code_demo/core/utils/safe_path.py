"""
code-demo/core/utils/safe_path.py — 安全路径解析
来源：19_mcp-plugin.py L2658–L2664
"""

from pathlib import Path
from code_demo.core.config import WORKDIR


def safe_path(p: str, workdir: Path | None = None) -> Path:
    """将路径字符串转换为绝对路径，确保在工作目录内。

    Args:
        p: 待解析的路径（相对或绝对）
        workdir: 工作目录基准。为 None 时使用模块级 WORKDIR

    增强安全检查：
    1. 两侧路径都调用 resolve() 防止符号链接绕过
    2. 检查路径每一层是否包含指向外部的符号链接
    """
    base = (workdir or WORKDIR).resolve()
    path = (base / p).resolve()

    # 基础检查：解析后路径必须在工作目录内
    if not path.is_relative_to(base):
        raise ValueError(f"路径 {p} 不在工作目录内")

    # 增强检查：遍历原始路径每一段，拦截指向外部的符号链接
    check_path = workdir or WORKDIR
    for part in Path(p).parts:
        check_path = check_path / part
        if check_path.is_symlink():
            target = check_path.resolve()
            if not target.is_relative_to(base):
                raise ValueError(
                    f"路径 {p} 包含指向工作目录外部的符号链接: {check_path} -> {target}"
                )

    return path