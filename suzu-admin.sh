#!/usr/bin/env bash
# Suzu AI — VPS admin TUI
# Run as: suzu-admin   (after running install.sh)

set -u

INSTALL_DIR="${SUZU_INSTALL_DIR:-/var/www/suzu-ai-web}"
ENV_FILE="${SUZU_ENV_FILE:-$INSTALL_DIR/.env}"
DB_FILE="$INSTALL_DIR/suzu.db"
SERVICE_NAME="suzu-ai"

# Where the panel scripts (chat_ai/, bin/) live. Set by install.sh.
PANEL_DIR="${SUZU_PANEL_DIR:-/opt/suzu-panel}"
CHAT_AI_LAUNCHER="${SUZU_CHAT_AI_LAUNCHER:-$PANEL_DIR/bin/suzu-chat-ai}"

c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_grn()   { printf "\033[32m%s\033[0m\n" "$*"; }
c_yel()   { printf "\033[33m%s\033[0m\n" "$*"; }
c_cyn()   { printf "\033[36m%s\033[0m\n" "$*"; }
c_bld()   { printf "\033[1m%s\033[0m\n" "$*"; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    c_red "Run as root."
    exit 1
  fi
}

require_install() {
  if [ ! -f "$ENV_FILE" ]; then
    c_red "Suzu AI not installed (no env at $ENV_FILE). Run install.sh first."
    exit 1
  fi
}

# Soft check: only fail when calling an action that truly needs the legacy
# suzu-ai-web checkout. Chat AI itself does not require it.
require_web_install() {
  if [ ! -d "$INSTALL_DIR" ]; then
    c_yel "suzu-ai-web is not installed at $INSTALL_DIR."
    c_yel "This action only applies to the legacy web panel."
    return 1
  fi
  return 0
}

# Read a value from .env (returns blank if missing)
env_get() {
  local key="$1"
  awk -F= -v k="$key" '$1==k { sub(/^[^=]*=/, "", $0); print $0 }' "$ENV_FILE" | tail -n 1
}

# Set a value in .env. Creates the line if missing.
env_set() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # Use awk so we don't have to worry about special chars in `sed`
    local tmp
    tmp="$(mktemp)"
    awk -F= -v k="$key" -v v="$value" 'BEGIN{set=0} {
      if ($1==k) { print k"="v; set=1 } else { print $0 }
    } END { if (!set) print k"="v }' "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

restart_service() {
  c_cyn "Restarting $SERVICE_NAME via PM2…"
  pm2 restart "$SERVICE_NAME" >/dev/null && c_grn "  Restarted." || c_red "  PM2 restart failed (is it started?)"
}

sql() {
  sqlite3 "$DB_FILE" "$@"
}

press_enter() {
  printf "\nPress Enter to continue…"
  read -r _ || true
}

action_update_domain() {
  c_bld "=== Update domain ==="
  local current
  current="$(env_get SUZU_DOMAIN)"
  printf "Current domain: %s\n" "${current:-<none>}"
  read -rp "New domain (e.g. suzu-ai.online): " DOMAIN
  [ -z "$DOMAIN" ] && { c_yel "Cancelled."; return; }

  env_set SUZU_DOMAIN "$DOMAIN"

  local NGINX_CONF="/etc/nginx/sites-available/suzu-ai"
  if [ -f "$NGINX_CONF" ]; then
    sed -i -E "s/server_name [^;]+;/server_name $DOMAIN;/" "$NGINX_CONF"
    nginx -t && systemctl reload nginx && c_grn "Nginx reloaded for $DOMAIN"
  else
    c_yel "Nginx config not found at $NGINX_CONF — skipping reload"
  fi

  read -rp "Issue SSL via certbot for $DOMAIN now? [Y/n] " ans
  if [ "${ans:-Y}" != "n" ] && [ "${ans:-Y}" != "N" ]; then
    read -rp "Email for Let's Encrypt [admin@$DOMAIN]: " EMAIL
    EMAIL="${EMAIL:-admin@$DOMAIN}"
    certbot --nginx --non-interactive --agree-tos --email "$EMAIL" -d "$DOMAIN" || c_yel "certbot failed"
  fi
  press_enter
}

action_update_baseurl() {
  c_bld "=== Update AI base URL ==="
  printf "Current: %s\n" "$(env_get AI_API_BASE_URL)"
  read -rp "New base URL: " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  env_set AI_API_BASE_URL "$V"
  restart_service
  press_enter
}

action_update_apikey() {
  c_bld "=== Update AI API key ==="
  printf "Current (masked): %s\n" "$(env_get AI_API_KEY | sed -E 's/(.{4}).*(.{4})/\1…\2/')"
  read -rp "New API key: " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  env_set AI_API_KEY "$V"
  restart_service
  press_enter
}

action_update_model() {
  c_bld "=== Update default model ==="
  printf "Current: %s\n" "$(env_get AI_DEFAULT_MODEL)"
  read -rp "New model id: " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  env_set AI_DEFAULT_MODEL "$V"
  restart_service
  press_enter
}

