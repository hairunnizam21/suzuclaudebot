"""Per-user long-term memory that persists across sessions.

Memory notes survive `/new` (session reset) and bot restarts, giving the AI
context about the user's projects and preferences regardless of which session
is active.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MemoryNote:
    text: str
    ts: float = field(default_factory=time.time)
    source: str = "user"  # "user" or "ai"


class MemoryStore:
    """Flat-file per-user memory: ``<memory_dir>/<chat_id>.json``."""

    def __init__(self, memory_dir: Path, max_notes: int = 50) -> None:
        self._dir = memory_dir
        self._max = max_notes
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id: int) -> Path:
        return self._dir / f"{chat_id}.json"

    def _load(self, chat_id: int) -> list[dict[str, Any]]:
        p = self._path(chat_id)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return list(data.get("notes", []))
        except (OSError, json.JSONDecodeError, ValueError):
            return []

    def _save(self, chat_id: int, notes: list[dict[str, Any]]) -> None:
        p = self._path(chat_id)
        tmp = p.with_suffix(".json.tmp")
        payload = {"notes": notes[-self._max:], "updated_at": time.time()}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        import os
        os.replace(tmp, p)

    def add(self, chat_id: int, text: str, source: str = "user") -> int:
        notes = self._load(chat_id)
        notes.append({"text": text.strip(), "ts": time.time(), "source": source})
        self._save(chat_id, notes)
        return len(notes)

    def list_notes(self, chat_id: int) -> list[dict[str, Any]]:
        return self._load(chat_id)

    def remove(self, chat_id: int, index: int) -> bool:
        notes = self._load(chat_id)
        if 0 <= index < len(notes):
            notes.pop(index)
            self._save(chat_id, notes)
            return True
        return False

    def clear(self, chat_id: int) -> int:
        notes = self._load(chat_id)
        count = len(notes)
        self._save(chat_id, [])
        return count

    def render_block(self, chat_id: int, char_budget: int = 4000) -> str:
        """Format memory notes for injection into the system prompt.

        Returns empty string if the user has no memory notes.
        """
        notes = self._load(chat_id)
        if not notes:
            return ""
        lines = [
            "\n\n## Long-term memory (ingatan pengguna ini)",
            "Nota-nota di bawah kekal merentasi sesi. Guna maklumat ini untuk "
            "beri konteks yang lebih baik. Jangan ulang balik nota-nota ini "
            "kepada pengguna kecuali mereka tanya.",
        ]
        total = 0
        for i, n in enumerate(notes):
            entry = f"{i+1}. [{n.get('source','?')}] {n['text']}"
            if total + len(entry) > char_budget:
                lines.append(f"… ({len(notes) - i} lagi nota dipotong)")
                break
            lines.append(entry)
            total += len(entry)
        return "\n".join(lines)
