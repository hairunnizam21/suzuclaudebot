"""Telegram front-end for the Suzu Chat AI.

Stdlib-only, long-polling Bot API client.  Reuses the same ``chat_ai`` agent
loop and the 31 tools available to the terminal CLI — so anything the user
can do in ``suzu-chat-ai`` they can also do over Telegram: chat, build APK,
decompile / recompile, analyse uploaded files, run reverse-engineering
tools, write Python scripts, and so on.

Highlights
----------
* **One session per Telegram chat.**  State is persisted to
  ``$SUZU_STATE_DIR/sessions`` exactly like the terminal CLI — so a
  conversation survives bot restarts and is shared across the two
  front-ends if you choose to point ``/resume`` at the same id.
* **Allowlist.**  ``TELEGRAM_ALLOWED_USER_IDS`` is a comma-separated list of
  Telegram user ids that may interact with the bot.  Anyone else gets a
  polite "not authorized" reply.
* **File uploads.**  Documents / photos sent to the bot are downloaded into
  the active session's workspace and announced to the model so it can
  immediately reason about them ("here is an APK, please detect framework
  and decompile").
* **Live progress.**  While the agent is thinking / calling tools, the bot
  edits a status message ("Thinking…", "Running apk_decompile…") and
  drives a ``typing`` chat action so the UI stays responsive.

Environment
-----------
* ``TELEGRAM_BOT_TOKEN``       — required, from @BotFather.
* ``TELEGRAM_ALLOWED_USER_IDS``— required, comma-separated ids.
* ``TELEGRAM_BOT_USERNAME``    — optional, used in the welcome banner.
* All ``chat_ai`` env vars (``AI_API_KEY``, ``AI_API_BASE_URL``,
  ``AI_DEFAULT_MODEL``, ``SUZU_STATE_DIR``, …).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .api import APIError, ChatClient
from .config import Config
from .context import strip_hallucinated_protocols
from .prompts import render_system_prompt
from .runner import run_turn, summarize_tool_output
from .models_registry import ModelProfile, ModelRegistry
from .state import Session, list_sessions
from .tools import ToolContext, build_default_registry

log = logging.getLogger("suzu.telegram")

# Telegram message bodies are capped at 4096 chars.  Leave a small margin for
# the bot-controlled prefix we attach to chunks (" (1/3)" etc.).
MAX_MSG_CHARS = 3900

# How often the typing indicator must be refreshed.  Telegram clears it after
# ~5 s of silence, so we ping it every 4 s while the agent is busy.
TYPING_REFRESH_S = 4.0

# Maximum file size we attempt to download from a user upload (Telegram Bot API
# caps this at 20 MB for ``getFile`` and 50 MB for uploads — we err on the
# safe side and let the model deal with anything over the limit).
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

# Hard limit imposed by Telegram's *cloud* Bot API: getFile / file download
# only works for files up to 20 MB. Anything bigger fails server-side with
# "file is too big", so we detect it early and tell the user.
TELEGRAM_GETFILE_LIMIT = 20 * 1024 * 1024

# Default poll timeout sent to ``getUpdates``.  Telegram supports up to 50 s.
POLL_TIMEOUT_S = 30

# Frames for the animated status "card" we keep editing while a turn runs.
_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Friendly, user-facing phase labels per tool (keeps status professional
# instead of leaking raw tool names / output).
_PHASE_LABELS = {
    "shell": "Menjalankan arahan",
    "python": "Menjalankan skrip Python",
    "run_python": "Menjalankan skrip Python",
    "read_file": "Membaca fail",
    "write_file": "Menulis fail",
    "edit_file": "Menyunting fail",
    "list_dir": "Menyemak fail",
    "glob": "Mencari fail",
    "grep": "Mencari dalam kod",
    "deliver": "Menyiapkan hasil akhir",
}


def _phase_label(tool_name: str) -> str:
    if tool_name in _PHASE_LABELS:
        return _PHASE_LABELS[tool_name]
    n = tool_name.lower()
    if "decompile" in n:
        return "Decompile APK"
    if "build" in n or "recompile" in n or "compile" in n:
        return "Build / recompile APK"
    if "sign" in n:
        return "Menandatangani APK"
    if "align" in n:
        return "Zipalign APK"
    if "apk" in n or "aapt" in n:
        return "Memproses APK"
    if "analy" in n or "framework" in n:
        return "Menganalisis"
    return f"Menjalankan {tool_name}"


# --------------------------------------------------------------------------- #
# Telegram Bot API client (stdlib only)
# --------------------------------------------------------------------------- #


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    """Tiny ``urllib``-based Bot API wrapper.

    Only the methods we actually use are implemented; everything else can be
    reached via :meth:`call`.
    """

    def __init__(self, token: str, *, timeout: float = 35.0) -> None:
        if not token:
            raise TelegramError("empty bot token")
        self.token = token
        self.timeout = timeout
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    # -- low level ---------------------------------------------------------- #

    def call(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, tuple[str, bytes, str]]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Invoke ``method`` and return the ``result`` field of the reply."""
        url = f"{self.base}/{method}"
        if files:
            body, content_type = _build_multipart(params or {}, files)
            headers = {"Content-Type": content_type, "User-Agent": "suzu-telegram-bot/0.1"}
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        else:
            data = json.dumps(params or {}).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "suzu-telegram-bot/0.1",
            }
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise TelegramError(f"{method} HTTP {e.code}: {body[:400]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TelegramError(f"{method} network error: {e!r}") from e
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TelegramError(f"{method} bad JSON: {e}; body={raw[:200]}") from e
        if not parsed.get("ok"):
            raise TelegramError(f"{method} not ok: {parsed.get('description')!r}")
        return parsed.get("result")

    # -- helpers ------------------------------------------------------------ #

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
            timeout=timeout + 10,
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: Optional[int] = None,
        parse_mode: Optional[str] = None,
        disable_preview: bool = True,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        try:
            return self.call("editMessageText", params)
        except TelegramError as e:
            # Telegram returns 400 if the text is identical — that's fine.
            if "message is not modified" in str(e).lower():
                return None
            raise

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text[:200]
        if show_alert:
            params["show_alert"] = True
        try:
            self.call("answerCallbackQuery", params)
        except TelegramError as e:
            log.debug("answerCallbackQuery failed: %s", e)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError as e:
            log.debug("sendChatAction failed: %s", e)

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, dest: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
        url = f"{self.file_base}/{file_path}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "suzu-telegram-bot/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(dest, "wb") as f:
            written = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise TelegramError(
                        f"file exceeds {max_bytes} bytes; refusing to keep downloading"
                    )
                f.write(chunk)
            return written

    def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: Optional[str] = None,
        reply_to: Optional[int] = None,
    ) -> dict[str, Any]:
        mime, _ = mimetypes.guess_type(path.name)
        mime = mime or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        params: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption[:1024]
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        return self.call(
            "sendDocument",
            params,
            files={"document": (path.name, data, mime)},
        )


def _build_multipart(
    fields: dict[str, Any],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    """Encode a multipart/form-data body."""
    boundary = "----SuzuBot" + uuid.uuid4().hex
    sep = f"--{boundary}\r\n".encode()
    end = f"--{boundary}--\r\n".encode()
    body: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        body.append(sep)
        body.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.append(_field_value_bytes(value) + b"\r\n")
    for name, (filename, content, mime) in files.items():
        body.append(sep)
        body.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        body.append(f"Content-Type: {mime}\r\n\r\n".encode())
        body.append(content + b"\r\n")
    body.append(end)
    return b"".join(body), f"multipart/form-data; boundary={boundary}"


def _field_value_bytes(value: Any) -> bytes:
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, (int, float)):
        return str(value).encode("utf-8")
    if isinstance(value, (dict, list)):
        return json.dumps(value).encode("utf-8")
    return str(value).encode("utf-8")


