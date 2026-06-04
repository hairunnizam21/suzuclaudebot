# Suzu AI — Telegram bot + VPS panel + terminal AI assistant

`suzuclaudebot` provisions a fresh Ubuntu VPS with:

- A **Telegram bot** (`suzu-telegram-bot`) — chat with the AI and do real APK
  work straight from your phone: send an `.apk`, ask it to decompile / patch /
  recompile / sign, get the new APK back. Pick which AI model to use with
  `/models`.
- A **terminal chat AI** (`suzu-chat-ai`) — like opencode / aider / claude-code,
  but specialised for **APK reverse engineering**: build / decompile / recompile
  APKs of any framework (Java, Kotlin, Native, Flutter, React Native, Unity,
  Xamarin, …), patch resources, sign APKs, run general RE workflows.
- An interactive **admin TUI** (`suzu-admin`, the *ClaudeSuzuBot* menu) — auto-
  launches on SSH login; manage env, users, a multi-provider **Model Manager**,
  backups, **Self-Update** from GitHub, *and* jump into the chat AI.
- (Optional) the legacy [suzu-ai-web](https://github.com/hairunnizam21/suzu-ai-web)
  frontend if you set `SUZU_INSTALL_WEB=1`.

## Install (fresh Ubuntu VPS) — all-in-one, one command

The installer is **all-in-one**: a single command installs every tool (JDK,
apktool, aapt/aapt2, jadx, dex2jar, smali/baksmali, zipalign, apksigner), the
Telegram bot, the admin TUI, and a 24/7 systemd service. It enables the
`universe` apt repo automatically, installs packages with per-package fallback
(one bad package never aborts the rest), heals missing tools, and prints a
✅/❌ verification summary at the end.

### Recommended: non-interactive one-liner (Telegram bot)

Pass your settings as environment variables so there are no prompts:

```bash
curl -fsSL https://raw.githubusercontent.com/hairunnizam21/suzuclaudebot/setup/install.sh | sudo -E env \
  TELEGRAM_BOT_TOKEN='123456:your-bot-token' \
  TELEGRAM_ALLOWED_USER_IDS='5547807879' \
  AI_API_BASE_URL='http://103.200.216.137:3000/v1' \
  AI_API_KEY='your_api_key' \
  AI_DEFAULT_MODEL='claude-sonnet-4-6' \
  bash
```

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather) (`/newbot`).
- `TELEGRAM_ALLOWED_USER_IDS` — comma-separated admin Telegram id(s). Other
  users who `/start` go to *pending* until an admin approves them.
- `AI_API_BASE_URL` / `AI_API_KEY` / `AI_DEFAULT_MODEL` — your OpenAI-compatible
  endpoint. (You can add more providers later via the Model Manager.)

### Alternative: git clone (works even if the repo is private)

```bash
git clone -b setup https://github.com/hairunnizam21/suzuclaudebot.git /opt/suzu-src
cd /opt/suzu-src
sudo -E env \
  TELEGRAM_BOT_TOKEN='123456:your-bot-token' \
  TELEGRAM_ALLOWED_USER_IDS='5547807879' \
  AI_API_BASE_URL='http://103.200.216.137:3000/v1' \
  AI_API_KEY='your_api_key' \
  bash install.sh
```

### After install

1. Telegram → open your bot → `/start` (you're admin if your id is in
   `TELEGRAM_ALLOWED_USER_IDS`). Use `/models` to pick a model.
2. SSH into the server → the **ClaudeSuzuBot** menu auto-launches → **Model
   Manager** to add more providers (Claude / Deepseek / GPT …), **Self-Update**
   to pull the latest scripts from GitHub. Choose `0) Exit to shell` for a
   normal prompt (or `export SUZU_NO_AUTOLAUNCH=1`).

What you get:

- `python3` + venv + `pip`
- `openjdk-17`, `apktool`, `aapt`/`aapt2`, `zipalign`, `apksigner`
- Reverse-engineering extras: `jadx`, `dex2jar`, `smali`/`baksmali`, `file`,
  `binutils` (for `strings`)
- A debug keystore (used by the chat AI to sign rebuilt APKs)
- `/usr/local/bin/suzu-admin`   → admin TUI
- `/usr/local/bin/suzu-chat-ai` → terminal chat AI
- An env file at `/etc/suzu-panel/.env` (or `/var/www/suzu-ai-web/.env` when
  `SUZU_INSTALL_WEB=1`) holding `AI_API_KEY`, `AI_API_BASE_URL`,
  `AI_DEFAULT_MODEL`, `SUZU_KEYSTORE`, `SUZU_STATE_DIR`, `SUZU_PANEL_DIR`.

### Installer flags

