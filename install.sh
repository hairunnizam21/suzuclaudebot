#!/usr/bin/env bash
# Suzu AI — one-shot VPS installer
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/hairunnizam21/suzuclaudebot/setup/install.sh | sudo bash
# or (interactive):
#   sudo bash install.sh
#
# Environment knobs:
#   SUZU_INSTALL_WEB=1   also install the legacy suzu-ai-web frontend
#                        (nginx + PM2 + Node). Default 0 = chat-only.
#   SUZU_PANEL_DIR=/opt/suzu-panel        where the panel scripts live
#   SUZU_STATE_DIR=/var/lib/suzu-ai       where sessions/workspaces live
#   SUZU_ENV_FILE=/etc/suzu-panel/.env    fallback env when web is not installed

set -euo pipefail

REPO_URL="${SUZU_REPO_URL:-https://github.com/hairunnizam21/suzu-ai-web.git}"
INSTALL_DIR="${SUZU_INSTALL_DIR:-/var/www/suzu-ai-web}"
ADMIN_LINK="${SUZU_ADMIN_LINK:-/usr/local/bin/suzu-admin}"
CHAT_LINK="${SUZU_CHAT_LINK:-/usr/local/bin/suzu-chat-ai}"
BOT_LINK="${SUZU_BOT_LINK:-/usr/local/bin/suzu-telegram-bot}"
PANEL_DIR="${SUZU_PANEL_DIR:-/opt/suzu-panel}"
STATE_DIR="${SUZU_STATE_DIR:-/var/lib/suzu-ai}"
INSTALL_WEB="${SUZU_INSTALL_WEB:-0}"
INSTALL_BOT="${SUZU_INSTALL_BOT:-1}"
NODE_MAJOR="${SUZU_NODE_MAJOR:-20}"
BRANCH="${SUZU_BRANCH:-main}"