action_default_limit() {
  c_bld "=== Default daily token limit (for ALL users) ==="
  local current
  current="$(sql "SELECT IFNULL(MIN(tokens_limit_daily),0) FROM users")"
  printf "Smallest current limit in DB: %s\n" "$current"
  read -rp "New default limit for all users (e.g. 2000000): " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  if ! [[ "$V" =~ ^[0-9]+$ ]]; then c_red "Must be an integer."; press_enter; return; fi
  sql "UPDATE users SET tokens_limit_daily=$V;"
  c_grn "Updated $(sql "SELECT changes()") users."
  press_enter
}

action_set_user_limit() {
  c_bld "=== Set token limit for one user (donor) ==="
  read -rp "User id (uid) or email substring: " Q
  [ -z "$Q" ] && { c_yel "Cancelled."; return; }
  local matches
  matches="$(sql "SELECT id, IFNULL(display_name,''), IFNULL(email,''), tokens_used_today, tokens_limit_daily FROM users WHERE id LIKE '%$Q%' OR email LIKE '%$Q%' LIMIT 20" -separator " | ")"
  if [ -z "$matches" ]; then c_red "No users match."; press_enter; return; fi
  printf "Matches (id | name | email | used | limit):\n%s\n" "$matches"
  read -rp "Exact user id to update: " UID2
  [ -z "$UID2" ] && { c_yel "Cancelled."; return; }
  read -rp "New token limit (e.g. 10000000): " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  if ! [[ "$V" =~ ^[0-9]+$ ]]; then c_red "Must be an integer."; press_enter; return; fi
  sql "UPDATE users SET tokens_limit_daily=$V WHERE id='$UID2';"
  c_grn "Updated user $UID2 to $V tokens/day."
  press_enter
}

osc52_copy() {
  # Best-effort clipboard copy via OSC52 escape (works in iTerm2, Windows Terminal,
  # kitty, mintty, recent xterm, tmux with set-clipboard on, etc).
  local data="$1" b64
  if command -v base64 >/dev/null 2>&1; then
    b64="$(printf '%s' "$data" | base64 | tr -d '\n')"
    printf '\033]52;c;%s\a' "$b64" >/dev/tty 2>/dev/null || true
  fi
}

_grant_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  printf "Duration formats: 24h, 7d, 30d, 3mo, 1y, or a bare number = days\n"
  read -rp "Premium duration [30d]: " DUR
  DUR="${DUR:-30d}"
  local secs
  if ! secs="$(parse_duration_to_seconds "$DUR")"; then c_red "Invalid duration."; return; fi
  read -rp "Premium daily token limit [20000000]: " LIM
  LIM="${LIM:-20000000}"
  if ! [[ "$LIM" =~ ^[0-9]+$ ]]; then c_red "Limit must be integer."; return; fi
  local expiry_iso
  expiry_iso="$(date -u -d "+$secs seconds" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || python3 -c "import datetime; print((datetime.datetime.utcnow()+datetime.timedelta(seconds=$secs)).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
  sql "UPDATE users SET plan='premium', plan_expires_at='$expiry_iso', tokens_limit_daily=$LIM WHERE id='$UID2';"
  c_grn "Granted Premium to $UID2 until $expiry_iso (limit=$LIM/day)."
}

_extend_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  local plan cur
  plan="$(sql "SELECT IFNULL(plan,'free') FROM users WHERE id='$UID2';")"
  cur="$(sql "SELECT IFNULL(plan_expires_at,'') FROM users WHERE id='$UID2';")"
  if [ "$plan" != "premium" ] || [ -z "$cur" ]; then
    c_yel "User is not premium yet. Use Grant instead."; return
  fi
  printf "Current expiry: %s\n" "$cur"
  read -rp "Extra duration (e.g. 7d, 24h, 1mo): " DUR
  [ -z "$DUR" ] && { c_yel "Cancelled."; return; }
  local secs
  if ! secs="$(parse_duration_to_seconds "$DUR")"; then c_red "Invalid."; return; fi
  local new_iso
  new_iso="$(date -u -d "$cur + $secs seconds" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"
  if [ -z "$new_iso" ]; then c_red "Could not compute new expiry."; return; fi
  sql "UPDATE users SET plan_expires_at='$new_iso' WHERE id='$UID2';"
  c_grn "New expiry for $UID2: $new_iso"
}

_revoke_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  sql "UPDATE users SET plan='free', plan_expires_at=NULL, tokens_limit_daily=2000000 WHERE id='$UID2';"
  c_grn "Revoked. $UID2 is now Free (limit reset to 2,000,000/day)."
}

_set_limit_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  read -rp "New daily token limit (e.g. 10000000): " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  if ! [[ "$V" =~ ^[0-9]+$ ]]; then c_red "Must be integer."; return; fi
  sql "UPDATE users SET tokens_limit_daily=$V WHERE id='$UID2';"
  c_grn "Updated $UID2 limit to $V/day."
}

_reset_tokens_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  sql "UPDATE users SET tokens_used_today=0 WHERE id='$UID2';"
  c_grn "Reset $UID2 tokens_used_today=0."
}