# --------------------------------------------------------------------------- #
# Per-chat session map
# --------------------------------------------------------------------------- #


@dataclass
class ChatBinding:
    """Persistent ``chat_id`` → ``session_id`` mapping, plus per-chat config."""

    sessions_dir: Path
    workspaces_dir: Path
    path: Path
    data: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, state_dir: Path, sessions_dir: Path, workspaces_dir: Path) -> "ChatBinding":
        path = state_dir / "telegram" / "chats.json"
        data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        return cls(sessions_dir, workspaces_dir, path, data)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def session_for(self, chat_id: int, model: str) -> Session:
        key = str(chat_id)
        sid = self.data.get(key, {}).get("session_id")
        if sid:
            try:
                return Session.load(self.sessions_dir, sid)
            except (OSError, json.JSONDecodeError):
                log.warning("stale session %s for chat %s; creating a fresh one", sid, key)
        s = Session.new(self.sessions_dir, self.workspaces_dir, model=model)
        s.title = f"Telegram chat {key}"
        s.save(self.sessions_dir)
        self.data[key] = {"session_id": s.id, "created_at": time.time()}
        self._save()
        return s

    def rebind(self, chat_id: int, session: Session) -> None:
        self.data[str(chat_id)] = {"session_id": session.id, "created_at": time.time()}
        self._save()


# --------------------------------------------------------------------------- #
# User store (approved / banned / pending / admins) backed by users.json
# --------------------------------------------------------------------------- #


class UserStore:
    """Live allow/ban/admin list for the bot.

    Persists to ``<state_dir>/telegram/users.json``.  Mutated at runtime by
    the admin commands (``/approve``, ``/ban``, ...) and by the suzu-admin
    TUI on the same host \u2014 a write picks up immediately on the next message
    because we re-read the file at decision time when the on-disk mtime
    changes.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.approved: dict[str, dict[str, Any]] = {}
        self.banned: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.admins: set[str] = set()
        self._mtime: float = 0.0
        self._load()

    @classmethod
    def initialize(
        cls,
        path: Path,
        *,
        seed_allowed: Iterable[int] = (),
        seed_admins: Iterable[int] = (),
    ) -> "UserStore":
        store = cls(path)
        seed_allowed = list(seed_allowed)
        seed_admins = list(seed_admins)
        with store._lock:
            now = time.time()
            changed = False
            # Seed approved on first ever start, but also top-up if a new id
            # appears in the env var (so the env stays a useful "always-trust"
            # fallback).
            for uid in seed_allowed:
                key = str(uid)
                if key not in store.approved and key not in store.banned:
                    store.approved[key] = {"added_at": now, "added_by": "env"}
                    changed = True
            if not store.admins:
                # First boot \u2014 promote the env-listed admins (or fall back to
                # all approved users if no explicit admin list was given).
                pick = [str(uid) for uid in (seed_admins or seed_allowed)]
                if pick:
                    store.admins = set(pick)
                    changed = True
            if changed:
                store._save_locked()
        return store

    # -- io ---------------------------------------------------------------- #

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._mtime = self.path.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                return
            self.approved = dict(raw.get("approved", {}))
            self.banned = dict(raw.get("banned", {}))
            self.pending = dict(raw.get("pending", {}))
            self.admins = set(map(str, raw.get("admins", [])))

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {
            "approved": self.approved,
            "banned": self.banned,
            "pending": self.pending,
            "admins": sorted(self.admins, key=lambda x: int(x) if x.isdigit() else 0),
            "saved_at": time.time(),
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            pass

    def _refresh_if_changed(self) -> None:
        """Re-read users.json if it was modified by suzu-admin TUI."""
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                return
            if mtime > self._mtime:
                self._load()

    # -- queries ----------------------------------------------------------- #

    def is_approved(self, uid: int) -> bool:
        with self._lock:
            self._refresh_if_changed()
            key = str(uid)
            return key in self.approved and key not in self.banned

    def is_admin(self, uid: int) -> bool:
        with self._lock:
            self._refresh_if_changed()
            return str(uid) in self.admins

    def get_approved(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_if_changed()
            return [
                {"id": int(k), **v, "is_admin": k in self.admins}
                for k, v in sorted(self.approved.items(), key=lambda kv: int(kv[0]))
            ]

    def get_banned(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_if_changed()
            return [{"id": int(k), **v} for k, v in self.banned.items()]

    def get_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_if_changed()
            return [
                {"id": int(k), **v}
                for k, v in sorted(
                    self.pending.items(),
                    key=lambda kv: kv[1].get("last_seen", 0),
                    reverse=True,
                )
            ]

    # -- mutations --------------------------------------------------------- #

    def approve(self, uid: int, *, by_uid: int, meta: Optional[dict[str, Any]] = None) -> bool:
        key = str(uid)
        with self._lock:
            self._refresh_if_changed()
            if key in self.approved:
                return False
            entry: dict[str, Any] = {
                "added_at": time.time(),
                "added_by": str(by_uid),
            }
            # Carry over any metadata we observed while the user was pending
            # so the admin sees @username, first_name etc.
            for src in (self.pending.get(key), meta):
                if src:
                    for k in ("username", "first_name", "last_name"):
                        if src.get(k):
                            entry[k] = src[k]
            self.approved[key] = entry
            self.pending.pop(key, None)
            self.banned.pop(key, None)
            self._save_locked()
            return True

    def ban(self, uid: int, *, by_uid: int, reason: str = "") -> bool:
        key = str(uid)
        with self._lock:
            self._refresh_if_changed()
            entry: dict[str, Any] = {
                "banned_at": time.time(),
                "banned_by": str(by_uid),
                "reason": reason,
            }
            # Preserve display name if we had one.
            src = self.approved.get(key) or self.pending.get(key) or {}
            for k in ("username", "first_name", "last_name"):
                if src.get(k):
                    entry[k] = src[k]
            self.banned[key] = entry
            self.approved.pop(key, None)
            self.pending.pop(key, None)
            self.admins.discard(key)
            self._save_locked()
            return True

    def unban(self, uid: int) -> bool:
        key = str(uid)
        with self._lock:
            self._refresh_if_changed()
            if key not in self.banned:
                return False
            del self.banned[key]
            self._save_locked()
            return True

    def add_admin(self, uid: int) -> bool:
        key = str(uid)
        with self._lock:
            self._refresh_if_changed()
            if key not in self.approved:
                # Promote to approved first so admins are always approved.
                self.approved[key] = {"added_at": time.time(), "added_by": "promote"}
            if key in self.admins:
                return False
            self.admins.add(key)
            self._save_locked()
            return True

    def remove_admin(self, uid: int) -> bool:
        key = str(uid)
        with self._lock:
            self._refresh_if_changed()
            if key not in self.admins:
                return False
            self.admins.discard(key)
            self._save_locked()
            return True

    def record_attempt(self, uid: int, *, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Log a rejected attempt so admins can review and approve later."""
        key = str(uid)
        with self._lock:
            self._refresh_if_changed()
            entry = self.pending.get(key, {})
            now = time.time()
            entry["first_seen"] = entry.get("first_seen", now)
            entry["last_seen"] = now
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if meta:
                for k in ("username", "first_name", "last_name", "language_code"):
                    if meta.get(k):
                        entry[k] = meta[k]
            self.pending[key] = entry
            self._save_locked()
            return entry


# --------------------------------------------------------------------------- #
# Typing indicator helper
# --------------------------------------------------------------------------- #


class TypingPing:
    """Refreshes the ``typing`` chat-action every few seconds in the background."""

    def __init__(self, api: TelegramAPI, chat_id: int, action: str = "typing"):
        self.api = api
        self.chat_id = chat_id
        self.action = action
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "TypingPing":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.api.send_chat_action(self.chat_id, self.action)
            self._stop.wait(TYPING_REFRESH_S)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #


