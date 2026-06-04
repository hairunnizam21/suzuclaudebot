"""Programmatic, non-interactive agent driver.

The interactive REPL in :mod:`chat_ai.agent` is tightly coupled to terminal
output (animations, stdout writes).  Front-ends like the Telegram bot don't
want any of that — they want to send a user message, optionally observe the
intermediate tool calls, and receive the assistant's final text.

This module provides :func:`run_turn`, a small wrapper that drives one
streaming chat-completion + tool-call loop and emits structured events via an
``on_event`` callback so callers can render progress however they like.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .api import APIError, ChatClient, ChatRequest, DeltaAccumulator
from .config import Config
from .context import prune_messages
from .state import Session
from .tools import ToolContext, ToolRegistry

# Image uploads the model can "see" when sent as OpenAI-compatible image_url
# content blocks (data URLs). Anything bigger than this is referenced by path
# only — embedding multi-MB base64 in every request would blow the context.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

# When the model is still mid-task at the iteration cap, nudge it to keep going
# instead of leaving the work half-finished for the user to chase manually.
_CONTINUE_NUDGE = (
    "Teruskan tugas tadi sampai SIAP sepenuhnya tanpa berhenti bertanya. "
    "Kalau ini kerja APK, sambung pipeline sehingga recompile + zipalign + "
    "sign + verify, kemudian panggil `deliver` untuk fail akhir."
)


def _image_data_url(path: Path) -> str:
    mime = _IMAGE_MIME.get(path.suffix.lower(), "image/jpeg")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _build_user_content(text: str, images: list[Path]) -> Any:
    """Return either a plain string (no usable images) or a multimodal list."""
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for img in images:
        try:
            if img.stat().st_size > _MAX_IMAGE_BYTES:
                continue
            blocks.append(
                {"type": "image_url", "image_url": {"url": _image_data_url(img)}}
            )
        except OSError:
            continue
    if not any(b.get("type") == "image_url" for b in blocks):
        return text
    return blocks


def _collapse_images(message: dict[str, Any]) -> None:
    """Replace embedded base64 image blocks with a short text marker in place.

    Keeps session.json small and avoids re-sending megabytes of base64 on every
    later turn — the model has already seen the image during this turn.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return
    text = " ".join(
        b.get("text", "") for b in content if b.get("type") == "text"
    ).strip()
    n = sum(1 for b in content if b.get("type") == "image_url")
    marker = f"\n[{n} imej dilampirkan oleh pengguna]" if n else ""
    message["content"] = (text + marker).strip() or marker.strip()

# An event callback receives ``(kind, payload)`` tuples.  Kinds:
#   "thinking"      payload = {}                                  (first model call)
#   "text_delta"    payload = {"text": str}                       (assistant tokens)
#   "tool_start"    payload = {"name", "arguments", "call_id"}    (a tool is about to run)
#   "tool_end"      payload = {"name", "call_id", "output"}       (tool finished — output is the raw JSON string)
#   "iteration"     payload = {"index": int}                      (loop iteration boundary)
#   "done"          payload = {"text": str}                       (full assistant text for this turn)
#   "error"         payload = {"error": str}                      (API/transport error)
EventCb = Callable[[str, dict], None]


