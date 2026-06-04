"""Minimal OpenAI-compatible chat client using only the Python stdlib.

We intentionally avoid external dependencies so ``install.sh`` does not need to
provision pip wheels on a fresh VPS.  The client supports:

* ``POST /chat/completions`` with ``tools`` (function calling)
* Streaming responses (``stream: true``) — yields delta dicts
* Automatic retry on transient network failures
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator


class APIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "stream": self.stream,
        }
        if self.tools:
            payload["tools"] = self.tools
            if self.tool_choice is not None:
                payload["tool_choice"] = self.tool_choice
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        payload.update(self.extra)
        return payload


class ChatClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # --------------------------------------------------------------------- #
    # Low-level
    # --------------------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "suzu-chat-ai/0.1",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, path: str, payload: dict[str, Any], stream: bool) -> urllib.request.addinfourl:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        # For streaming, use a shorter timeout for the initial connection +
        # first byte so a hanging model provider fails fast (~45s) instead of
        # blocking the user for the full self.timeout (often 600s).
        connect_timeout = min(self.timeout, 45.0) if stream else self.timeout
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = urllib.request.urlopen(req, timeout=connect_timeout)
                if stream:
                    # Once connected, allow long reads (tool calls etc.).
                    try:
                        resp.fp.raw._sock.settimeout(self.timeout)  # type: ignore[union-attr]
                    except (AttributeError, OSError):
                        pass
                return resp
            except urllib.error.HTTPError as e:
                # 4xx is not retryable.
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if 400 <= e.code < 500:
                    raise APIError(
                        f"HTTP {e.code} from {url}: {body[:400]}",
                        status=e.code,
                        body=body,
                    ) from e
                last_err = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
            time.sleep(1.5 * (attempt + 1))
        raise APIError(f"Network error contacting {url}: {last_err!r}") from last_err

    # --------------------------------------------------------------------- #
    # High-level: non-streaming
    # --------------------------------------------------------------------- #
    def chat(self, req: ChatRequest) -> dict[str, Any]:
        req.stream = False
        resp = self._post("/chat/completions", req.to_payload(), stream=False)
        with resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise APIError(f"Invalid JSON from server: {e}; body={raw[:400]}") from e

    # --------------------------------------------------------------------- #
    # High-level: streaming
    # --------------------------------------------------------------------- #
    def stream(self, req: ChatRequest) -> Iterator[dict[str, Any]]:
        req.stream = True
        resp = self._post("/chat/completions", req.to_payload(), stream=True)
        with resp:
            for line in _iter_sse(resp):
                if not line:
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                else:
                    data = line.strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        return
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    # Tolerate keep-alive comments etc.
                    continue


def _iter_sse(resp: Iterable[bytes]) -> Iterator[str]:
    buf = b""
    for chunk in resp:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace").rstrip("\r")
    if buf:
        yield buf.decode("utf-8", errors="replace").rstrip("\r")


# --------------------------------------------------------------------------- #
# Streaming delta accumulator
# --------------------------------------------------------------------------- #


class DeltaAccumulator:
    """Reassembles a streamed assistant message from OpenAI-style deltas."""

    def __init__(self) -> None:
        self.content: str = ""
        self.tool_calls: list[dict[str, Any]] = []
        self.finish_reason: str | None = None
        self.role: str = "assistant"

    def push(self, event: dict[str, Any]) -> str:
        """Apply ``event`` and return any newly added textual content."""

        choices = event.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        added = ""
        if "role" in delta and isinstance(delta["role"], str):
            self.role = delta["role"]
        if "content" in delta and isinstance(delta["content"], str):
            added = delta["content"]
            self.content += added
        tcs = delta.get("tool_calls") or []
        for tc in tcs:
            idx = tc.get("index", 0)
            while len(self.tool_calls) <= idx:
                self.tool_calls.append(
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
            slot = self.tool_calls[idx]
            if "id" in tc and tc["id"]:
                slot["id"] = tc["id"]
            if "type" in tc and tc["type"]:
                slot["type"] = tc["type"]
            fn = tc.get("function") or {}
            if "name" in fn and fn["name"]:
                slot["function"]["name"] += fn["name"]
            if "arguments" in fn and fn["arguments"]:
                slot["function"]["arguments"] += fn["arguments"]
        fr = choices[0].get("finish_reason")
        if fr:
            self.finish_reason = fr
        return added

    def to_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content or None}
        if self.tool_calls:
            # Drop empty entries (some providers emit them).
            cleaned = [tc for tc in self.tool_calls if tc.get("function", {}).get("name")]
            if cleaned:
                msg["tool_calls"] = cleaned
        return msg