@dataclass
class BotConfig:
    bot_token: str
    allowed_user_ids: set[int]
    admin_user_ids: set[int]
    bot_username: str = ""

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is empty — set it in /etc/suzu-panel/.env (or the "
                "session env file) and restart the bot."
            )
        allowed = _parse_id_list(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
        admins = _parse_id_list(os.environ.get("TELEGRAM_ADMIN_USER_IDS", ""))
        # Live allowlist is stored in users.json; the env var is only used as
        # a seed on first boot (and as a top-up if a new id appears).  An
        # empty env var on subsequent boots is fine — the on-disk allowlist
        # is the source of truth.
        username = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
        return cls(
            bot_token=token,
            allowed_user_ids=allowed,
            admin_user_ids=admins,
            bot_username=username,
        )


def _parse_id_list(raw: str) -> set[int]:
    out: set[int] = set()
    for tok in (raw or "").replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            log.warning("ignoring non-numeric Telegram id: %r", tok)
    return out


class ChatWorkers:
    """Run work on a dedicated background thread *per chat*.

    The poll loop must never block: a single APK build can take many minutes,
    and while it runs the bot still has to answer everyone else (and let the
    same user send follow-ups). We give each chat its own FIFO queue + thread,
    so:

    * different chats run concurrently;
    * messages within one chat stay strictly in order;
    * ``getUpdates`` keeps polling no matter how long a turn takes.
    """

    def __init__(self) -> None:
        self._queues: dict[int, "queue.Queue[Callable[[], None]]"] = {}
        self._lock = threading.Lock()

    def submit(self, key: int, fn: Callable[[], None]) -> None:
        with self._lock:
            q = self._queues.get(key)
            if q is None:
                q = queue.Queue()
                self._queues[key] = q
                threading.Thread(
                    target=self._worker, args=(key, q), daemon=True, name=f"chat-{key}"
                ).start()
        q.put(fn)

    def _worker(self, key: int, q: "queue.Queue[Callable[[], None]]") -> None:
        while True:
            fn = q.get()
            try:
                fn()
            except Exception:  # noqa: BLE001 — one bad turn must not kill the worker
                log.exception("error in chat worker %s", key)
            finally:
                q.task_done()


class Bot:
    def __init__(self, bot_cfg: BotConfig, cfg: Config):
        self.bot_cfg = bot_cfg
        self.cfg = cfg
        self.api = TelegramAPI(bot_cfg.bot_token)
        self.registry = build_default_registry()
        self.binding = ChatBinding.load(cfg.state_dir, cfg.sessions_dir, cfg.workspaces_dir)
        self.users = UserStore.initialize(
            cfg.state_dir / "telegram" / "users.json",
            seed_allowed=bot_cfg.allowed_user_ids,
            seed_admins=bot_cfg.admin_user_ids,
        )
        self.client = ChatClient(cfg.api_base_url, cfg.api_key, timeout=cfg.request_timeout)
        # Multi-provider model registry (shared with the suzu-admin TUI via
        # <state_dir>/models.json). Seeded from the .env on first run so the
        # bot always has at least one usable profile.
        self.models = ModelRegistry.load(
            cfg.state_dir,
            seed=ModelProfile(
                name=cfg.default_model,
                base_url=cfg.api_base_url,
                model=cfg.default_model,
                api_key=cfg.api_key,
            ),
        )
        # Cache one ChatClient per (base_url, api_key) so switching models is
        # cheap and connection settings are reused.
        self._clients: dict[tuple[str, str], ChatClient] = {}
        self.workers = ChatWorkers()
        # Files a user uploaded but hasn't yet told us what to do with. Keyed by
        # chat id; the per-chat worker is the only writer/reader, so no lock.
        self._pending_files: dict[int, list[str]] = {}
        me = self.api.get_me()
        approved_now = [u["id"] for u in self.users.get_approved()]
        admins_now = sorted(int(a) for a in self.users.admins)
        log.info(
            "Suzu Telegram bot ready: @%s (id=%s) approved=%s admins=%s",
            me.get("username"),
            me.get("id"),
            approved_now,
            admins_now,
        )
        if not bot_cfg.bot_username and me.get("username"):
            self.bot_cfg.bot_username = me["username"]

    # -- main loop ---------------------------------------------------------- #

    def run(self) -> None:
        offset = 0
        backoff = 1.0
        while True:
            try:
                updates = self.api.get_updates(offset=offset, timeout=POLL_TIMEOUT_S)
                backoff = 1.0
            except TelegramError as e:
                log.error("getUpdates failed: %s (sleeping %.1fs)", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            for upd in updates:
                offset = max(offset, upd.get("update_id", 0) + 1)
                # Hand the update to the per-chat worker so a long-running turn
                # (e.g. an APK build) never blocks polling or other chats.
                key = _update_chat_key(upd)
                self.workers.submit(key, lambda u=upd: self._dispatch_safely(u))

    def _dispatch_safely(self, update: dict[str, Any]) -> None:
        try:
            self._dispatch(update)
        except Exception:  # noqa: BLE001 — never let one update kill the worker
            log.exception("error handling update %s", update.get("update_id"))

    # -- dispatch ----------------------------------------------------------- #

    def _dispatch(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._dispatch_callback(update["callback_query"])
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        sender = msg.get("from") or {}
        uid = sender.get("id")
        if uid is None:
            return
        if not self.users.is_approved(uid):
            # Track the attempt so an admin can review/approve them later.
            entry = self.users.record_attempt(
                uid,
                meta={
                    "username": sender.get("username", ""),
                    "first_name": sender.get("first_name", ""),
                    "last_name": sender.get("last_name", ""),
                    "language_code": sender.get("language_code", ""),
                },
            )
            log.info(
                "reject from user %s (@%s) — attempts=%s",
                uid,
                sender.get("username", ""),
                entry.get("attempts"),
            )
            try:
                self.api.send_message(
                    msg["chat"]["id"],
                    "Maaf, awak tidak dibenarkan menggunakan bot ini.\n\n"
                    f"Telegram id awak: `{uid}`.\n"
                    "Pentadbir bot perlu approve dulu.",
                    parse_mode="Markdown",
                )
            except TelegramError as e:
                log.debug("reject reply failed: %s", e)
            # Notify admins so they can act quickly.
            self._notify_admins_pending(uid, sender, entry)
            return
        self._handle_message(msg)

    def _notify_admins_pending(
        self, uid: int, sender: dict[str, Any], entry: dict[str, Any]
    ) -> None:
        """Ping every admin that someone tried to use the bot, with inline
        Approve/Ban buttons so they can act in one tap."""
        # Only ping once when the user first appears — not on every attempt.
        if int(entry.get("attempts", 1)) != 1:
            return
        label = self._user_label(sender)
        body = (
            f"🔔 Pending user: {label}\n"
            f"Telegram id: `{uid}`\n"
            "Use the buttons below to approve or ban."
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{uid}"},
                    {"text": "🚫 Ban", "callback_data": f"ban:{uid}"},
                ]
            ]
        }
        for admin_id in sorted(int(a) for a in self.users.admins):
            try:
                self.api.send_message(admin_id, body, parse_mode="Markdown", reply_markup=kb)
            except TelegramError as e:
                log.debug("could not notify admin %s: %s", admin_id, e)

    # -- message types ------------------------------------------------------ #

    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        user_instruction = text  # what the user actually typed (no file notes yet)
        if text.startswith("/"):
            if self._handle_command(msg, text):
                return

        # Make sure we have a session before downloading anything, so files
        # land inside the workspace.
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        self._ensure_system_prompt(session)

        downloaded: list[Path] = []
        dl_errors: list[str] = []
        if "document" in msg:
            downloaded += self._download_document(session, msg["document"], dl_errors)
        if "photo" in msg:
            downloaded += self._download_photo(session, msg["photo"], dl_errors)
        if "video" in msg:
            downloaded += self._download_document(session, msg["video"], dl_errors)
        if "audio" in msg:
            downloaded += self._download_document(session, msg["audio"], dl_errors)
        if "voice" in msg:
            downloaded += self._download_document(session, msg["voice"], dl_errors)
        if "animation" in msg:
            downloaded += self._download_document(session, msg["animation"], dl_errors)

        if downloaded:
            note_lines = [
                f"User uploaded file: {p} (size {p.stat().st_size} bytes)"
                for p in downloaded
            ]
            ack = "\n".join(
                f"📥 saved: `{p.name}` → `{p}` ({_human_size(p.stat().st_size)})"
                for p in downloaded
            )
            self.api.send_message(chat_id, ack, parse_mode="Markdown")
            text = (text + "\n\n" + "\n".join(note_lines)).strip() if text else "\n".join(note_lines)

        # Surface download failures to the user. Previously these were logged
        # and silently swallowed, so an oversized APK looked like the AI simply
        # "couldn't read" the file.
        if dl_errors:
            self.api.send_message(
                chat_id,
                "⚠️ Tak dapat ambil sebahagian fail:\n"
                + "\n".join(f"• {e}" for e in dl_errors)
                + "\n\nCadangan: hantar APK ≤ 20 MB, zip & pecahkan, atau letak di "
                "link (Drive/MEGA) dan beri saya URL untuk `shell` muat turun.",
                parse_mode="Markdown",
            )

        # If the user dropped an APK/AAB without saying what to do, don't just
        # start hammering away — ask first (inline buttons), like a pro analyst.
        apk_uploads = [
            p for p in downloaded if p.suffix.lower() in _FINAL_ARTIFACT_EXTS
        ]
        if apk_uploads and not user_instruction:
            self._pending_files[chat_id] = [str(p) for p in apk_uploads]
            self._ask_apk_intent(chat_id, apk_uploads, reply_to=msg.get("message_id"))
            return

        if not text:
            return

        self._route_to_agent(chat_id, session, text, reply_to=msg.get("message_id"))

    def _ask_apk_intent(
        self, chat_id: int, apks: list[Path], *, reply_to: Optional[int] = None
    ) -> None:
        names = ", ".join(f"`{p.name}`" for p in apks)
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🔍 Analisa", "callback_data": "apk:analyze"},
                    {"text": "🧩 Decompile", "callback_data": "apk:decompile"},
                ],
                [
                    {"text": "🔨 Build / Recompile", "callback_data": "apk:build"},
                    {"text": "🛠️ Fix masalah", "callback_data": "apk:fix"},
                ],
                [
                    {"text": "📲 Sambung projek", "callback_data": "apk:continue"},
                ],
            ]
        }
        self.api.send_message(
            chat_id,
            f"📦 Dah terima {names}.\n\n*Nak buat apa dengan APK ni?*\n"
            "Pilih di bawah, atau terus taip arahan awak.",
            parse_mode="Markdown",
            reply_to=reply_to,
            reply_markup=kb,
        )

    _APK_INTENTS = {
        "analyze": "Analisa APK ni: kenal pasti framework, struktur, permission, "
                   "library utama, dan ringkaskan apa app ni buat.",
        "decompile": "Decompile APK ni (smali + resources) dan ringkaskan struktur "
                     "kod & komponen penting. Jangan hantar fail tengah-tengah.",
        "build": "Recompile/build APK ni semula jadi APK yang ditandatangan (signed) "
                 "& zipalign, sedia untuk dipasang. Hantar APK akhir sahaja.",
        "fix": "Saya nak fix sesuatu dalam APK ni. Tanya saya dulu apa yang nak "
               "dibaiki kalau belum jelas, kemudian baiki dan bina semula APK yang signed.",
        "continue": "Sambung projek menggunakan APK ni. Tanya saya konteks projek "
                    "kalau perlu, kemudian teruskan kerja.",
    }

    def _handle_apk_intent(
        self, chat_id: int, action: str, *, msg_id: Optional[int] = None
    ) -> None:
        instruction = self._APK_INTENTS.get(action)
        if instruction is None:
            return
        files = self._pending_files.pop(chat_id, [])
        # Clear the buttons on the picker so it can't be tapped twice.
        if msg_id is not None:
            label = {
                "analyze": "🔍 Analisa",
                "decompile": "🧩 Decompile",
                "build": "🔨 Build / Recompile",
                "fix": "🛠️ Fix masalah",
                "continue": "📲 Sambung projek",
            }.get(action, action)
            try:
                self.api.edit_message_text(
                    chat_id, msg_id, f"▶️ Pilihan: *{label}*",
                    parse_mode="Markdown", reply_markup={"inline_keyboard": []},
                )
            except TelegramError:
                pass
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        self._ensure_system_prompt(session)
        note = ""
        if files:
            note = "\n\n" + "\n".join(f"Fail APK: {f}" for f in files)
        self._route_to_agent(chat_id, session, instruction + note)

    # -- commands ----------------------------------------------------------- #

    def _handle_command(self, msg: dict[str, Any], text: str) -> bool:
        chat_id = msg["chat"]["id"]
        sender = msg.get("from") or {}
        head, _, rest = text.partition(" ")
        head = head.split("@", 1)[0].lower()  # strip "@botname"
        rest = rest.strip()
        is_admin = self.users.is_admin(int(sender.get("id") or 0))

        if head == "/start":
            self._cmd_start(chat_id, is_admin=is_admin)
            return True
        if head == "/help":
            self._cmd_help(chat_id, is_admin=is_admin)
            return True
        if head == "/status":
            self._cmd_status(chat_id)
            return True
        if head == "/new":
            self._cmd_new(chat_id)
            return True
        if head in ("/sessions", "/projects"):
            self._cmd_sessions(chat_id)
            return True
        if head == "/workspace":
            self._cmd_workspace(chat_id)
            return True
        if head in ("/clear", "/reset"):
            self._cmd_clear(chat_id)
            return True
        if head == "/models":
            self._cmd_models(chat_id)
            return True
        if head == "/model":
            self._cmd_model(chat_id, rest)
            return True
        if head == "/whoami":
            uid = sender.get("id")
            self.api.send_message(
                chat_id,
                f"id: `{uid}`\nusername: @{sender.get('username','')}\n"
                f"name: {sender.get('first_name','')} {sender.get('last_name','')}\n"
                f"role: {'admin' if is_admin else 'user'}".strip(),
                parse_mode="Markdown",
            )
            return True

        # ------------------------------------------------------------------
        # Admin-only commands. We always handle the dispatch (return True) so
        # the message doesn't accidentally get forwarded to the model, but we
        # tell unauthorised callers they can't run it.
        # ------------------------------------------------------------------
        if head in ("/users", "/pending", "/approve", "/ban", "/unban",
                    "/admins", "/promote", "/demote"):
            if not is_admin:
                self.api.send_message(
                    chat_id,
                    "Maaf, command ini untuk admin sahaja.",
                )
                return True
            uid_admin = int(sender["id"])
            if head == "/users":
                self._cmd_admin_users(chat_id)
            elif head == "/pending":
                self._cmd_admin_pending(chat_id)
            elif head == "/approve":
                self._cmd_admin_approve(chat_id, rest, by_uid=uid_admin)
            elif head == "/ban":
                self._cmd_admin_ban(chat_id, rest, by_uid=uid_admin)
            elif head == "/unban":
                self._cmd_admin_unban(chat_id, rest)
            elif head == "/admins":
                self._cmd_admin_admins(chat_id)
            elif head == "/promote":
                self._cmd_admin_promote(chat_id, rest)
            elif head == "/demote":
                self._cmd_admin_demote(chat_id, rest, self_uid=uid_admin)
            return True

        # Unknown slash command — let the model see it as plain text.
        return False

    def _cmd_start(self, chat_id: int, *, is_admin: bool = False) -> None:
        uname = self.bot_cfg.bot_username or "bot"
        text = (
            f"👋 *Selamat datang ke Suzu Chat AI* (@{uname})\n\n"
            "Saya pakar di dalam APK reverse engineering — boleh build, "
            "decompile, recompile, sign, analyse file, dan banyak lagi.\n\n"
            "Hantar mesej biasa untuk berborak, atau forward APK/file untuk "
            "saya analyse terus.\n\n"
            "_Slash commands:_\n"
            "  /help — list semua command\n"
            "  /status — status tools di server\n"
            "  /new — start session baru (history kosong)\n"
            "  /clear (atau /reset) — kosongkan history sesi semasa\n"
            "     (guna kalau bot mula merepek atau ulang benda yang awak\n"
            "      tak pernah cakap — history mungkin tercemar)\n"
            "  /sessions — list session\n"
            "  /workspace — print path workspace\n"
            "  /models — senarai model, pilih dgn butang\n"
            "  /model NAME — tukar model terus\n"
            "  /whoami — Telegram id awak"
        )
        if is_admin:
            text += (
                "\n\n*Admin commands:*\n"
                "  /users — list approved/banned/pending users\n"
                "  /pending — review users yang nak akses (dgn butang approve/ban)\n"
                "  /approve <id> — approve user\n"
                "  /ban <id> [reason] — ban user\n"
                "  /unban <id> — buka ban\n"
                "  /admins — list admins\n"
                "  /promote <id> — jadikan admin\n"
                "  /demote <id> — turunkan dari admin"
            )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "📊 Status", "callback_data": "menu:status"},
                    {"text": "📁 Workspace", "callback_data": "menu:workspace"},
                ],
                [
                    {"text": "💬 New session", "callback_data": "menu:new"},
                    {"text": "📂 Sessions", "callback_data": "menu:sessions"},
                ],
                [
                    {"text": "🧠 Pilih Model", "callback_data": "menu:models"},
                    {"text": "❓ Help", "callback_data": "menu:help"},
                ],
            ]
        }
        if is_admin:
            kb["inline_keyboard"].append(
                [
                    {"text": "👥 Users", "callback_data": "menu:users"},
                    {"text": "⏳ Pending", "callback_data": "menu:pending"},
                ]
            )
        self.api.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)

    def _cmd_help(self, chat_id: int, *, is_admin: bool = False) -> None:
        self._cmd_start(chat_id, is_admin=is_admin)

    def _cmd_status(self, chat_id: int) -> None:
        tools = [
            "apktool", "zipalign", "apksigner", "aapt2",
            "jadx", "d2j-dex2jar", "smali", "baksmali",
            "file", "strings", "python3", "java",
        ]
        lines = ["*Tools available di server:*", ""]
        for t in tools:
            p = shutil.which(t)
            mark = "✅" if p else "❌"
            lines.append(f"{mark} `{t}` → `{p or 'MISSING'}`")
        lines.append("")
        self.models.reload_if_changed()
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        cur = self._resolve_profile(session)
        if cur is not None:
            lines.append(f"Model     : `{cur.model}`  ({cur.name})")
            lines.append(f"API base  : `{cur.base_url}`")
        else:
            lines.append(f"Model     : `{self.cfg.default_model}`")
            lines.append(f"API base  : `{self.cfg.api_base_url}`")
        lines.append(f"Profiles  : {len(self.models.list())} (guna /models)")
        lines.append(f"Workspace : `{self.cfg.workspaces_dir}`")
        self.api.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_new(self, chat_id: int) -> None:
        session = Session.new(
            self.cfg.sessions_dir,
            self.cfg.workspaces_dir,
            model=self.cfg.default_model,
        )
        session.title = f"Telegram chat {chat_id}"
        session.save(self.cfg.sessions_dir)
        self.binding.rebind(chat_id, session)
        self._ensure_system_prompt(session)
        self.api.send_message(
            chat_id,
            f"🆕 Session baru: `{session.id}`\nWorkspace: `{session.workspace}`",
            parse_mode="Markdown",
        )

    def _cmd_sessions(self, chat_id: int) -> None:
        items = list_sessions(self.cfg.sessions_dir)
        if not items:
            self.api.send_message(chat_id, "(no sessions yet)")
            return
        # Highlight the one currently bound to this chat.
        bound = self.binding.data.get(str(chat_id), {}).get("session_id")
        lines = ["*Sessions (latest 15):*", ""]
        for s in items[:15]:
            star = "⭐" if s["id"] == bound else "  "
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
            lines.append(
                f"{star} `{s['id']}` · {when} · msgs={s['messages']} · {s.get('title','')}"
            )
        self.api.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_workspace(self, chat_id: int) -> None:
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        self.api.send_message(
            chat_id,
            f"`{session.workspace}`",
            parse_mode="Markdown",
        )

    def _cmd_clear(self, chat_id: int) -> None:
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        sys_msgs = [m for m in session.messages if m.get("role") == "system"]
        session.messages = sys_msgs
        session.save(self.cfg.sessions_dir)
        # Any queued uploads from the now-discarded turn no longer make sense.
        self._pending_files.pop(chat_id, None)
        self.api.send_message(
            chat_id,
            "🧹 History dikosongkan. Hantar task baru — fresh slate.",
        )

    def _cmd_model(self, chat_id: int, rest: str) -> None:
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        self.models.reload_if_changed()
        if not rest:
            cur = self._resolve_profile(session)
            label = cur.name if cur else session.model
            self.api.send_message(
                chat_id,
                f"Model semasa: *{label}*\n`{session.model}`\n\n"
                "Guna /models untuk pilih dari senarai.",
                parse_mode="Markdown",
            )
            return
        arg = rest.strip()
        prof = self.models.get(arg)
        if prof is not None:
            session.profile = prof.name
            session.model = prof.model
            session.save(self.cfg.sessions_dir)
            self.api.send_message(
                chat_id, f"✅ Model ditukar: *{prof.name}* (`{prof.model}`)",
                parse_mode="Markdown",
            )
            return
        # Legacy: treat the argument as a raw model id on the current endpoint.
        session.model = arg.split()[0]
        session.save(self.cfg.sessions_dir)
        self.api.send_message(
            chat_id,
            f"✅ model id: `{session.model}`\n"
            "(tiada profil dengan nama itu — guna /models untuk senarai)",
            parse_mode="Markdown",
        )

    def _cmd_models(self, chat_id: int) -> None:
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        self.models.reload_if_changed()
        profs = self.models.list()
        if not profs:
            self.api.send_message(
                chat_id,
                "Belum ada model. Admin boleh tambah di server: "
                "`suzu-admin` → *Model Manager*.",
                parse_mode="Markdown",
            )
            return
        current = self._resolve_profile(session)
        cur_name = current.name if current else ""
        lines = ["*Pilih model:*", ""]
        rows: list[list[dict[str, str]]] = []
        for p in profs:
            mark = "🟢" if p.name == cur_name else "⚪"
            lines.append(f"{mark} *{p.name}* — `{p.model}`")
            rows.append([{"text": f"{mark} {p.name}", "callback_data": f"model:set:{p.key}"}])
        self.api.send_message(
            chat_id,
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": rows},
        )

    # -- admin commands ---------------------------------------------------- #

    def _cmd_admin_users(self, chat_id: int) -> None:
        approved = self.users.get_approved()
        banned = self.users.get_banned()
        pending = self.users.get_pending()
        lines = [f"*\ud83d\udc65 Users* (approved={len(approved)} banned={len(banned)} pending={len(pending)})", ""]
        if approved:
            lines.append("*Approved:*")
            for u in approved[:20]:
                admin_mark = " \u2b50" if u.get("is_admin") else ""
                label = self._user_label_from_info(u)
                lines.append(f"  {label}{admin_mark}")
        if banned:
            lines.append("\n*Banned:*")
            for u in banned[:10]:
                label = self._user_label_from_info(u)
                lines.append(f"  {label} \u2014 {u.get('reason', '')}")
        if pending:
            lines.append("\n*Pending (wants access):*")
            for u in pending[:10]:
                label = self._user_label_from_info(u)
                lines.append(f"  {label} \u2014 attempts={u.get('attempts', 0)}")
        self.api.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_admin_pending(self, chat_id: int) -> None:
        pending = self.users.get_pending()
        if not pending:
            self.api.send_message(chat_id, "\u2705 Tiada pending users.")
            return
        for u in pending[:10]:
            label = self._user_label_from_info(u)
            uid = u["id"]
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(u.get("last_seen", 0)))
            body = (
                f"\ud83d\udc64 *Pending user*\n"
                f"  {label}\n"
                f"  attempts: {u.get('attempts', 0)} | last: {when}\n"
            )
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "\u2705 Approve", "callback_data": f"approve:{uid}"},
                        {"text": "\ud83d\udeab Ban", "callback_data": f"ban:{uid}"},
                    ]
                ]
            }
            self.api.send_message(chat_id, body, parse_mode="Markdown", reply_markup=kb)

    def _cmd_admin_approve(self, chat_id: int, rest: str, *, by_uid: int) -> None:
        uid = self._parse_uid_arg(rest)
        if uid is None:
            self.api.send_message(chat_id, "Usage: `/approve <user_id>`", parse_mode="Markdown")
            return
        ok = self.users.approve(uid, by_uid=by_uid)
        if ok:
            self.api.send_message(chat_id, f"\u2705 User `{uid}` approved.", parse_mode="Markdown")
            try:
                self.api.send_message(uid, "\ud83c\udf89 Awak telah diluluskan! Hantar `/start` untuk mula.")
            except TelegramError:
                pass
        else:
            self.api.send_message(chat_id, f"User `{uid}` sudah approved sebelum ini.", parse_mode="Markdown")

    def _cmd_admin_ban(self, chat_id: int, rest: str, *, by_uid: int) -> None:
        parts = rest.split(maxsplit=1)
        uid = self._parse_uid_arg(parts[0] if parts else "")
        reason = parts[1] if len(parts) > 1 else ""
        if uid is None:
            self.api.send_message(chat_id, "Usage: `/ban <user_id> [reason]`", parse_mode="Markdown")
            return
        self.users.ban(uid, by_uid=by_uid, reason=reason)
        self.api.send_message(chat_id, f"\ud83d\udeab User `{uid}` banned. reason: {reason or '(none)'}", parse_mode="Markdown")

    def _cmd_admin_unban(self, chat_id: int, rest: str) -> None:
        uid = self._parse_uid_arg(rest)
        if uid is None:
            self.api.send_message(chat_id, "Usage: `/unban <user_id>`", parse_mode="Markdown")
            return
        ok = self.users.unban(uid)
        if ok:
            self.api.send_message(chat_id, f"\u2705 User `{uid}` unbanned.", parse_mode="Markdown")
        else:
            self.api.send_message(chat_id, f"User `{uid}` not in ban list.", parse_mode="Markdown")

    def _cmd_admin_admins(self, chat_id: int) -> None:
        admins = sorted(int(a) for a in self.users.admins)
        if not admins:
            self.api.send_message(chat_id, "Tiada admins (semua env users)")
            return
        lines = ["*Admins:*"]
        for uid in admins:
            info = self.users.approved.get(str(uid), {})
            lines.append(f"  \u2b50 `{uid}` {info.get('username', '')} {info.get('first_name', '')}")
        self.api.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_admin_promote(self, chat_id: int, rest: str) -> None:
        uid = self._parse_uid_arg(rest)
        if uid is None:
            self.api.send_message(chat_id, "Usage: `/promote <user_id>`", parse_mode="Markdown")
            return
        self.users.add_admin(uid)
        self.api.send_message(chat_id, f"\u2b50 `{uid}` sekarang admin.", parse_mode="Markdown")

    def _cmd_admin_demote(self, chat_id: int, rest: str, *, self_uid: int) -> None:
        uid = self._parse_uid_arg(rest)
        if uid is None:
            self.api.send_message(chat_id, "Usage: `/demote <user_id>`", parse_mode="Markdown")
            return
        if uid == self_uid:
            self.api.send_message(chat_id, "Awak tak boleh demote diri sendiri.")
            return
        ok = self.users.remove_admin(uid)
        if ok:
            self.api.send_message(chat_id, f"\u2705 `{uid}` bukan admin lagi.", parse_mode="Markdown")
        else:
            self.api.send_message(chat_id, f"`{uid}` bukan admin.", parse_mode="Markdown")

    # -- callback queries (inline button presses) -------------------------- #

    def _dispatch_callback(self, query: dict[str, Any]) -> None:
        qid = query.get("id", "")
        sender = query.get("from") or {}
        uid = int(sender.get("id") or 0)
        data = query.get("data") or ""
        msg = query.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id") or uid
        msg_id = msg.get("message_id")

        # Menu shortcuts (available to all approved users).
        if data.startswith("menu:"):
            self.api.answer_callback_query(qid)
            action = data.split(":", 1)[1]
            is_admin = self.users.is_admin(uid)
            if action == "status":
                self._cmd_status(chat_id)
            elif action == "workspace":
                self._cmd_workspace(chat_id)
            elif action == "new":
                self._cmd_new(chat_id)
            elif action == "sessions":
                self._cmd_sessions(chat_id)
            elif action == "help":
                self._cmd_help(chat_id, is_admin=is_admin)
            elif action == "models":
                self._cmd_models(chat_id)
            elif action == "users" and is_admin:
                self._cmd_admin_users(chat_id)
            elif action == "pending" and is_admin:
                self._cmd_admin_pending(chat_id)
            return

        # Model picker (available to all approved users).
        if data.startswith("model:set:"):
            if not self.users.is_approved(uid):
                self.api.answer_callback_query(qid, text="Tak dibenarkan.", show_alert=True)
                return
            key = data.split(":", 2)[2]
            self.models.reload_if_changed()
            prof = self.models.get(key)
            if prof is None:
                self.api.answer_callback_query(qid, text="Model tak dijumpai", show_alert=True)
                return
            session = self.binding.session_for(chat_id, model=self.cfg.default_model)
            session.profile = prof.name
            session.model = prof.model
            session.save(self.cfg.sessions_dir)
            self.api.answer_callback_query(qid, text=f"✅ {prof.name}")
            if msg_id:
                self.api.edit_message_text(
                    chat_id, msg_id,
                    f"✅ Model ditukar: *{prof.name}*\n`{prof.model}`",
                    parse_mode="Markdown",
                    reply_markup={"inline_keyboard": []},
                )
            return

        # "What to do with this APK?" picker (available to all approved users).
        if data.startswith("apk:"):
            if not self.users.is_approved(uid):
                self.api.answer_callback_query(qid, text="Tak dibenarkan.", show_alert=True)
                return
            self.api.answer_callback_query(qid)
            self._handle_apk_intent(chat_id, data.split(":", 1)[1], msg_id=msg_id)
            return

        # Admin approve/ban actions.
        if not self.users.is_admin(uid):
            self.api.answer_callback_query(qid, text="Admin sahaja.", show_alert=True)
            return

        if data.startswith("approve:"):
            target_uid = int(data.split(":", 1)[1])
            ok = self.users.approve(target_uid, by_uid=uid)
            if ok:
                self.api.answer_callback_query(qid, text=f"\u2705 {target_uid} approved")
                # Edit the original message to mark as handled.
                if msg_id:
                    self.api.edit_message_text(
                        chat_id, msg_id,
                        f"\u2705 *Approved* user `{target_uid}`",
                        parse_mode="Markdown",
                        reply_markup={"inline_keyboard": []},
                    )
                # Let the user know they got access.
                try:
                    self.api.send_message(
                        target_uid,
                        "\ud83c\udf89 Awak telah diluluskan! Hantar `/start` untuk mula.",
                    )
                except TelegramError:
                    pass
            else:
                self.api.answer_callback_query(qid, text=f"{target_uid} already approved")
            return

        if data.startswith("ban:"):
            target_uid = int(data.split(":", 1)[1])
            self.users.ban(target_uid, by_uid=uid)
            self.api.answer_callback_query(qid, text=f"\ud83d\udeab {target_uid} banned")
            if msg_id:
                self.api.edit_message_text(
                    chat_id, msg_id,
                    f"\ud83d\udeab *Banned* user `{target_uid}`",
                    parse_mode="Markdown",
                    reply_markup={"inline_keyboard": []},
                )
            return

        self.api.answer_callback_query(qid, text="Unknown action")

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def _user_label(sender: dict[str, Any]) -> str:
        uname = sender.get("username", "")
        name = f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip()
        uid = sender.get("id", "?")
        return f"@{uname} ({name}, id={uid})" if uname else f"{name} (id={uid})"

    @staticmethod
    def _user_label_from_info(info: dict[str, Any]) -> str:
        uid = info.get("id", "?")
        uname = info.get("username", "")
        name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
        return f"`{uid}` @{uname} ({name})" if uname else f"`{uid}` {name}"

    @staticmethod
    def _parse_uid_arg(s: str) -> Optional[int]:
        s = s.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None

    # -- session helpers --------------------------------------------------- #

    def _ensure_system_prompt(self, session: Session) -> None:
        prompt = render_system_prompt(session.workspace, session.model)
        dirty = False
        # Auto-heal: drop assistant messages echoing known hallucinated
        # "protocols" (e.g. "Protokol chunked write"). Targeted to specific
        # phrases and text-only messages, so real work is never touched.
        removed = strip_hallucinated_protocols(session.messages)
        if removed:
            log.info(
                "chat %s: stripped %d hallucinated-protocol message(s) from history",
                session.id, removed,
            )
            dirty = True
        for m in session.messages:
            if m.get("role") == "system":
                # Refresh in place so existing sessions pick up updated rules
                # (e.g. the new deliver-only / ask-first behaviour).
                if m.get("content") != prompt:
                    m["content"] = prompt
                    dirty = True
                if dirty:
                    session.save(self.cfg.sessions_dir)
                return
        session.messages.insert(0, {"role": "system", "content": prompt})
        session.save(self.cfg.sessions_dir)

    # -- file downloads ---------------------------------------------------- #

    def _download_document(
        self,
        session: Session,
        doc: dict[str, Any],
        errors: Optional[list[str]] = None,
    ) -> list[Path]:
        name_hint = doc.get("file_name") or "file"
        size = doc.get("file_size") or 0
        # Telegram's cloud Bot API only serves getFile/download for files up to
        # 20 MB. Bigger uploads fail server-side, so warn the user up front
        # instead of silently dropping the file (which made the AI look like it
        # "couldn't read" the APK).
        if size and size > TELEGRAM_GETFILE_LIMIT:
            if errors is not None:
                errors.append(
                    f"`{name_hint}` ({_human_size(size)}) terlalu besar — Telegram "
                    f"Bot API hanya benarkan muat turun sehingga {_human_size(TELEGRAM_GETFILE_LIMIT)}."
                )
            return []
        try:
            info = self.api.get_file(doc["file_id"])
        except TelegramError as e:
            log.warning("getFile failed for %s: %s", doc.get("file_id"), e)
            if errors is not None:
                errors.append(_download_error_hint(name_hint, size, e))
            return []
        remote = info.get("file_path") or ""
        if not remote:
            if errors is not None:
                errors.append(f"`{name_hint}`: Telegram tak pulangkan path fail.")
            return []
        size = info.get("file_size") or size
        name = doc.get("file_name") or Path(remote).name or f"file_{int(time.time())}"
        dest = Path(session.workspace) / "uploads" / _safe_filename(name)
        try:
            self.api.download_file(remote, dest)
        except TelegramError as e:
            log.warning("download failed for %s: %s", remote, e)
            if errors is not None:
                errors.append(_download_error_hint(name, size, e))
            return []
        log.info("downloaded %s -> %s", remote, dest)
        return [dest]

    def _download_photo(
        self,
        session: Session,
        photo_sizes: list[dict[str, Any]],
        errors: Optional[list[str]] = None,
    ) -> list[Path]:
        if not photo_sizes:
            return []
        # Pick the largest variant.
        largest = max(photo_sizes, key=lambda p: p.get("file_size") or (p.get("width", 0) * p.get("height", 0)))
        try:
            info = self.api.get_file(largest["file_id"])
        except TelegramError as e:
            log.warning("getFile photo failed: %s", e)
            if errors is not None:
                errors.append(_download_error_hint("photo", largest.get("file_size", 0), e))
            return []
        remote = info.get("file_path") or ""
        if not remote:
            return []
        name = Path(remote).name or f"photo_{int(time.time())}.jpg"
        dest = Path(session.workspace) / "uploads" / _safe_filename(name)
        try:
            self.api.download_file(remote, dest)
        except TelegramError as e:
            log.warning("download photo failed: %s", e)
            return []
        return [dest]

    # -- agent routing ----------------------------------------------------- #

    # -- model profiles ---------------------------------------------------- #

    def _client_for(self, profile: ModelProfile) -> ChatClient:
        """Return a cached ChatClient for this profile's endpoint/key."""
        key = (profile.base_url, profile.api_key)
        client = self._clients.get(key)
        if client is None:
            client = ChatClient(
                profile.base_url or self.cfg.api_base_url,
                profile.api_key or self.cfg.api_key,
                timeout=self.cfg.request_timeout,
            )
            self._clients[key] = client
        return client

    def _resolve_profile(self, session: Session) -> Optional[ModelProfile]:
        """Pick the model profile for this session, honouring live edits.

        Order: the session's chosen profile -> registry default -> None
        (caller then falls back to the env-configured default client).
        """
        self.models.reload_if_changed()
        prof = self.models.get(session.profile) if session.profile else None
        if prof is None:
            prof = self.models.default_profile()
        return prof

    def _route_to_agent(
        self,
        chat_id: int,
        session: Session,
        text: str,
        *,
        reply_to: Optional[int] = None,
    ) -> None:
        # Resolve which model/provider this chat should use right now. Admin
        # edits in models.json are picked up live here.
        profile = self._resolve_profile(session)
        if profile is not None:
            client = self._client_for(profile)
            if session.model != profile.model or session.profile != profile.name:
                session.model = profile.model
                session.profile = profile.name
                session.save(self.cfg.sessions_dir)
        else:
            client = self.client
        # Initial status message we'll keep editing into a small live "card".
        start_ts = time.time()
        try:
            status_msg = self.api.send_message(
                chat_id, "🤖 Suzu sedang berfikir…", reply_to=reply_to
            )
        except TelegramError as e:
            log.warning("could not send status message: %s", e)
            status_msg = None
        status_msg_id: Optional[int] = (status_msg or {}).get("message_id")

        # Throttled status editor — Telegram rate-limits edits.
        last_edit_ts = [0.0]
        spin = [0]
        steps = [0]
        last_text = [""]

        def render(phase: str, detail: str = "", *, force: bool = False) -> None:
            if status_msg_id is None:
                return
            now = time.time()
            if not force and now - last_edit_ts[0] < 0.7:
                return
            frame = _SPINNER[spin[0] % len(_SPINNER)]
            spin[0] += 1
            elapsed = int(now - start_ts)
            lines = [f"{frame} *{phase}*"]
            if detail:
                lines.append(f"`{detail[:120]}`")
            lines.append(f"🧩 langkah {steps[0]} · ⏱️ {elapsed}s")
            body = "\n".join(lines)
            if body == last_text[0]:
                return
            last_text[0] = body
            last_edit_ts[0] = now
            try:
                self.api.edit_message_text(
                    chat_id, status_msg_id, body, parse_mode="Markdown"
                )
            except TelegramError as e:
                log.debug("edit status failed: %s", e)

        # Snapshot files in workspace before the turn so we can detect new ones.
        pre_files = _snapshot_files(Path(session.workspace))

        def on_event(kind: str, payload: dict[str, Any]) -> None:
            if kind == "thinking":
                render("Menganalisis…", force=steps[0] == 0)
            elif kind == "tool_start":
                steps[0] += 1
                name = payload.get("name", "tool")
                render(_phase_label(name), force=True)
            elif kind == "tool_end":
                name = payload.get("name", "tool")
                summary = summarize_tool_output(name, payload.get("output", ""))
                render(_phase_label(name), summary, force=True)
            elif kind == "error":
                render("Ralat", payload.get("error", "error"), force=True)

        ctx = ToolContext(workspace=Path(session.workspace), debug=self.cfg.debug)
        with TypingPing(self.api, chat_id):
            try:
                final_text = run_turn(
                    client,
                    self.registry,
                    ctx,
                    self.cfg,
                    session,
                    user_message=text,
                    on_event=on_event,
                )
            except APIError as e:
                final_text = f"API error: {e}"
            except Exception as e:  # noqa: BLE001
                log.exception("agent crashed")
                final_text = f"Internal error: {e}"

        # Replace the status message with the first chunk of the answer.
        chunks = list(_chunk_message(final_text or "(empty response)"))
        first = chunks[0]
        try:
            if status_msg_id is not None:
                self.api.edit_message_text(chat_id, status_msg_id, first)
            else:
                self.api.send_message(chat_id, first)
        except TelegramError as e:
            log.warning("final edit failed (%s) — sending as new message", e)
            self.api.send_message(chat_id, first)
        for extra in chunks[1:]:
            try:
                self.api.send_message(chat_id, extra)
            except TelegramError as e:
                log.warning("send chunk failed: %s", e)

        # Send files back to the user. We deliberately do NOT echo every changed
        # file (that spammed modified images / smali / class files). Priority:
        #   1. Files the assistant explicitly handed over via the `deliver` tool.
        #   2. Fallback: a freshly produced final APK/AAB, if the assistant
        #      forgot to call `deliver`.
        to_send: list[Path] = []
        seen: set[Path] = set()
        for raw in ctx.deliverables:
            fp = Path(raw)
            if fp.is_file() and fp not in seen:
                seen.add(fp)
                to_send.append(fp)
        if not to_send:
            post_files = _snapshot_files(Path(session.workspace))
            for fp in _new_final_artifacts(pre_files, post_files)[:2]:
                if fp not in seen:
                    seen.add(fp)
                    to_send.append(fp)

        for fp in to_send:
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if size == 0 or size > 45 * 1024 * 1024:
                self.api.send_message(
                    chat_id,
                    f"⚠️ `{fp.name}` ({_human_size(size)}) tak boleh dihantar terus "
                    f"(had Telegram ~50 MB). Fail ada di `{fp}` pada server.",
                    parse_mode="Markdown",
                )
                continue
            try:
                self.api.send_document(
                    chat_id, fp, caption=f"✅ {fp.name} ({_human_size(size)})"
                )
            except TelegramError as e:
                log.warning("send_document failed for %s: %s", fp, e)


