"""Tool registry for the Suzu Chat AI.

Each tool is a ``Tool`` dataclass with an OpenAI-style ``schema`` (JSON Schema)
describing its parameters and a ``handler`` callable that executes it.  The
agent loop converts every assistant ``tool_calls`` entry into a handler call
and feeds the result back as a ``tool``-role message.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import apk, analysis, files, python_env, shell


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], "ToolContext"], dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    workspace: str
    debug: bool = False
    # Files the assistant has explicitly chosen to hand back to the user (e.g.
    # the final signed APK).  Front-ends (the Telegram bot) read this after a
    # turn and upload *only* these, instead of echoing every intermediate file.
    deliverables: list[str] = field(default_factory=list)
    # Long-term memory store + chat_id for the `remember` tool.
    memory_store: Any = None
    chat_id: int = 0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def invoke(self, name: str, raw_args: str, ctx: ToolContext) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                args = {"value": args}
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"invalid JSON arguments: {e}", "raw": raw_args[:400]})
        try:
            result = tool.handler(args, ctx)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc() if ctx.debug else ""
            result = {"error": f"{type(e).__name__}: {e}", "trace": tb}
        if not isinstance(result, dict):
            result = {"result": result}
        return _safe_json(result)


def _safe_json(obj: Any, max_chars: int = 60_000) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=_default)
    if len(s) > max_chars:
        truncated = obj.copy() if isinstance(obj, dict) else {"result": obj}
        truncated["_truncated"] = True
        truncated["_note"] = (
            f"output trimmed from {len(s)} to {max_chars} chars — "
            "save large outputs to files and read them back"
        )
        # Trim any stdout/stderr-ish fields.
        for key in ("stdout", "stderr", "content", "preview"):
            if key in truncated and isinstance(truncated[key], str):
                truncated[key] = truncated[key][: max_chars // 2] + "\n…[trimmed]…"
        s = json.dumps(truncated, ensure_ascii=False, default=_default)
        if len(s) > max_chars:
            s = s[:max_chars]
    return s


def _default(o: Any) -> Any:
    try:
        return str(o)
    except Exception:
        return repr(o)


def _deliver(args: dict[str, Any], ctx: "ToolContext") -> dict[str, Any]:
    """Hand a finished file back to the user (the only files a front-end sends)."""
    path = args.get("path")
    if not path:
        return {"error": "`path` is required"}
    p = Path(path)
    if not p.is_absolute():
        p = Path(ctx.workspace) / p
    if not p.exists() or not p.is_file():
        return {"error": f"file not found: {p}"}
    resolved = str(p)
    if resolved not in ctx.deliverables:
        ctx.deliverables.append(resolved)
    return {
        "delivered": resolved,
        "size": p.stat().st_size,
        "note": "Queued to send to the user as a final deliverable.",
    }


def _remember(args: dict[str, Any], ctx: "ToolContext") -> dict[str, Any]:
    """Save a note to the user's long-term memory (persists across sessions)."""
    text = (args.get("note") or "").strip()
    if not text:
        return {"error": "`note` is required"}
    if ctx.memory_store is None or ctx.chat_id == 0:
        return {"error": "memory not available"}
    count = ctx.memory_store.add(ctx.chat_id, text, source="ai")
    return {"saved": True, "total_notes": count}


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    shell.register(reg, Tool)
    files.register(reg, Tool)
    apk.register(reg, Tool)
    analysis.register(reg, Tool)
    python_env.register(reg, Tool)
    reg.register(
        Tool(
            name="deliver",
            description=(
                "Send a FINISHED file to the user (e.g. the final signed/aligned APK, "
                "an AAB, or a packaged zip of the project). This is the ONLY way a file "
                "reaches the user — intermediate artefacts (decompiled smali/java, "
                "resources, modified images, class files) are NOT sent automatically. "
                "Call this once at the end with the final deliverable(s)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the finished file (absolute, or relative to the workspace).",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional short caption shown with the file.",
                    },
                },
                "required": ["path"],
            },
            handler=_deliver,
        )
    )
    reg.register(
        Tool(
            name="remember",
            description=(
                "Save an important note to the user's LONG-TERM memory. "
                "These notes persist across sessions and are always visible to you. "
                "Use this to record: project names, package IDs, common errors and "
                "their fixes, user preferences, or key decisions. Keep notes concise."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "The note to remember (concise, factual).",
                    },
                },
                "required": ["note"],
            },
            handler=_remember,
        )
    )
    return reg