action_list_users() {
  c_bld "=== Users ==="
  # Get rows: id|email|display_name|plan|expires|used|limit
  local IFS_BACKUP="$IFS"
  mapfile -t rows < <(sql "SELECT id || '|' || IFNULL(email,'') || '|' || IFNULL(display_name,'') || '|' || IFNULL(plan,'free') || '|' || IFNULL(plan_expires_at,'') || '|' || tokens_used_today || '|' || tokens_limit_daily FROM users ORDER BY (plan='premium') DESC, tokens_used_today DESC LIMIT 200;")
  if [ "${#rows[@]}" -eq 0 ]; then c_yel "No users yet."; press_enter; return; fi

  printf "  %-3s  %-9s  %-32s  %-20s  %-19s  %s\n" "#" "Plan" "Email" "Name" "Expires" "Tokens"
  printf "  %s\n" "------------------------------------------------------------------------------------------------------------"
  local i=0
  for row in "${rows[@]}"; do
    i=$((i+1))
    IFS='|' read -r rid remail rname rplan rexpires rused rlimit <<<"$row"
    local badge="Free"
    [ "$rplan" = "premium" ] && badge="★PREMIUM"
    printf "  %-3s  %-9s  %-32s  %-20s  %-19s  %s/%s\n" \
      "$i" "$badge" "${remail:0:32}" "${rname:0:20}" "${rexpires:0:19}" "$rused" "$rlimit"
  done
  IFS="$IFS_BACKUP"
  echo
  read -rp "Pick row # for actions (Enter to exit): " PICK
  if [ -z "$PICK" ]; then return; fi
  if ! [[ "$PICK" =~ ^[0-9]+$ ]] || [ "$PICK" -lt 1 ] || [ "$PICK" -gt "${#rows[@]}" ]; then
    c_red "Invalid #."; press_enter; return
  fi
  local picked="${rows[$((PICK-1))]}"
  IFS='|' read -r SEL_ID SEL_EMAIL SEL_NAME SEL_PLAN SEL_EXP SEL_USED SEL_LIMIT <<<"$picked"
  user_action_menu "$SEL_ID" "$SEL_EMAIL" "$SEL_NAME" "$SEL_PLAN" "$SEL_EXP"
}

user_action_menu() {
  local UID2="$1" EMAIL="$2" NAME="$3" PLAN="$4" EXP="$5"
  while true; do
    echo
    c_bld "--- Selected user ---"
    printf "  ID:      %s\n" "$UID2"
    printf "  Email:   %s\n" "$EMAIL"
    printf "  Name:    %s\n" "$NAME"
    printf "  Plan:    %s\n" "$PLAN"
    printf "  Expires: %s\n" "${EXP:-—}"
    echo
    echo "  1) Salin User ID (clipboard via OSC52 + print full)"
    echo "  2) Grant Premium (donor)"
    echo "  3) Extend Premium"
    echo "  4) Revoke Premium"
    echo "  5) Set token limit"
    echo "  6) Reset tokens hari ini"
    echo "  0) Back"
    read -rp "Action: " a
    case "$a" in
      1)
         osc52_copy "$UID2"
         echo
         c_cyn "User ID (highlight to copy):"
         printf "  %s\n" "$UID2"
         c_grn "(also sent to clipboard via OSC52 if your terminal supports it)"
         press_enter
         ;;
      2) _grant_uid "$UID2";    PLAN="premium"; EXP="$(sql "SELECT IFNULL(plan_expires_at,'') FROM users WHERE id='$UID2';")"; press_enter ;;
      3) _extend_uid "$UID2";   EXP="$(sql "SELECT IFNULL(plan_expires_at,'') FROM users WHERE id='$UID2';")"; press_enter ;;
      4) _revoke_uid "$UID2";   PLAN="free"; EXP="" ; press_enter ;;
      5) _set_limit_uid "$UID2";press_enter ;;
      6) _reset_tokens_uid "$UID2"; press_enter ;;
      0|"") return ;;
      *) c_red "Invalid."; sleep 1 ;;
    esac
  done
}

# Parse duration strings like '24h', '7d', '3mo', '90m', or bare number (interpreted as days)
parse_duration_to_seconds() {
  local s="$1"
  if [[ "$s" =~ ^([0-9]+)$ ]]; then
    # bare number = days
    echo $(( ${BASH_REMATCH[1]} * 86400 ))
    return 0
  fi
  if [[ "$s" =~ ^([0-9]+)(s|m|h|d|w|mo|y)$ ]]; then
    local n="${BASH_REMATCH[1]}" unit="${BASH_REMATCH[2]}"
    case "$unit" in
      s)  echo $(( n )) ;;
      m)  echo $(( n * 60 )) ;;
      h)  echo $(( n * 3600 )) ;;
      d)  echo $(( n * 86400 )) ;;
      w)  echo $(( n * 604800 )) ;;
      mo) echo $(( n * 2592000 )) ;;
      y)  echo $(( n * 31536000 )) ;;
    esac
    return 0
  fi
  return 1
}

