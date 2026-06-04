"""Session state persistence.

A *session* is a long-lived conversation with the AI.  When the user types
``/menu`` they bounce back to the suzu-admin TUI, but the conversation, the
workspace folder, and the project metadata stay on disk so they can pick up
exactly where they left off via the *Resume* menu entry.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


SESSION_VERSION = 1


@dataclass
class Session:
    id: str
    title: str
    created_at: float
    updated_at: float
    workspace: str
    model: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    profile: str = ""
    project: dict[str, Any] = field(default_factory=dict)
    version: int = SESSION_VERSION

    @classmethod
    def new(cls, sessions_dir: Path, workspaces_dir: Path, model: str, title: str = "") -> "Session":
        sid = _new_session_id()
        ws = workspaces_dir / sid
        ws.mkdir(parents=True, exist_ok=True)
        now = time.time()
        return cls(
            id=sid,
            title=title or f"Session {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}",
            created_at=now,
            updated_at=now,
            workspace=str(ws),
            model=model,
            messages=[],
            project={},
        )

    @classmethod
    def load(cls, sessions_dir: Path, sid: str) -> "Session":
        p = sessions_dir / f"{sid}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            workspace=data["workspace"],
            model=data.get("model", ""),
            messages=list(data.get("messages", [])),
            profile=str(data.get("profile", "")),
            project=dict(data.get("project", {})),
            version=int(data.get("version", SESSION_VERSION)),
        )

    def save(self, sessions_dir: Path) -> None:
        self.updated_at = time.time()
        p = sessions_dir / f"{self.id}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace)


# --------------------------------------------------------------------------- #


def list_sessions(sessions_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not sessions_dir.exists():
        return out
    for p in sorted(sessions_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(
            {
                "id": data.get("id", p.stem),
                "title": data.get("title", ""),
                "model": data.get("model", ""),
                "updated_at": float(data.get("updated_at", p.stat().st_mtime)),
                "messages": len(data.get("messages", [])),
                "project": data.get("project", {}),
                "workspace": data.get("workspace", ""),
            }
        )
    return out


def latest_session_id(sessions_dir: Path) -> str | None:
    items = list_sessions(sessions_dir)
    return items[0]["id"] if items else None


def delete_session(sessions_dir: Path, sid: str, workspaces_dir: Path) -> bool:
    p = sessions_dir / f"{sid}.json"
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    p.unlink(missing_ok=True)
    ws = Path(data.get("workspace", workspaces_dir / sid))
    if ws.is_dir() and ws.is_relative_to(workspaces_dir):
        # Best-effort remove.
        import shutil

        shutil.rmtree(ws, ignore_errors=True)
    return True


def _new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
