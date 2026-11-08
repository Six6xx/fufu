"""Run shell commands with the platform's interactive shell semantics."""

import os
import shutil
import subprocess


def run_shell_command(command: str, **kwargs) -> subprocess.CompletedProcess:
    """Run a command in bash on POSIX or PowerShell on Windows.

    The returned CompletedProcess preserves the normal ``subprocess.run``
    contract (including timeout, cwd, env, capture_output, and text options).
    """
    if os.name != "nt":
        return subprocess.run(command, shell=True, **kwargs)

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell:
        utf8 = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); $OutputEncoding = [Console]::OutputEncoding; "
        argv = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8 + command]
        if kwargs.get("text") and "encoding" not in kwargs:
            kwargs["encoding"] = "utf-8"
            kwargs.setdefault("errors", "replace")
        return subprocess.run(argv, shell=False, **kwargs)

    # PowerShell is included with supported Windows installations, but retain
    # a cmd.exe fallback for minimal environments.
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    return subprocess.run([comspec, "/d", "/s", "/c", command], shell=False, **kwargs)