# Where this installer (and the script_ai_panel checkout) lives at runtime.
# When piped via curl | bash, this falls back to a temp clone done below.
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_grn()   { printf "\033[32m%s\033[0m\n" "$*"; }
c_yel()   { printf "\033[33m%s\033[0m\n" "$*"; }
c_cyn()   { printf "\033[36m%s\033[0m\n" "$*"; }
c_bld()   { printf "\033[1m%s\033[0m\n" "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  c_red "Please run as root (sudo bash install.sh)"
  exit 1
fi

c_bld "==> Suzu AI installer"
c_cyn "Panel dir:   $PANEL_DIR"
c_cyn "State dir:   $STATE_DIR"
if [ "$INSTALL_WEB" = "1" ]; then
  c_cyn "Web install: ENABLED (suzu-ai-web at $INSTALL_DIR)"
  c_cyn "Repo:        $REPO_URL (branch $BRANCH)"
else
  c_cyn "Web install: disabled (set SUZU_INSTALL_WEB=1 to enable)"
fi

apt_install() {
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

c_bld "==> Updating APT"
DEBIAN_FRONTEND=noninteractive apt-get update -qq

c_bld "==> Installing system packages"
CORE_PKGS="ca-certificates curl gnupg git \
  openjdk-17-jre-headless apktool zipalign apksigner unzip zip sqlite3 \
  build-essential whiptail file binutils \
  python3 python3-venv python3-pip"
# APK reverse-engineering extras. Best-effort via apt: a missing package
# falls back to manual install from upstream releases below.
RE_PKGS="aapt aapt2"
apt_install $CORE_PKGS
for p in $RE_PKGS; do
  if ! apt_install "$p" 2>/dev/null; then
    c_yel "  optional package not available: $p (skipping)"
  fi
done

# nginx/certbot only needed if we still ship the legacy web frontend.
if [ "$INSTALL_WEB" = "1" ]; then
  apt_install nginx certbot python3-certbot-nginx
fi

# -----------------------------------------------------------------------------
# Manual installs for RE tools that are no longer in Ubuntu repos (22.04+).
# Each block is idempotent — re-running install.sh just refreshes paths.
# -----------------------------------------------------------------------------
# Keep RE tools OUTSIDE $PANEL_DIR: in curl|bash mode the panel dir is later
# `rm -rf`'d and re-cloned, which would wipe anything we install under it.
TOOLS_DIR="${SUZU_TOOLS_DIR:-/opt/suzu-tools}"
mkdir -p "$TOOLS_DIR"

install_apktool() {
  # The apt apktool on Ubuntu 22.04 is 2.5.x and fails to rebuild many modern
  # APKs (Material You / aapt2 issues). Prefer a recent upstream jar and shadow
  # the apt binary via /usr/local/bin (which precedes /usr/bin on PATH). If the
  # download fails we keep whatever apt installed.
  c_bld "==> Installing apktool (iBotPeaches/Apktool)"
  local ver url
  ver="${SUZU_APKTOOL_VER:-}"
  if [ -z "$ver" ]; then
    ver="$(curl -fsSL https://api.github.com/repos/iBotPeaches/Apktool/releases/latest 2>/dev/null \
      | grep -oE '"tag_name": "v[0-9.]+"' | head -1 | grep -oE '[0-9.]+')" || true
  fi
  [ -z "$ver" ] && ver="3.0.2"
  url="https://github.com/iBotPeaches/Apktool/releases/download/v${ver}/apktool_${ver}.jar"
  mkdir -p "$TOOLS_DIR/apktool"
  if curl -fsSL -o "$TOOLS_DIR/apktool/apktool.jar" "$url"; then
    cat > /usr/local/bin/apktool <<EOF
#!/usr/bin/env bash
exec java -jar "$TOOLS_DIR/apktool/apktool.jar" "\$@"
EOF
    chmod +x /usr/local/bin/apktool
    hash -r
    c_grn "  apktool -> $(apktool --version 2>&1 | head -1)"
  else
    c_yel "  could not fetch apktool $ver upstream — using apt version: $(command -v apktool || echo none)"
  fi
}

install_jadx() {
  if command -v jadx >/dev/null 2>&1; then
    c_yel "  jadx already installed: $(command -v jadx)"
    return
  fi
  c_bld "==> Installing jadx (skylot/jadx)"
  local url
  url="$(curl -fsSL https://api.github.com/repos/skylot/jadx/releases/latest 2>/dev/null \
    | grep -oE '"browser_download_url": "[^"]*jadx-[0-9][^"]*\.zip"' \
    | grep -v 'gui' | head -1 | cut -d'"' -f4)" || true
  if [ -z "$url" ]; then
    # GitHub API rate-limited / offline — fall back to a known-good release.
    local jver="${SUZU_JADX_VER:-1.5.1}"
    url="https://github.com/skylot/jadx/releases/download/v${jver}/jadx-${jver}.zip"
    c_yel "  GitHub API unavailable — using pinned jadx ${jver}"
  fi
  rm -rf "$TOOLS_DIR/jadx"
  mkdir -p "$TOOLS_DIR/jadx"
  curl -fsSL -o "$TOOLS_DIR/jadx.zip" "$url"
  ( cd "$TOOLS_DIR/jadx" && unzip -q "$TOOLS_DIR/jadx.zip" )
  rm -f "$TOOLS_DIR/jadx.zip"
  chmod +x "$TOOLS_DIR/jadx/bin/jadx" "$TOOLS_DIR/jadx/bin/jadx-gui" 2>/dev/null || true
  ln -sf "$TOOLS_DIR/jadx/bin/jadx"     /usr/local/bin/jadx
  ln -sf "$TOOLS_DIR/jadx/bin/jadx-gui" /usr/local/bin/jadx-gui
  c_grn "  jadx -> $(jadx --version 2>&1 | head -1)"
}

install_dex2jar() {
  if command -v d2j-dex2jar >/dev/null 2>&1; then
    c_yel "  dex2jar already installed"
    return
  fi
  c_bld "==> Installing dex2jar (pxb1988/dex2jar)"
  local url
  url="$(curl -fsSL https://api.github.com/repos/pxb1988/dex2jar/releases/latest 2>/dev/null \
    | grep -oE '"browser_download_url": "[^"]*\.zip"' | head -1 | cut -d'"' -f4)" || true
  if [ -z "$url" ]; then
    # GitHub API rate-limited / offline — fall back to a known-good release.
    local dver="${SUZU_DEX2JAR_VER:-2.4}"
    url="https://github.com/pxb1988/dex2jar/releases/download/v${dver}/dex-tools-v${dver}.zip"
    c_yel "  GitHub API unavailable — using pinned dex2jar ${dver}"
  fi
  rm -rf "$TOOLS_DIR/dex2jar"
  mkdir -p "$TOOLS_DIR/dex2jar"
  curl -fsSL -o "$TOOLS_DIR/dex2jar.zip" "$url"
  ( cd "$TOOLS_DIR/dex2jar" && unzip -q "$TOOLS_DIR/dex2jar.zip" )
  rm -f "$TOOLS_DIR/dex2jar.zip"
  local bin
  bin="$(find "$TOOLS_DIR/dex2jar" -maxdepth 3 -type d -name "dex-tools-*" | head -1)"
  [ -z "$bin" ] && bin="$(find "$TOOLS_DIR/dex2jar" -maxdepth 3 -name "d2j_invoke.sh" -printf "%h\n" | head -1)"
  if [ -z "$bin" ]; then
    c_yel "  could not locate dex2jar bin/ — skipping"
    return
  fi
  chmod +x "$bin"/*.sh 2>/dev/null || true
  local f n
  for f in "$bin"/d2j-*.sh; do
    [ -e "$f" ] || continue
    n="$(basename "$f" .sh)"
    ln -sf "$f" "/usr/local/bin/$n"
  done
  c_grn "  d2j-dex2jar -> $(command -v d2j-dex2jar)"
}

install_smali() {
  if command -v baksmali >/dev/null 2>&1 && command -v smali >/dev/null 2>&1; then
    c_yel "  smali/baksmali already installed"
    return
  fi
  c_bld "==> Installing smali / baksmali (JesusFreke 2.5.2)"
  local ver="${SUZU_SMALI_VER:-2.5.2}"
  mkdir -p "$TOOLS_DIR/smali"
  local art url
  for art in smali baksmali; do
    url="https://bitbucket.org/JesusFreke/smali/downloads/$art-$ver.jar"
    if ! curl -fsSL --fail -o "$TOOLS_DIR/smali/$art.jar" "$url"; then
      c_yel "  failed to download $art $ver — skipping"
      continue
    fi
    cat > "/usr/local/bin/$art" <<EOF
#!/usr/bin/env bash
exec java -jar "$TOOLS_DIR/smali/$art.jar" "\$@"
EOF
    chmod +x "/usr/local/bin/$art"
  done
  c_grn "  baksmali -> $(baksmali --version 2>&1 | head -1)"
  c_grn "  smali    -> $(smali --version 2>&1 | head -1)"
}

# These upstream fetches are best-effort: a transient GitHub API rate-limit or
# network blip must not abort the whole install (set -e). Guarding each call
# with `|| ...` disables errexit for the function body, so a failure just warns.
install_apktool || c_yel "  apktool upgrade skipped (non-fatal)"
install_jadx    || c_yel "  jadx install skipped (non-fatal)"
install_dex2jar || c_yel "  dex2jar install skipped (non-fatal)"
install_smali   || c_yel "  smali install skipped (non-fatal)"

if [ "$INSTALL_WEB" = "1" ]; then
  if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed 's/v//;s/\..*//')" -lt "$NODE_MAJOR" ]; then
    c_bld "==> Installing Node.js $NODE_MAJOR"
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt_install nodejs
  fi
  if ! command -v pm2 >/dev/null 2>&1; then
    c_bld "==> Installing PM2"
    npm install -g pm2 >/dev/null
  fi

  c_bld "==> Cloning suzu-ai-web to $INSTALL_DIR"
  if [ -d "$INSTALL_DIR/.git" ]; then
    c_yel "Existing checkout detected — pulling latest from $BRANCH"
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
  cd "$INSTALL_DIR"
else
  # Headless / chat-only install. The web tree may still exist from an earlier
  # run; we leave it alone. Just ensure the panel state dir exists so the
  # chat AI has somewhere to put its keystore + workspaces.
  mkdir -p "$INSTALL_DIR" || true
  cd "$INSTALL_DIR" 2>/dev/null || cd /
fi

c_bld "==> Generating debug keystore (for signing recompiled APKs)"
KEYSTORE_DIR="$INSTALL_DIR/server/keystores"
if [ "$INSTALL_WEB" != "1" ]; then
  KEYSTORE_DIR="$STATE_DIR/keystores"
fi
mkdir -p "$KEYSTORE_DIR" "$STATE_DIR/workspaces" "$STATE_DIR/sessions" "$STATE_DIR/logs"
KEYSTORE_PATH="$KEYSTORE_DIR/debug.keystore"
if [ ! -f "$KEYSTORE_PATH" ]; then
  keytool -genkey -v -keystore "$KEYSTORE_PATH" -storepass android \
    -alias androiddebugkey -keypass android -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null
  c_grn "  Created $KEYSTORE_PATH"
else
  c_yel "  Existing keystore reused at $KEYSTORE_PATH"
fi

# Choose where the env file lives. With web install it lives in the app dir;
# otherwise we use /etc/suzu-panel/.env so suzu-admin and the chat AI launcher
# share the same source of truth.
if [ "$INSTALL_WEB" = "1" ]; then
  ENV_FILE="${SUZU_ENV_FILE:-$INSTALL_DIR/.env}"
else
  ENV_FILE="${SUZU_ENV_FILE:-/etc/suzu-panel/.env}"
  mkdir -p "$(dirname "$ENV_FILE")"
fi

if [ ! -f "$ENV_FILE" ] || [ "${SUZU_FORCE_REINIT_ENV:-0}" = "1" ]; then
  c_bld "==> Initial configuration"
  read -rp "Public domain (e.g. suzu-ai.online; leave empty for IP-only):  " DOMAIN
  read -rp "AI base URL [https://core.fiqstr.com/v1]: " AI_API_BASE_URL
  AI_API_BASE_URL="${AI_API_BASE_URL:-https://core.fiqstr.com/v1}"
  read -rp "AI API key: " AI_API_KEY
  read -rp "Default model [fiqstr/claude-sonnet-4.6-thinking-agentic]: " AI_DEFAULT_MODEL
  AI_DEFAULT_MODEL="${AI_DEFAULT_MODEL:-fiqstr/claude-sonnet-4.6-thinking-agentic}"
  read -rp "Firebase project ID (suzu-ai-39dc5): " FIREBASE_PROJECT_ID
  FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-suzu-ai-39dc5}"

  # Generate a random admin token for the REST admin API + APK panel.
  SUZU_ADMIN_TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48)"

  cat > "$ENV_FILE" <<EOF
NODE_ENV=production
PORT=3001
AI_API_KEY=$AI_API_KEY
AI_API_BASE_URL=$AI_API_BASE_URL
AI_DEFAULT_MODEL=$AI_DEFAULT_MODEL
FIREBASE_PROJECT_ID=$FIREBASE_PROJECT_ID
SUZU_DOMAIN=$DOMAIN
SUZU_ADMIN_TOKEN=$SUZU_ADMIN_TOKEN
SUZU_KEYSTORE=$KEYSTORE_PATH
SUZU_STATE_DIR=$STATE_DIR
SUZU_PANEL_DIR=$PANEL_DIR
EOF
  chmod 600 "$ENV_FILE"
  c_grn "  Wrote $ENV_FILE"
  c_cyn "  Generated admin token (for the APK admin panel):"
  printf "    %s\n" "$SUZU_ADMIN_TOKEN"
else
  c_yel "Existing .env preserved at $ENV_FILE — use 'suzu-admin' to edit."
  DOMAIN="$(grep -E '^SUZU_DOMAIN=' "$ENV_FILE" | sed 's/SUZU_DOMAIN=//')"
  # Backfill admin token / panel paths if missing on existing installs.
  if ! grep -qE '^SUZU_ADMIN_TOKEN=' "$ENV_FILE"; then
    SUZU_ADMIN_TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48)"
    printf "SUZU_ADMIN_TOKEN=%s\n" "$SUZU_ADMIN_TOKEN" >> "$ENV_FILE"
    c_cyn "  Backfilled SUZU_ADMIN_TOKEN: $SUZU_ADMIN_TOKEN"
  fi
  grep -qE '^SUZU_KEYSTORE='  "$ENV_FILE" || printf 'SUZU_KEYSTORE=%s\n'  "$KEYSTORE_PATH" >> "$ENV_FILE"
  grep -qE '^SUZU_STATE_DIR=' "$ENV_FILE" || printf 'SUZU_STATE_DIR=%s\n' "$STATE_DIR"     >> "$ENV_FILE"
  grep -qE '^SUZU_PANEL_DIR=' "$ENV_FILE" || printf 'SUZU_PANEL_DIR=%s\n' "$PANEL_DIR"     >> "$ENV_FILE"
fi

if [ "$INSTALL_WEB" = "1" ]; then
  c_bld "==> Installing npm dependencies"
  npm install --no-audit --no-fund
  ( cd client && npm install --no-audit --no-fund )

  c_bld "==> Building client"
  ( cd client && npm run build )
fi

if [ "$INSTALL_WEB" = "1" ]; then
c_bld "==> Configuring Nginx"
NGINX_CONF="/etc/nginx/sites-available/suzu-ai"
SERVER_NAME="${DOMAIN:-_}"
cat > "$NGINX_CONF" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $SERVER_NAME;
    client_max_body_size 250M;

    location / {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
    }
}
NGINX
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/suzu-ai
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if [ -n "${DOMAIN:-}" ]; then
  c_bld "==> Issuing/renewing SSL with certbot for $DOMAIN"
  certbot --nginx --non-interactive --agree-tos \
    --email "${SUZU_LETSENCRYPT_EMAIL:-admin@$DOMAIN}" \
    -d "$DOMAIN" || c_yel "  certbot failed — you can rerun via 'suzu-admin'"