action_grant_premium() {
  c_bld "=== Donate / Grant Premium to a user ==="
  read -rp "User id (uid) or email substring (or leave empty to pick from list): " Q
  if [ -z "$Q" ]; then action_list_users; return; fi
  local matches
  matches="$(sql "SELECT id || ' | ' || IFNULL(email,'') || ' | ' || IFNULL(display_name,'') || ' | plan=' || IFNULL(plan,'free') FROM users WHERE id LIKE '%$Q%' OR email LIKE '%$Q%' LIMIT 20")"
  if [ -z "$matches" ]; then c_red "No users match."; press_enter; return; fi
  printf "Matches:\n%s\n" "$matches"
  read -rp "Exact user id to upgrade: " UID2
  _grant_uid "$UID2"
  press_enter
}

action_extend_premium() {
  c_bld "=== Extend Premium duration ==="
  read -rp "User id: " UID2
  _extend_uid "$UID2"
  press_enter
}

action_revoke_premium() {
  c_bld "=== Revoke Premium ==="
  read -rp "User id: " UID2
  _revoke_uid "$UID2"
  press_enter
}

action_reset_user_tokens() {
  c_bld "=== Reset today's token usage for a user ==="
  read -rp "User id: " UID2
  _reset_tokens_uid "$UID2"
  press_enter
}

action_restart() {
  restart_service
  press_enter
}

action_logs() {
  c_bld "=== Live logs (Ctrl-C to exit) ==="
  pm2 logs "$SERVICE_NAME" --lines 100 || true
}

action_update_repo() {
  c_bld "=== Update from git + rebuild ==="
  cd "$INSTALL_DIR" || return
  git fetch --all --tags
  read -rp "Branch to checkout [$(git rev-parse --abbrev-ref HEAD)]: " B
  B="${B:-$(git rev-parse --abbrev-ref HEAD)}"
  git checkout "$B"
  git pull --ff-only
  npm install --no-audit --no-fund
  ( cd client && npm install --no-audit --no-fund )
  ( cd client && npm run build )
  restart_service
  press_enter
}

action_view_env() {
  c_bld "=== Current .env (secrets masked) ==="
  awk -F= '{
    if ($1=="AI_API_KEY" || $1=="SUZU_ADMIN_TOKEN") {
      v=$2
      if (length(v)>8) { print $1"="substr(v,1,4)"…"substr(v,length(v)-3) }
      else { print $1"=…" }
    } else { print $0 }
  }' "$ENV_FILE"
  press_enter
}

action_backup() {
  c_bld "=== Export backup (zip of suzu.db + meta.json) ==="
  local ts="$(date +%Y%m%d-%H%M%S)"
  local out="/var/backups/suzu-backup-$ts.zip"
  mkdir -p /var/backups
  if ! command -v zip >/dev/null 2>&1; then
    c_yel "Installing 'zip'…"
    apt-get install -y zip >/dev/null 2>&1 || { c_red "Failed to install zip."; press_enter; return; }
  fi
  local work; work="$(mktemp -d)"
  # Online snapshot via sqlite3 .backup to avoid WAL inconsistency.
  if ! command -v sqlite3 >/dev/null 2>&1; then
    apt-get install -y sqlite3 >/dev/null 2>&1 || true
  fi
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_FILE" ".backup '$work/suzu.db'" || { c_red "sqlite3 .backup failed"; rm -rf "$work"; press_enter; return; }
  else
    cp "$DB_FILE" "$work/suzu.db" || { c_red "cp failed"; rm -rf "$work"; press_enter; return; }
  fi
  local users convs
  users="$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM users" 2>/dev/null || echo 0)"
  convs="$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM conversations" 2>/dev/null || echo 0)"
  cat > "$work/meta.json" <<EOF
{
  "version": 1,
  "app": "suzu-ai-web",
  "exported_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "user_count": $users,
  "conversation_count": $convs
}
EOF
  ( cd "$work" && zip -q -9 "$out" suzu.db meta.json )
  rm -rf "$work"
  c_grn "Saved: $out"
  printf "  users:         %s\n" "$users"
  printf "  conversations: %s\n" "$convs"
  press_enter
}