# --------------------------------------------------------------------------- #
# Workspace helpers
# --------------------------------------------------------------------------- #


# Only *final* build artefacts are auto-sent as a fallback. Everything else
# (decompiled smali/java, resources, modified images, class files, logs) is
# intermediate and must be handed over explicitly via the `deliver` tool.
_FINAL_ARTIFACT_EXTS = {".apk", ".aab", ".apks", ".xapk"}


def _snapshot_files(root: Path) -> dict[Path, float]:
    """Return ``{path: mtime}`` for all files under ``root``."""
    snap: dict[Path, float] = {}
    if not root.exists():
        return snap
    for p in root.rglob("*"):
        if p.is_file():
            try:
                snap[p] = p.stat().st_mtime
            except OSError:
                pass
    return snap


def _new_final_artifacts(pre: dict[Path, float], post: dict[Path, float]) -> list[Path]:
    """Newly produced final build artefacts (APK/AAB), newest first.

    Used only as a fallback when the assistant didn't explicitly ``deliver``
    anything — so the user still gets the final APK without the old spam of
    every changed image/class file.
    """
    new: list[Path] = []
    for p, mtime in post.items():
        if p in pre and pre[p] >= mtime:
            continue
        # Skip uploads/* — that's where the user's own uploads land.
        try:
            if "uploads" in p.relative_to(p.anchor).parts:
                continue
        except ValueError:
            pass
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in _FINAL_ARTIFACT_EXTS:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0 or size > 45 * 1024 * 1024:
            continue
        new.append(p)
    new.sort(key=lambda x: post.get(x, 0.0), reverse=True)
    return new