fi

c_bld "==> Starting Suzu AI under PM2"
pm2 delete suzu-ai >/dev/null 2>&1 || true
pm2 start npm --name suzu-ai --cwd "$INSTALL_DIR" -- start
pm2 save
pm2 startup systemd -u root --hp /root >/dev/null || true
fi  # /INSTALL_WEB

c_bld "==> Installing panel scripts to $PANEL_DIR"
mkdir -p "$PANEL_DIR"
if [ -n "$SELF_DIR" ] && [ -d "$SELF_DIR/chat_ai" ]; then
  # Run from a checkout — copy everything alongside this script.
  cp -a "$SELF_DIR/chat_ai"     "$PANEL_DIR/"
  cp -a "$SELF_DIR/bin"         "$PANEL_DIR/"
  cp -a "$SELF_DIR/suzu-admin.sh" "$PANEL_DIR/"
  if [ -f "$SELF_DIR/install.sh" ]; then
    cp -a "$SELF_DIR/install.sh" "$PANEL_DIR/"
  fi
else
  # Curl|bash mode: re-clone this very repo into PANEL_DIR.
  PANEL_REPO="${SUZU_PANEL_REPO:-https://github.com/hairunnizam21/suzuclaudebot.git}"
  PANEL_BRANCH="${SUZU_PANEL_BRANCH:-setup}"
  if [ -d "$PANEL_DIR/.git" ]; then
    git -C "$PANEL_DIR" fetch origin "$PANEL_BRANCH"
    git -C "$PANEL_DIR" checkout "$PANEL_BRANCH"
    git -C "$PANEL_DIR" pull --ff-only origin "$PANEL_BRANCH"
  else
    rm -rf "$PANEL_DIR"
    git clone --branch "$PANEL_BRANCH" "$PANEL_REPO" "$PANEL_DIR"
  fi