action_restore() {
  c_bld "=== Import backup (zip from /api/admin/backup) ==="
  read -rp "Path to backup .zip: " zp
  zp="${zp/#\~/$HOME}"
  if [ ! -f "$zp" ]; then c_red "Not found: $zp"; press_enter; return; fi
  if ! command -v unzip >/dev/null 2>&1; then
    apt-get install -y unzip >/dev/null 2>&1 || { c_red "Failed to install unzip."; press_enter; return; }
  fi
  local work; work="$(mktemp -d)"
  unzip -q "$zp" -d "$work" || { c_red "Invalid zip."; rm -rf "$work"; press_enter; return; }
  if [ ! -f "$work/suzu.db" ]; then c_red "ZIP missing suzu.db"; rm -rf "$work"; press_enter; return; fi
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$work/suzu.db" "SELECT COUNT(*) FROM users" >/dev/null 2>&1 \
      || { c_red "Not a valid Suzu DB (no users table)."; rm -rf "$work"; press_enter; return; }
  fi
  echo
  c_yel "WARNING: this will REPLACE the current database."
  read -rp "Type 'restore' to confirm: " ok
  [ "$ok" = "restore" ] || { c_yel "Cancelled."; rm -rf "$work"; press_enter; return; }
  local ts; ts="$(date +%Y%m%d-%H%M%S)"
  local safety="$DB_FILE.bak-$ts"
  cp "$DB_FILE" "$safety" 2>/dev/null && c_yel "Safety snapshot: $safety"
  # Stop service while we swap.
  pm2 stop suzu-ai >/dev/null 2>&1 || true
  rm -f "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm" "$DB_FILE-journal"
  cp "$work/suzu.db" "$DB_FILE"
  pm2 start suzu-ai >/dev/null 2>&1 || pm2 restart suzu-ai >/dev/null
  rm -rf "$work"
  c_grn "Restore selesai."
  press_enter
}

action_chat_ai() {
  clear
  print_header
  echo
  c_bld "  ── Chat AI ──"
  c_grn "  AI bebas: decompile / recompile / build APK A-Z, reverse engineering,"
  c_grn "  analisis, boleh masuk website untuk test, semua jenis framework — laju."
  echo
  if [ ! -x "$CHAT_AI_LAUNCHER" ] && ! command -v suzu-chat-ai >/dev/null 2>&1; then
    c_red "Chat AI launcher not found at $CHAT_AI_LAUNCHER."
    c_yel "Run install.sh (or 'suzu-admin' rebuild) on the latest script_ai_panel to install it."
    press_enter
    return
  fi
  local launcher
  if [ -x "$CHAT_AI_LAUNCHER" ]; then
    launcher="$CHAT_AI_LAUNCHER"
  else
    launcher="$(command -v suzu-chat-ai)"
  fi
  echo
  echo "  1) Continue last session (default)"
  echo "  2) Start a new session"
  echo "  3) List sessions"
  echo "  4) Resume a specific session id"
  echo "  0) Back"
  read -rp "Choice [1]: " cc
  cc="${cc:-1}"
  local rc=0
  case "$cc" in
    1) SUZU_ENV_FILE="$ENV_FILE" "$launcher" --last; rc=$? ;;
    2) SUZU_ENV_FILE="$ENV_FILE" "$launcher" --new;  rc=$? ;;
    3) SUZU_ENV_FILE="$ENV_FILE" "$launcher" --list; press_enter; return ;;
    4)
       read -rp "Session id: " sid
       [ -z "$sid" ] && return
       SUZU_ENV_FILE="$ENV_FILE" "$launcher" --resume "$sid"; rc=$?
       ;;
    0|"") return ;;
    *) c_red "Invalid."; sleep 1; return ;;
  esac
  # Exit code 10 = /menu requested from inside chat. Exit code 0 = /exit.
  if [ "$rc" -ne 10 ] && [ "$rc" -ne 0 ]; then
    c_yel "Chat AI exited with code $rc."
    press_enter
  fi
}

_bot_users_json() {
  printf '%s\n' "${SUZU_STATE_DIR:-/var/lib/suzu-ai}/telegram/users.json"
}

_bot_users_py() {
  # Stream the users.json through a Python helper.  Arg 1 is the action;
  # any additional args are passed positionally (e.g. user id).
  local path
  path="$(_bot_users_json)"
  python3 - "$path" "$@" <<'PY'
import json, sys, time, os
path = sys.argv[1]
action = sys.argv[2] if len(sys.argv) > 2 else "list"
def load():
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"approved": {}, "banned": {}, "pending": {}, "admins": []}
def save(d):
    d["saved_at"] = time.time()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)
def label(info):
    name = (info.get("first_name", "") + " " + info.get("last_name", "")).strip()
    uname = info.get("username", "")
    return f"@{uname} ({name})" if uname else name or "(no name)"
d = load()
if action == "list":
    admins = set(map(str, d.get("admins", [])))
    print("== Approved (%d) ==" % len(d.get("approved", {})))
    for uid, info in sorted(d.get("approved", {}).items(), key=lambda kv: int(kv[0])):
        admin = " [admin]" if uid in admins else ""
        print(f"  {uid}  {label(info)}{admin}")
    print("\n== Banned (%d) ==" % len(d.get("banned", {})))
    for uid, info in d.get("banned", {}).items():
        print(f"  {uid}  {label(info)}  reason: {info.get('reason','')}")
    print("\n== Pending (%d) ==" % len(d.get("pending", {})))
    for uid, info in sorted(d.get("pending", {}).items(), key=lambda kv: -kv[1].get("last_seen", 0)):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.get("last_seen", 0)))
        print(f"  {uid}  {label(info)}  attempts={info.get('attempts',0)} last={when}")
