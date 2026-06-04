"""Runtime configuration for the Suzu Chat AI.

Values are sourced from environment variables (which suzu-admin loads from
``/var/www/suzu-ai-web/.env`` or wherever ``SUZU_ENV_FILE`` points).  Sensible
defaults are provided so the CLI also works in a developer checkout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


@dataclass(frozen=True)
class Config:
    api_base_url: str
    api_key: str
    default_model: str
    state_dir: Path
    sessions_dir: Path
    workspaces_dir: Path
    log_dir: Path
    request_timeout: float
    max_tool_iters: int
    auto_continue_rounds: int
    show_tool_io: bool
    auto_approve_shell: bool
    debug: bool
    max_tokens: int
    context_char_budget: int
    tool_result_char_cap: int

    @classmethod
    def load(cls) -> "Config":
        env_file = _env("SUZU_ENV_FILE")
        if env_file and Path(env_file).is_file():
            _load_env_file(Path(env_file))

        base = Path(_env("SUZU_STATE_DIR", "/var/lib/suzu-ai"))
        sessions = base / "sessions"
        workspaces = base / "workspaces"
        logs = base / "logs"

        for d in (sessions, workspaces, logs):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                # Fallback to user-local state (developer mode).
                base = Path.home() / ".local" / "share" / "suzu-ai"
                sessions = base / "sessions"
                workspaces = base / "workspaces"
                logs = base / "logs"
                for dd in (sessions, workspaces, logs):
                    dd.mkdir(parents=True, exist_ok=True)
                break

        return cls(
            api_base_url=_env("AI_API_BASE_URL", "https://core.fiqstr.com/v1").rstrip("/"),
            api_key=_env("AI_API_KEY", ""),
            default_model=_env("AI_DEFAULT_MODEL", "fiqstr/claude-sonnet-4.6-thinking-agentic"),
            state_dir=base,
            sessions_dir=sessions,
            workspaces_dir=workspaces,
            log_dir=logs,
            request_timeout=float(_env("SUZU_REQUEST_TIMEOUT", "600")),
            max_tool_iters=int(_env("SUZU_MAX_TOOL_ITERS", "60")),
            # When the model is still working at the iteration cap, auto-nudge it
            # to continue this many extra rounds so long APK pipelines finish
            # without the user having to type "continue".
            auto_continue_rounds=int(_env("SUZU_AUTO_CONTINUE_ROUNDS", "2")),
            show_tool_io=_env("SUZU_SHOW_TOOL_IO", "1") not in ("0", "false", "False"),
            auto_approve_shell=_env("SUZU_AUTO_APPROVE_SHELL", "1") not in ("0", "false", "False"),
            debug=_env("SUZU_DEBUG", "0") not in ("0", "false", "False"),
            # Upper bound on tokens the model may generate per reply. 0/empty
            # disables the cap (let the server decide).
            max_tokens=int(_env("SUZU_MAX_TOKENS", "8192") or 0),
            # Approximate character budget for the conversation we resend on
            # every model call. The full transcript is still kept on disk; we
            # only trim what we *send* so latency stays bounded as history grows.
            context_char_budget=int(_env("SUZU_CONTEXT_CHAR_BUDGET", "48000") or 0),
            # Hard cap applied to each individual tool-result message before it
            # is sent back to the model. Large dumps (decompile/aapt/strings)
            # are truncated here; the user still receives full files.
            tool_result_char_cap=int(_env("SUZU_TOOL_RESULT_CHAR_CAP", "8000") or 0),
        )


def _load_env_file(path: Path) -> None:
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass
