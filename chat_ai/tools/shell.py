"""Generic shell command execution."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

# Default timeout for ad-hoc shell/exec commands. apktool / gradle / flutter
# builds (especially the first run that downloads dependencies) routinely take
# several minutes, so 180s was killing real builds half-way. Configurable via
# SUZU_SHELL_TIMEOUT. The dedicated apk_* / build_project tools use their own
# longer timeouts.
_DEFAULT_SHELL_TIMEOUT = float(os.environ.get("SUZU_SHELL_TIMEOUT", "600"))


def _resolve_cwd(cwd: str | None, workspace: str) -> str:
    if not cwd:
        return workspace
    p = Path(cwd)
    if not p.is_absolute():
        p = Path(workspace) / p
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _run_shell(args: dict[str, Any], ctx) -> dict[str, Any]:
    command = args.get("command")
    if not command or not isinstance(command, str):
        return {"error": "`command` (string) is required"}
    cwd = _resolve_cwd(args.get("cwd"), ctx.workspace)
    timeout = float(args.get("timeout") or _DEFAULT_SHELL_TIMEOUT)
    env = os.environ.copy()
    if isinstance(args.get("env"), dict):
        for k, v in args["env"].items():
            env[str(k)] = str(v)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "error": f"timeout after {timeout}s",
            "stdout": (e.stdout or "")[-8000:] if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "")[-8000:] if isinstance(e.stderr, str) else "",
            "command": command,
            "cwd": cwd,
        }
    out = proc.stdout or ""
    err = proc.stderr or ""
    # Trim very large output; agent can read files for full text.
    max_chars = 20_000
    return {
        "command": command,
        "cwd": cwd,
        "exit_code": proc.returncode,
        "stdout": out if len(out) <= max_chars else out[-max_chars:] + "\n…[trimmed head]…",
        "stderr": err if len(err) <= max_chars else err[-max_chars:] + "\n…[trimmed head]…",
        "truncated": len(out) > max_chars or len(err) > max_chars,
    }


def _run_argv(args: dict[str, Any], ctx) -> dict[str, Any]:
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv:
        return {"error": "`argv` (list[str]) is required"}
    argv = [str(x) for x in argv]
    cwd = _resolve_cwd(args.get("cwd"), ctx.workspace)
    timeout = float(args.get("timeout") or _DEFAULT_SHELL_TIMEOUT)
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        return {"error": f"command not found: {argv[0]}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {
            "error": f"timeout after {timeout}s",
            "stdout": (e.stdout or "")[-8000:] if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "")[-8000:] if isinstance(e.stderr, str) else "",
            "argv": argv,
        }
    return {
        "argv": argv,
        "cwd": cwd,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-20_000:],
        "stderr": (proc.stderr or "")[-20_000:],
    }


def register(reg, Tool) -> None:
    reg.register(
        Tool(
            name="shell",
            description=(
                "Run a shell command (bash -c). Use for general-purpose CLI tasks. "
                "Working directory defaults to the session workspace. "
                "Returns stdout/stderr/exit_code; large output is trimmed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command line, run via bash -c."},
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (relative paths are resolved under the workspace).",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default 600). Pass a larger value for slow gradle/flutter builds.",
                    },
                    "env": {
                        "type": "object",
                        "description": "Extra environment variables.",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["command"],
            },
            handler=_run_shell,
        )
    )

    reg.register(
        Tool(
            name="exec",
            description=(
                "Run a process directly via execve (no shell interpolation). "
                "Use this when you want to avoid shell quoting issues."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Argument vector, e.g. [\"apktool\", \"d\", \"foo.apk\"].",
                    },
                    "cwd": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["argv"],
            },
            handler=_run_argv,
        )
    )