elif action == "approve":
    uid = sys.argv[3]; src = d.get("pending", {}).get(uid, {})
    entry = {"added_at": time.time(), "added_by": "suzu-admin"}
    for k in ("username", "first_name", "last_name"):
        if src.get(k): entry[k] = src[k]
    d.setdefault("approved", {})[uid] = entry
    d.get("pending", {}).pop(uid, None)
    d.get("banned", {}).pop(uid, None)
    save(d); print(f"approved {uid}")
elif action == "ban":
    uid = sys.argv[3]; reason = sys.argv[4] if len(sys.argv) > 4 else ""
    entry = {"banned_at": time.time(), "banned_by": "suzu-admin", "reason": reason}
    src = d.get("approved", {}).get(uid) or d.get("pending", {}).get(uid) or {}
    for k in ("username", "first_name", "last_name"):
        if src.get(k): entry[k] = src[k]
    d.setdefault("banned", {})[uid] = entry
    d.get("approved", {}).pop(uid, None)
    d.get("pending", {}).pop(uid, None)
    admins = [a for a in d.get("admins", []) if str(a) != uid]
    d["admins"] = admins
    save(d); print(f"banned {uid}")
elif action == "unban":
    uid = sys.argv[3]
    if uid in d.get("banned", {}):
        del d["banned"][uid]; save(d); print(f"unbanned {uid}")
    else:
        print(f"{uid} not in ban list")
elif action == "promote":
    uid = sys.argv[3]
    if uid not in d.get("approved", {}):
        d.setdefault("approved", {})[uid] = {"added_at": time.time(), "added_by": "promote"}
    admins = list(map(str, d.get("admins", [])))
    if uid not in admins:
        admins.append(uid)
    d["admins"] = admins
    save(d); print(f"promoted {uid}")
elif action == "demote":
    uid = sys.argv[3]
    admins = [a for a in map(str, d.get("admins", [])) if a != uid]
    d["admins"] = admins
    save(d); print(f"demoted {uid}")
elif action == "count":
    print(len(d.get("approved", {})), len(d.get("banned", {})), len(d.get("pending", {})), len(d.get("admins", [])))
PY
}

action_telegram_bot() {
  while :; do
    clear
    c_bld "=== Suzu Telegram Bot ==="
    local token_set ids_set status counts
    token_set="$(env_get TELEGRAM_BOT_TOKEN)"
    ids_set="$(env_get TELEGRAM_ALLOWED_USER_IDS)"
    if systemctl is-active --quiet suzu-telegram-bot.service 2>/dev/null; then
      status="$(printf '\033[32mrunning\033[0m')"
    elif systemctl list-unit-files --type=service 2>/dev/null | grep -q '^suzu-telegram-bot\.service'; then
      status="$(printf '\033[33minactive\033[0m')"
    else
      status="$(printf '\033[31mnot installed\033[0m')"
    fi
    counts="$(_bot_users_py count 2>/dev/null || echo '0 0 0 0')"
    set -- $counts
    printf "  Status     : %b\n" "$status"
    printf "  Bot        : @%s\n" "$(env_get TELEGRAM_BOT_USERNAME)"
    printf "  Token      : %s\n" "$([ -n "$token_set" ] && echo '<set>' || echo '<empty>')"
    printf "  Users file : %s\n" "$(_bot_users_json)"
    printf "  Users      : approved=%s banned=%s pending=%s admins=%s\n" "${1:-0}" "${2:-0}" "${3:-0}" "${4:-0}"
    echo
    c_bld "  Users:"
    echo "   1) List users (approved/banned/pending)"
    echo "   2) Approve user (enter id)"
    echo "   3) Ban user (enter id [reason])"
    echo "   4) Unban user"
    echo "   5) Promote user to admin"
    echo "   6) Demote admin"
    c_bld "  Service:"
    echo "   7) Restart bot"
    echo "   8) Stop bot"
    echo "   9) Start bot"
    echo "  10) Tail logs (journalctl -f)"
    echo "  11) View systemctl status"
    c_bld "  Settings:"
    echo "  12) Set TELEGRAM_BOT_TOKEN"
    echo "  13) Set TELEGRAM_ALLOWED_USER_IDS (env seed only)"
    echo "  14) Set TELEGRAM_BOT_USERNAME"
    echo
    echo "   0) Back"
    read -rp "Choice: " tc
    case "$tc" in
      1) _bot_users_py list | less -R ;;
      2)
        read -rp "User id to approve: " uid
        [ -z "$uid" ] && continue
        _bot_users_py approve "$uid"
        press_enter ;;
      3)
        read -rp "User id to ban: " uid
        [ -z "$uid" ] && continue
        read -rp "Reason (optional): " reason
        _bot_users_py ban "$uid" "$reason"
        press_enter ;;
      4)
        read -rp "User id to unban: " uid
        [ -z "$uid" ] && continue
        _bot_users_py unban "$uid"
        press_enter ;;
      5)
        read -rp "User id to promote (admin): " uid
        [ -z "$uid" ] && continue
        _bot_users_py promote "$uid"
        press_enter ;;
      6)
        read -rp "User id to demote: " uid
        [ -z "$uid" ] && continue
        _bot_users_py demote "$uid"
        press_enter ;;
      7) systemctl restart suzu-telegram-bot.service && c_grn "Restarted." || c_red "Restart failed."; press_enter ;;
      8) systemctl stop    suzu-telegram-bot.service && c_grn "Stopped."   || c_red "Stop failed.";    press_enter ;;
      9) systemctl start   suzu-telegram-bot.service && c_grn "Started."   || c_red "Start failed.";   press_enter ;;
      10) journalctl -u suzu-telegram-bot.service -f --no-pager ;;
      11) systemctl status suzu-telegram-bot.service --no-pager -l | head -40; press_enter ;;
      12)
        read -rp "New TELEGRAM_BOT_TOKEN: " nt
        [ -z "$nt" ] && continue
        env_set TELEGRAM_BOT_TOKEN "$nt"
        systemctl restart suzu-telegram-bot.service 2>/dev/null || true
        c_grn "Token saved & service restarted."
        press_enter ;;
      13)
        read -rp "New TELEGRAM_ALLOWED_USER_IDS (csv, seed only): " ni
        [ -z "$ni" ] && continue
        env_set TELEGRAM_ALLOWED_USER_IDS "$ni"
        c_grn "Saved.  (note: live allowlist lives in users.json \u2014 use option 2 to approve)"
        press_enter ;;
      14)
        read -rp "New TELEGRAM_BOT_USERNAME: " nu
        [ -z "$nu" ] && continue
        env_set TELEGRAM_BOT_USERNAME "${nu#@}"
        c_grn "Bot username saved."
        press_enter ;;
      0|q|Q) return ;;
      *) c_red "Invalid choice."; sleep 0.5 ;;
    esac
  done
}