fi
chmod +x "$PANEL_DIR/bin/suzu-chat-ai" "$PANEL_DIR/suzu-admin.sh" 2>/dev/null || true

c_bld "==> Installing 'suzu-admin' and 'suzu-chat-ai' commands"
ln -sf "$PANEL_DIR/suzu-admin.sh"      "$ADMIN_LINK"
ln -sf "$PANEL_DIR/bin/suzu-chat-ai"   "$CHAT_LINK"
if [ -f "$PANEL_DIR/bin/suzu-telegram-bot" ]; then
  chmod +x "$PANEL_DIR/bin/suzu-telegram-bot" 2>/dev/null || true
  ln -sf "$PANEL_DIR/bin/suzu-telegram-bot" "$BOT_LINK"
fi

# -----------------------------------------------------------------------------
# Optional: install the Telegram bot systemd unit so the bot keeps running
# in the background and auto-starts at boot.  Set SUZU_INSTALL_BOT=0 to skip.
# The user still needs to populate TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS
# in the env file before the bot will start successfully.
# -----------------------------------------------------------------------------
if [ "$INSTALL_BOT" = "1" ] && [ -f "$PANEL_DIR/systemd/suzu-telegram-bot.service" ]; then
  c_bld "==> Installing Telegram bot systemd unit"
  cp -f "$PANEL_DIR/systemd/suzu-telegram-bot.service" /etc/systemd/system/suzu-telegram-bot.service
  # Make the unit point at the env file we're actually using on this box so
  # ``EnvironmentFile=`` resolves without manual editing.
  if [ -f "$ENV_FILE" ]; then
    sed -i "s|^EnvironmentFile=-/etc/suzu-panel/.env|EnvironmentFile=-$ENV_FILE|" \
      /etc/systemd/system/suzu-telegram-bot.service
  fi
  systemctl daemon-reload
  systemctl enable suzu-telegram-bot.service >/dev/null 2>&1 || true
  if grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE" 2>/dev/null \
     && grep -qE '^TELEGRAM_ALLOWED_USER_IDS=.+' "$ENV_FILE" 2>/dev/null; then
    systemctl restart suzu-telegram-bot.service || c_yel "  bot failed to start — check 'journalctl -u suzu-telegram-bot -e'"
    c_grn "  bot service restarted"
  else
    c_yel "  TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS not set in $ENV_FILE — bot left disabled."
    c_yel "  Add them and run: systemctl restart suzu-telegram-bot"
  fi