def _update_chat_key(update: dict[str, Any]) -> int:
    """Routing key for the per-chat worker pool.

    Updates from the same chat share a key (and thus a FIFO worker thread);
    everything that lacks a chat id falls back to 0.
    """
    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return int(chat["id"])
    cb = update.get("callback_query") or {}
    cb_msg = cb.get("message") or {}
    cb_chat = cb_msg.get("chat") or {}
    if cb_chat.get("id") is not None:
        return int(cb_chat["id"])
    frm = cb.get("from") or {}
    if frm.get("id") is not None:
        return int(frm["id"])
    return 0


def _download_error_hint(name: str, size: int, err: Exception) -> str:
    """Human-readable, Malay-friendly reason a Telegram download failed."""
    msg = str(err)
    if "too big" in msg.lower() or (size and size > TELEGRAM_GETFILE_LIMIT):
        sz = f" ({_human_size(size)})" if size else ""
        return (
            f"`{name}`{sz} terlalu besar untuk Telegram Bot API "
            f"(had {_human_size(TELEGRAM_GETFILE_LIMIT)})."
        )
    return f"`{name}`: {msg[:160]}"


def _safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("..", "_")
    return name[:200] or f"file_{int(time.time())}"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------- #
# Message chunking
# --------------------------------------------------------------------------- #


def _chunk_message(text: str, *, limit: int = MAX_MSG_CHARS) -> Iterable[str]:
    text = text or ""
    if len(text) <= limit:
        yield text
        return
    # Try to split on paragraph then line then word boundaries.
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        piece, remaining = remaining[:cut].rstrip(), remaining[cut:].lstrip()
        if piece:
            yield piece


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("SUZU_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        bot_cfg = BotConfig.from_env()
        cfg = Config.load()
    except TelegramError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if not cfg.api_key:
        print(
            "AI_API_KEY is empty.  Set it in the env file pointed at by "
            "SUZU_ENV_FILE before running the bot.",
            file=sys.stderr,
        )
        return 2
    try:
        bot = Bot(bot_cfg, cfg)
    except TelegramError as e:
        print(f"failed to initialise Telegram client: {e}", file=sys.stderr)
        return 3
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("interrupted — shutting down")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