# Run the Python model-registry CLI with the right env so it reads/writes the
# SAME models.json the Telegram bot uses (state dir comes from the env file).
_models_py() {
  SUZU_ENV_FILE="$ENV_FILE" PYTHONPATH="$PANEL_DIR" python3 -m chat_ai.models_registry "$@"
}

# ── Model Manager ─────────────────────────────────────────────────────────── #
# Add/list/remove multiple model providers. Each profile = name | base URL |
# model id | API key. The Telegram bot syncs live and lets users pick via
# /models, so admins can offer Claude, Deepseek, GPT, etc. side by side.
action_models_menu() {
  while :; do
    clear
    print_header
    echo
    c_bld "  ── Model Manager ──"
    echo "  (profil ditanda * = default. Bot sync automatik; user pilih via /models)"
    echo
    _models_py list 2>/dev/null || c_yel "  (cannot read registry yet)"
    echo
    c_bld "  Actions:"
    echo "   1) Add / update model (name, base URL, model id, api key)"
    echo "   2) Set default model"
    echo "   3) Remove model"
    echo "   4) Refresh list"
    echo
    echo "   0) Back"
    read -rp "Choice: " mc; mc="$(_sanitize "$mc")"
    case "$mc" in
      1)
        echo
        read -rp "Name (cth: Claude 4.8 Opus): " m_name; [ -z "$m_name" ] && continue
        read -rp "Base URL (cth: http://103.200.216.137:3000/v1): " m_url; [ -z "$m_url" ] && continue
        read -rp "Model id (cth: claude-opus-4-8): " m_model; [ -z "$m_model" ] && continue
        read -rp "API key: " m_key
        if _models_py add --name "$m_name" --base-url "$m_url" --model "$m_model" --api-key "$m_key"; then
          c_grn "  Saved. Bot akan sync automatik (user nampak di /models)."
        else
          c_red "  Failed to save."
        fi
        press_enter ;;
      2)
        echo
        read -rp "Name model jadi default: " m_name; [ -z "$m_name" ] && continue
        _models_py set-default --name "$m_name" || c_red "  not found"
        press_enter ;;
      3)
        echo
        read -rp "Name model nak buang: " m_name; [ -z "$m_name" ] && continue
        _models_py remove --name "$m_name" || c_red "  not found"
        press_enter ;;
      4) : ;;
      0|q|Q|"") return ;;
      *) c_red "Invalid choice."; sleep 0.5 ;;
    esac
  done
}

action_admin_token() {
  c_bld "=== Admin token (for APK admin panel) ==="
  local cur
  cur="$(env_get SUZU_ADMIN_TOKEN)"
  if [ -z "$cur" ]; then
    c_yel "No token yet. Generating one…"
  else
    printf "Current: %s\n" "$cur"
  fi
  echo
  echo "  1) Show full token"
  echo "  2) Generate a new token (existing APK installs must re-pair)"
  echo "  0) Back"
  read -rp "Choice: " a
  case "$a" in
    1) printf "Token: %s\n" "$cur" ;;
    2)
       local newt
       newt="$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48)"
       env_set SUZU_ADMIN_TOKEN "$newt"
       restart_service
       c_grn "New admin token: $newt"
       ;;
    *) return ;;
  esac
  press_enter
}