fi

# Login hook: export env + auto-open the admin TUI on interactive SSH login.
BASHRC_HOOK_FILE="/etc/profile.d/suzu-admin-banner.sh"
cat > "$BASHRC_HOOK_FILE" <<EOH
# Suzu AI — environment for suzu-admin / suzu-chat-ai / suzu-telegram-bot
export SUZU_ENV_FILE=$ENV_FILE
export SUZU_PANEL_DIR=$PANEL_DIR

# Auto-launch the admin menu ("ClaudeSuzubot" UI) on interactive root login.
# Choose "0) Exit to shell" inside the menu to drop to a normal prompt.
# Opt out permanently: export SUZU_NO_AUTOLAUNCH=1 (banner only), or
# SUZU_NO_ADMIN_BANNER=1 to silence everything.
case "\$-" in
  *i*)
    if [ -t 1 ] && [ "\$(id -u)" -eq 0 ] \\
       && [ -z "\${SUZU_ADMIN_ACTIVE:-}" ] \\
       && [ -z "\${SUZU_NO_AUTOLAUNCH:-}" ] \\
       && command -v suzu-admin >/dev/null 2>&1; then
      SUZU_ADMIN_ACTIVE=1 suzu-admin
    elif [ -t 1 ] && [ -z "\${SUZU_NO_ADMIN_BANNER:-}" ]; then
      printf "\n\033[36m=== Suzu AI VPS ===\033[0m\n"
      printf "Type \033[1msuzu-admin\033[0m to open the admin menu.\n\n"
    fi
    ;;
esac
EOH
chmod +x "$BASHRC_HOOK_FILE"

c_grn "==> Done."
if [ "$INSTALL_WEB" = "1" ]; then
  c_grn "    Web:        https://${DOMAIN:-<server-ip>}/"
fi
c_grn "    Admin TUI:  suzu-admin"
c_grn "    Chat AI:    suzu-chat-ai     (or option 19 inside suzu-admin)"
if [ "$INSTALL_BOT" = "1" ]; then
  c_grn "    Telegram:   suzu-telegram-bot (systemd: suzu-telegram-bot.service)"
fi