def run_turn(
    client: ChatClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    cfg: Config,
    session: Session,
    user_message: Optional[str] = None,
    on_event: Optional[EventCb] = None,
    images: Optional[list[str]] = None,
) -> str:
    """Drive one user→assistant exchange end-to-end.

    Appends ``user_message`` (if provided) to ``session.messages``, runs the
    streaming model loop up to ``cfg.max_tool_iters`` iterations, executes any
    tool calls inline, and returns the final assistant text.  The session is
    persisted after every model + tool step.

    ``images`` is an optional list of image file paths (screenshots/photos the
    user uploaded). Usable images are embedded as OpenAI-compatible
    ``image_url`` blocks so the model can actually see them. If the model can't
    handle images the call falls back to text-only automatically.

    If the model is still calling tools when it reaches ``cfg.max_tool_iters``,
    it is auto-nudged to keep going (up to ``cfg.auto_continue_rounds`` times)
    so long APK pipelines finish without the user having to type "continue".
    """

    def _emit(kind: str, payload: dict) -> None:
        if on_event:
            try:
                on_event(kind, payload)
            except Exception:  # noqa: BLE001 — front-ends must not break the loop
                pass

    img_paths = [
        Path(p) for p in (images or [])
        if Path(p).suffix.lower() in _IMAGE_EXTS and Path(p).is_file()
    ]

    # The user message object we keep a handle on so we can strip embedded
    # images from it once the turn is over (and on a non-vision fallback).
    user_msg_obj: Optional[dict[str, Any]] = None
    if user_message is not None:
        user_msg_obj = {
            "role": "user",
            "content": _build_user_content(user_message, img_paths),
        }
        session.messages.append(user_msg_obj)
        session.save(cfg.sessions_dir)

    tools = registry.schemas()
    final_text_parts: list[str] = []
    img_stripped = [not isinstance((user_msg_obj or {}).get("content"), list)]

    def _drive(max_iters: int) -> bool:
        """Run up to ``max_iters`` model+tool steps. Return True if the model
        finished naturally (no pending tool calls); False if it hit the cap."""
        for iteration in range(max_iters):
            _emit("iteration", {"index": iteration})
            _emit("thinking", {})

            req = ChatRequest(
                model=session.model,
                messages=prune_messages(
                    session.messages,
                    char_budget=cfg.context_char_budget,
                    tool_result_char_cap=cfg.tool_result_char_cap,
                ),
                tools=tools,
                stream=True,
                max_tokens=cfg.max_tokens or None,
            )
            accum = DeltaAccumulator()
            try:
                for event in client.stream(req):
                    added = accum.push(event)
                    if added:
                        _emit("text_delta", {"text": added})
            except APIError as e:
                # Most likely cause when images are attached: the selected model
                # is not vision-capable. Strip the image(s) and retry text-only
                # once instead of failing the whole turn.
                if user_msg_obj is not None and not img_stripped[0]:
                    _collapse_images(user_msg_obj)
                    img_stripped[0] = True
                    session.save(cfg.sessions_dir)
                    _emit("error", {
                        "error": "model tak sokong imej — cuba teks sahaja",
                    })
                    continue
                _emit("error", {"error": str(e)})
                return True

            assistant_msg = accum.to_message()
            if assistant_msg.get("content") is None:
                assistant_msg["content"] = ""
            session.messages.append(assistant_msg)
            session.save(cfg.sessions_dir)

            if assistant_msg.get("content"):
                final_text_parts.append(assistant_msg["content"])

            if not accum.tool_calls:
                return True

            for tc in accum.tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "") or ""
                raw_args = fn.get("arguments", "") or "{}"
                call_id = tc.get("id") or f"call_{int(time.time() * 1000)}"
                _emit(
                    "tool_start",
                    {"name": name, "arguments": raw_args, "call_id": call_id},
                )
                output = registry.invoke(name, raw_args, ctx)
                _emit(
                    "tool_end",
                    {"name": name, "call_id": call_id, "output": output},
                )
                session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": output,
                    }
                )
                session.save(cfg.sessions_dir)
            # loop again — feed tool results back to the model.
        return False

    try:
        nudges_left = max(0, cfg.auto_continue_rounds)
        while True:
            finished = _drive(cfg.max_tool_iters)
            if finished or nudges_left <= 0:
                break
            # Hit the cap while still working — auto-continue instead of
            # leaving the job half-done.
            nudges_left -= 1
            _emit("iteration", {"index": -1})
            session.messages.append({"role": "user", "content": _CONTINUE_NUDGE})
            session.save(cfg.sessions_dir)
    finally:
        if user_msg_obj is not None:
            _collapse_images(user_msg_obj)
            session.save(cfg.sessions_dir)

    text = "".join(final_text_parts)
    _emit("done", {"text": text})
    return text


def summarize_tool_output(name: str, raw: str, limit: int = 200) -> str:
    """Compact one-liner for a tool result (suitable for chat status lines)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _shorten(raw, limit)
    if isinstance(data, dict):
        if data.get("error"):
            return f"error: {_shorten(str(data['error']), limit)}"
        if "exit_code" in data:
            ec = data["exit_code"]
            out = (data.get("stdout") or "").strip().splitlines()
            head = out[0] if out else ""
            return f"exit={ec}" + (f" — {_shorten(head, limit)}" if head else "")
        if "matches" in data:
            return f"{len(data['matches'])} match(es)"
        if "entries" in data:
            return f"{len(data['entries'])} entries"
        if "frameworks" in data:
            extra = f" • abis={','.join(data.get('abis', []))}" if data.get("abis") else ""
            return ", ".join(data["frameworks"]) + extra
        if "path" in data:
            return str(data["path"])
    return _shorten(json.dumps(data, ensure_ascii=False), limit)


def _shorten(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