# Keep only the characters that can appear in a menu choice.  This also stops
# stray escape sequences (e.g. arrow keys send ESC[A / ESC[B) from polluting the
# prompt and triggering "Invalid choice" spam — the menus are number-driven.
_sanitize() { printf '%s' "${1:-}" | tr -cd '0-9A-Za-z'; }

# Short, prominent label for the active model, e.g. "OPUS 4.8".
_model_label() {
  local m short
  m="$(env_get AI_DEFAULT_MODEL)"
  short="$(printf '%s' "$m" | grep -oiE '(opus|sonnet|haiku|gpt)[-/ ]?[0-9.]+' | head -n1)"
  if [ -n "$short" ]; then
    printf '%s' "$short" | tr '[:lower:]-' '[:upper:] '
  else
    printf '%s' "${m:-unknown}"
  fi
}

print_header() {
  c_cyn "════════════════════════════════════════════"
  c_bld "               ClaudeSuzuBot"
  c_grn "                  $(_model_label)"
  c_cyn "════════════════════════════════════════════"
  printf "  Domain : %s\n" "$(env_get SUZU_DOMAIN)"
  printf "  Model  : %s\n" "$(env_get AI_DEFAULT_MODEL)"
}

# ── 1) Update ─────────────────────────────────────────────────────────────── #
action_update_menu() {
  while :; do
    clear
    print_header
    echo
    c_bld "  ── Update ──"
    echo "   1) Update domain (+ SSL)"
    echo "   2) Update AI base URL"
    echo "   3) Update AI API key"
    echo "   4) Update default model"
    echo "   5) Set default daily token limit (all users)"
    echo
    echo "   0) Back"
    read -rp "Choice: " u; u="$(_sanitize "$u")"
    case "$u" in
      1) action_update_domain ;;
      2) action_update_baseurl ;;
      3) action_update_apikey ;;
      4) action_update_model ;;
      5) action_default_limit ;;
      0|q|Q|"") return ;;
      *) c_red "Invalid choice."; sleep 0.5 ;;
    esac
  done
}

# ── 4) Users & Premium ────────────────────────────────────────────────────── #
action_users_premium_menu() {
  while :; do
    clear
    print_header
    echo
    c_bld "  ── Users & Premium ──"
    echo "   1) List users (interactive: pick row → actions)"
    echo "   2) Set token limit for one user (donor)"
    echo "   3) Reset today's token usage for a user"
    echo "   4) Grant Premium to user (donor)"
    echo "   5) Extend Premium duration"
    echo "   6) Revoke Premium"
    echo
    echo "   0) Back"
    read -rp "Choice: " u; u="$(_sanitize "$u")"
    case "$u" in
      1) action_list_users ;;
      2) action_set_user_limit ;;
      3) action_reset_user_tokens ;;
      4) action_grant_premium ;;
      5) action_extend_premium ;;
      6) action_revoke_premium ;;
      0|q|Q|"") return ;;
      *) c_red "Invalid choice."; sleep 0.5 ;;
    esac
  done
}

# ── 5) Service & Backup ───────────────────────────────────────────────────── #
action_service_backup_menu() {
  while :; do
    clear
    print_header
    echo
    c_bld "  ── Service & Backup ──"
    echo "   1) Restart service (pm2 restart)"
    echo "   2) View live logs"
    echo "   3) git pull + rebuild + restart"
    echo "   4) View current .env"
    echo "   5) Admin token (show / regenerate)"
    echo "   6) Export backup (zip)"
    echo "   7) Import backup (zip)"
    echo
    echo "   0) Back"
    read -rp "Choice: " u; u="$(_sanitize "$u")"
    case "$u" in
      1) action_restart ;;
      2) action_logs ;;
      3) action_update_repo ;;
      4) action_view_env ;;
      5) action_admin_token ;;
      6) action_backup ;;
      7) action_restore ;;
      0|q|Q|"") return ;;
      *) c_red "Invalid choice."; sleep 0.5 ;;
    esac
  done
}

show_menu() {
  clear
  print_header
  echo
  echo "  1) Update            — domain, base URL, API key, model, token limit"
  echo "  2) Chat AI           — decompile / recompile / build APK, reverse engineering"
  echo "  3) Telegram Bot      — users, service, settings"
  echo "  4) Model Manager     — tambah banyak model (Claude, Deepseek, GPT…), set default"
  echo "  5) Users & Premium   — limits, donor premium"
  echo "  6) Service & Backup  — restart, logs, update, env, admin token, backup"
  echo
  echo "  0) Exit to shell"
  echo
  read -rp "Choose an option: " choice
  choice="$(_sanitize "${choice:-}")"
}

main() {
  need_root
  require_install
  while true; do
    show_menu
    case "${choice:-}" in
      1) action_update_menu ;;
      2) action_chat_ai ;;
      3) action_telegram_bot ;;
      4) action_models_menu ;;
      5) action_users_premium_menu ;;
      6) action_service_backup_menu ;;
      0|q|Q|exit) c_grn "Bye."; exit 0 ;;
      "") : ;;
      *) c_red "Invalid choice."; sleep 1 ;;
    esac
  done
}

main "$@"
