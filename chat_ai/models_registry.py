"""Multi-provider model registry.

A *profile* bundles everything needed to talk to one model on one provider:

    name      human label shown in menus, e.g. "Claude Sonnet 4.6"
    base_url  OpenAI-compatible endpoint, e.g. http://1.2.3.4:3000/v1
    model     model id sent in the request, e.g. claude-sonnet-4-6
    api_key   bearer token for that endpoint

Profiles are stored as JSON at ``<state_dir>/models.json`` so the suzu-admin
TUI (which adds/edits them) and the Telegram bot (which reads them) share one
source of truth.  The bot re-reads the file when its mtime changes, so admin
edits sync live without a restart.

The module is also runnable as a CLI (``python3 -m chat_ai.models_registry``)
so the bash TUI can manage profiles without re-implementing JSON handling.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


REGISTRY_VERSION = 1


def _slug(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")


@dataclass
class ModelProfile:
    name: str
    base_url: str
    model: str
    api_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelProfile":
        return cls(
            name=str(d.get("name", "")).strip(),
            base_url=str(d.get("base_url", "")).strip().rstrip("/"),
            model=str(d.get("model", "")).strip(),
            api_key=str(d.get("api_key", "")).strip(),
        )

    @property
    def key(self) -> str:
        """Stable id used in callbacks / lookups (safe for Telegram data)."""
        return _slug(self.name) or _slug(self.model) or "profile"

    def masked_key(self) -> str:
        k = self.api_key
        if not k:
            return "<none>"
        if len(k) <= 8:
            return "•" * len(k)
        return f"{k[:4]}…{k[-4:]}"


class ModelRegistry:
    def __init__(self, path: Path, profiles: list[ModelProfile], default: str = ""):
        self.path = path
        self.profiles = profiles
        self.default = default
        self._mtime: float = 0.0

    # -- persistence -------------------------------------------------------- #

    @classmethod
    def load(cls, state_dir: Path, *, seed: Optional[ModelProfile] = None) -> "ModelRegistry":
        path = Path(state_dir) / "models.json"
        reg = cls(path, [], "")
        if path.is_file():
            reg._read()
        if not reg.profiles and seed is not None and (seed.base_url and seed.model):
            seed.name = seed.name or seed.model
            reg.profiles = [seed]
            reg.default = seed.name
            reg.save()
        return reg

    def _read(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        profs = [ModelProfile.from_dict(p) for p in data.get("profiles", [])]
        self.profiles = [p for p in profs if p.name and p.base_url and p.model]
        self.default = str(data.get("default", "")).strip()
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def reload_if_changed(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self._mtime:
            self._read()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "default": self.default,
            "profiles": [p.to_dict() for p in self.profiles],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    # -- queries ------------------------------------------------------------ #

    def list(self) -> list[ModelProfile]:
        return list(self.profiles)

    def get(self, name_or_key: str) -> Optional[ModelProfile]:
        if not name_or_key:
            return None
        needle = name_or_key.strip().lower()
        for p in self.profiles:
            if p.name.lower() == needle or p.key == needle:
                return p
        # second pass: match by raw model id
        for p in self.profiles:
            if p.model.lower() == needle:
                return p
        return None

    def default_profile(self) -> Optional[ModelProfile]:
        if self.default:
            p = self.get(self.default)
            if p:
                return p
        return self.profiles[0] if self.profiles else None

    # -- mutations ---------------------------------------------------------- #

    def add(self, profile: ModelProfile) -> None:
        existing = self.get(profile.name)
        if existing is not None:
            existing.base_url = profile.base_url
            existing.model = profile.model
            existing.api_key = profile.api_key
        else:
            self.profiles.append(profile)
        if not self.default:
            self.default = profile.name
        self.save()

    def remove(self, name_or_key: str) -> bool:
        p = self.get(name_or_key)
        if p is None:
            return False
        self.profiles = [x for x in self.profiles if x is not p]
        if self.default == p.name:
            self.default = self.profiles[0].name if self.profiles else ""
        self.save()
        return True

    def set_default(self, name_or_key: str) -> bool:
        p = self.get(name_or_key)
        if p is None:
            return False
        self.default = p.name
        self.save()
        return True


# --------------------------------------------------------------------------- #
# CLI (used by suzu-admin)
# --------------------------------------------------------------------------- #


def _seed_from_env() -> Optional[ModelProfile]:
    from .config import Config

    cfg = Config.load()
    if cfg.api_base_url and cfg.default_model:
        return ModelProfile(
            name=cfg.default_model,
            base_url=cfg.api_base_url,
            model=cfg.default_model,
            api_key=cfg.api_key,
        )
    return None


def _state_dir() -> Path:
    from .config import Config

    return Config.load().state_dir


def _print_list(reg: ModelRegistry) -> None:
    if not reg.profiles:
        print("(no model profiles yet)")
        return
    for i, p in enumerate(reg.profiles, 1):
        star = "*" if p.name == reg.default else " "
        print(f"{star} {i}) {p.name}")
        print(f"      base_url : {p.base_url}")
        print(f"      model    : {p.model}")
        print(f"      api_key  : {p.masked_key()}")


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="suzu-models", description="Manage Suzu model profiles")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list profiles")
    sub.add_parser("seed", help="create a profile from the current .env if registry is empty")
    sub.add_parser("get-default", help="print the default profile name")

    a = sub.add_parser("add", help="add or update a profile")
    a.add_argument("--name", required=True)
    a.add_argument("--base-url", required=True)
    a.add_argument("--model", required=True)
    a.add_argument("--api-key", default="")

    r = sub.add_parser("remove", help="remove a profile")
    r.add_argument("--name", required=True)

    d = sub.add_parser("set-default", help="set the default profile")
    d.add_argument("--name", required=True)

    args = ap.parse_args(argv)
    state_dir = _state_dir()

    if args.cmd == "seed":
        reg = ModelRegistry.load(state_dir, seed=_seed_from_env())
        _print_list(reg)
        return 0

    reg = ModelRegistry.load(state_dir)

    if args.cmd == "list":
        _print_list(reg)
        return 0
    if args.cmd == "get-default":
        print(reg.default or "")
        return 0
    if args.cmd == "add":
        reg.add(
            ModelProfile(
                name=args.name.strip(),
                base_url=args.base_url.strip().rstrip("/"),
                model=args.model.strip(),
                api_key=args.api_key.strip(),
            )
        )
        print(f"saved profile: {args.name}")
        return 0
    if args.cmd == "remove":
        ok = reg.remove(args.name)
        print("removed" if ok else "not found")
        return 0 if ok else 1
    if args.cmd == "set-default":
        ok = reg.set_default(args.name)
        print("default set" if ok else "not found")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