```bash
SUZU_INSTALL_WEB=1                # also install nginx + PM2 + Node + suzu-ai-web
SUZU_REPO_URL=…                   # suzu-ai-web repo URL
SUZU_BRANCH=main
SUZU_INSTALL_DIR=/var/www/suzu-ai-web
SUZU_PANEL_DIR=/opt/suzu-panel
SUZU_STATE_DIR=/var/lib/suzu-ai
SUZU_ENV_FILE=/etc/suzu-panel/.env
SUZU_LETSENCRYPT_EMAIL=admin@example.com
sudo -E bash install.sh
```

## The chat AI (`suzu-chat-ai`)

Run from anywhere on the VPS:

```bash
suzu-chat-ai            # resume the most recent session (or start fresh)
suzu-chat-ai --new      # start a new session
suzu-chat-ai --list     # list all sessions
suzu-chat-ai --resume <SID>
```

Or open it from the admin TUI: `suzu-admin` → **19) Chat AI**.

Inside the chat, the AI can call **31 tools** including:

- **APK**: `detect_apk_type`, `apk_decompile`, `apk_recompile`, `apk_zipalign`,
  `apk_sign`, `apk_verify_signature`, `apk_aapt_dump`, `apk_build_full`
- **Projects**: `detect_project`, `build_project` (auto-detects Gradle,
  Flutter, React Native, apktool projects, Xamarin)
- **Reverse engineering**: `jadx_decompile`, `dex2jar`, `smali_disasm`,
  `smali_asm`, `strings`, `hexdump`, `file_type`
- **Files**: `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`,
  `grep`, `mkdir`, `move`, `remove`
- **Shell + Python**: `shell`, `exec`, `venv_create`, `pip_install`,
  `run_python`

### Slash commands

| Command                | What it does                                              |
| ---------------------- | --------------------------------------------------------- |
| `/menu`                | Exit chat and return to `suzu-admin` menu (resumable)     |
| `/exit`, `/quit`       | Quit the chat                                             |
| `/clear`               | Clear conversation history (keep system prompt)           |
| `/model <name>`        | Switch model for this session                             |
| `/projects`            | List all sessions                                         |
| `/new`                 | Create a new blank session                                |
| `/resume [SID]`        | Resume a session (latest if SID omitted)                  |
| `/workspace`           | Print the workspace path of this session                  |
| `/title <text>`        | Rename the current session                                |
| `/cost`                | Quick history size summary                                |
| `/help`                | Show this help                                            |

Sessions persist on disk at `SUZU_STATE_DIR/sessions/*.json`. Each session has
a private workspace at `SUZU_STATE_DIR/workspaces/<sid>/` where decompiled
projects and intermediate APKs live.  Exiting with `/menu` leaves all of that
in place — the next time you launch the chat (option 19 in suzu-admin, or
`suzu-chat-ai` with no flags) you pick up exactly where you stopped.

### UI feedback

While the AI is processing, the terminal shows live status with a Unicode
spinner:

- `Thinking…`     when waiting for the model
- `Calling <tool>`     while a tool call is being streamed
- `Running <tool>(…)`  while a tool is executing on disk
- `<tool> done`        when the tool returns

All animations honour `NO_COLOR` and degrade gracefully on non-TTY terminals.

## Admin TUI (`suzu-admin`)

After install, just type:

```bash
suzu-admin
```

The menu lets you:

1. Update domain (+ reissue SSL)
2. Update AI base URL
3. Update AI API key
4. Update default model
5. Set the global daily token limit (applies to all users)
6. Set a per-user daily token limit (for donors)
7-11. Premium / users / token resets
12-15. Restart / logs / git pull + rebuild / view env
16. Admin token (show / regenerate)
17-18. Backup / restore
19. **Chat AI** — open the terminal AI assistant

The default daily limit is **2,000,000 tokens per user**, reset at UTC
midnight. Per-user limits override the default for a given user only.

## Re-installing / new VPS / updating

When your VPS expires or you migrate, spin up a new Ubuntu box and run the
one-liner above. The installer is idempotent — re-running it on an existing
install just refreshes the panel scripts and re-verifies the toolchain, while
preserving your `.env`, model profiles, keystore and session data.

To update an existing server in place without re-running the installer, open
`suzu-admin` → **Self-Update** (pulls the latest scripts from GitHub and
restarts the bot).

## Files

| Path                                | What it is                                |
| ----------------------------------- | ----------------------------------------- |
| `install.sh`                        | One-shot Ubuntu provisioner               |
| `suzu-admin.sh`                     | Admin TUI (`/usr/local/bin/suzu-admin`)   |
| `bin/suzu-chat-ai`                  | Chat AI launcher                          |
| `chat_ai/`                          | Python agent (stdlib-only, no pip deps)   |
| `chat_ai/agent.py`                  | Interactive loop                          |
| `chat_ai/api.py`                    | OpenAI-compatible streaming client        |
| `chat_ai/animations.py`             | Spinners, status banners                  |
| `chat_ai/state.py`                  | Session persistence                       |
| `chat_ai/prompts.py`                | System prompt                             |
| `chat_ai/tools/*.py`                | Tool handlers (shell, files, APK, …)      |

## License

MIT.
