#!/usr/bin/env bash
# dev.sh — launch / stop the whole Bird's Eye Review studio platform locally.
#
#   ./dev.sh up        start everything (redis check, API, both workers, studio)
#   ./dev.sh down      stop everything this script manages (and adopted orphans)
#   ./dev.sh status    show what's running
#   ./dev.sh logs X    tail a component's log (X = api | worker | pdfworker | studio)
#   ./dev.sh restart   down + up
#
#   --public           (with up/restart) host-behind-a-tunnel posture: both
#                      servers bind 127.0.0.1 only, studio runs a production
#                      build, and PUBLIC_MODE=true / TRUST_PROXY_HEADERS=true
#                      are exported so the API cannot be started half-public.
#                      See docs/hosting.md.
#   --lan              (with up/restart) bind the API and studio on 0.0.0.0 so
#                      other machines on your network (a phone, a colleague)
#                      can reach them. Off by default: everything binds
#                      127.0.0.1. Refused while JWT_SECRET is the placeholder.
#   --install-deps     allow dev.sh to install missing SYSTEM packages (sudo apt
#                      for redis / python3-venv, nvm for Node). Same as
#                      BER_AUTO_INSTALL=1. Without it, dev.sh only reports what
#                      is missing and how to install it.
#
# Components:
#   api        FastAPI backend  -> http://127.0.0.1:${API_PORT} (uvicorn)
#   worker     arq LLM worker   -> pipeline steps + full-text screen (queue ber:default)
#   pdfworker  arq PDF worker   -> full-text fetch + metadata (queue ber:pdf, non-LLM)
#   studio     Next.js frontend -> http://127.0.0.1:${STUDIO_PORT}
#   redis      system service (started if installed: systemd / service / daemon)
#
# State: PID files + logs live in .run/ (gitignored). Ports/env overridable:
#   API_PORT=8000 STUDIO_PORT=3002 NODE_BIN=/path/to/node/bin
# NODE_BIN is auto-discovered (PATH, nvm, fnm, volta) — set it only to override.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$REPO/.run"
API_PORT="${API_PORT:-8000}"
STUDIO_PORT="${STUDIO_PORT:-3002}"
# NODE_BIN is DISCOVERED (see resolve_node_bin), not hardcoded — an explicit
# NODE_BIN=... still wins. Empty here so the discovery below can run.
NODE_BIN="${NODE_BIN:-}"
VENV="$REPO/server/.venv"
PY="$VENV/bin/python"
# Set to 1 when studio was started but hadn't answered its port before we gave
# up waiting (common on /mnt/c, where the first `next dev` compile is minutes).
# Not a failure — cmd_up adapts its closing message instead of exiting.
STUDIO_SLOW=0
# Set to 1 when ensure_studio_env rewrote studio/.env.local. Next inlines
# NEXT_PUBLIC_* at BUILD time, so an already-running studio is serving a bundle
# with the OLD API base and must be restarted to pick up the new one.
ENV_CHANGED=0

# `./dev.sh up --public` — the posture for hosting this behind a Cloudflare
# tunnel, as opposed to running it on your own desk.
#
# Three differences, all of them load-bearing:
#   * Both servers bind 127.0.0.1 (as they do by default; --lan is refused
#     alongside --public), so the ONLY way in is the tunnel. Nothing on the LAN,
#     and nothing that finds the machine some other way, can reach a port.
#   * The studio is a production BUILD (`next build && next start`), not
#     `next dev --turbopack`. A dev server hands out error overlays containing
#     source, recompiles on demand, and is not written to face the internet.
#   * uvicorn gets --proxy-headers so the rate limiter can see the real client
#     address through the proxy chain (see app/ratelimit.py::client_key).
# --public also exports PUBLIC_MODE=true into the API/worker environment
# (overrides server/.env). Starting without --public while server/.env
# already has PUBLIC_MODE=true is refused: that would run the dev studio server
# with public-posture settings.
PUBLIC_MODE=0
# `--lan`: opt in to binding 0.0.0.0 (see the header). 0 = loopback only.
LAN=0
# `--install-deps` / BER_AUTO_INSTALL=1: may this script install system packages?
AUTO_INSTALL="${BER_AUTO_INSTALL:-0}"

mkdir -p "$RUN_DIR"

# Roll a component log that has grown past ~8MB, keeping one previous
# generation. These are append-only across every restart, so .run/api.log had
# reached 9.7MB of request lines — which is both a slow `./dev.sh logs` and, on
# a hosted instance, an ever-growing record of who asked for what. One rotation
# is enough: the interesting window is always the current run.
# The host a component is actually reachable on, given the mode. Only --lan
# binds a non-loopback address, so only then is the LAN IP a live address.
bind_host() { [ "$LAN" = 1 ] && echo "$LAN_IP" || echo "127.0.0.1"; }
# The address the servers listen on.
listen_host() { [ "$LAN" = 1 ] && echo "0.0.0.0" || echo "127.0.0.1"; }

rotate_log() {
  local f="$RUN_DIR/$1.log" max=$((8 * 1024 * 1024)) size
  [ -f "$f" ] || return 0
  size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  [ "$size" -gt "$max" ] || return 0
  mv -f "$f" "$f.1"
  echo "  rotated .run/$1.log (was $((size / 1024 / 1024))MB) -> $1.log.1"
}

# Corepack asks "Do you want to continue? [Y/n]" before downloading the pnpm
# version pinned in package.json — which blocks an otherwise unattended setup.
# Answer yes up front so `pnpm install` never stops for input.
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0

# Are we inside WSL? This changes which address is correct (see host_ip below).
is_wsl() {
  [ -n "${WSL_DISTRO_NAME:-}" ] && return 0
  [ -n "${WSL_INTEROP:-}" ] && return 0
  grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null
}

# This machine's address on the network, for the URLs we print and for the
# studio's API base.
#
# The honest definition of "my address" is the one on the interface that owns
# the DEFAULT ROUTE — not simply the first private address `ip addr` lists. A
# dev box typically has many virtual interfaces (docker0, br-*, lxcbr0, VPN
# tunnels); picking the first match is picking at random.
lan_ip() {
  local ip
  # Scan real interfaces first. NOTE: we deliberately do NOT lead with
  # `ip route get 1.1.1.1`, because on a machine with a corporate VPN up that
  # returns the TUNNEL address (e.g. cscotun0's 10.15.x.x) — right for reaching
  # the internet, wrong for "open this URL in a browser on my LAN".
  # Skips virtual/bridge/tunnel interfaces and the WSL2 host stub.
  ip="$(ip -4 addr show scope global 2>/dev/null | awk '
    /^[0-9]+:/ {
      iface = $2; sub(/:$/, "", iface)
      skip = (iface ~ /^(docker|br-|lxc|virbr|veth|anbox|tun|tap|cscotun|wg|zt)/)
    }
    !skip && /inet (10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)/ {
      split($2, a, "/")
      # 10.255.255.254 is the WSL2 Windows-side host/DNS stub, never ours.
      if (a[1] != "10.255.255.254") { print a[1]; exit }
    }')"
  if [ -n "$ip" ]; then echo "$ip"; return 0; fi
  # Last resort: whatever the default route uses (tunnel included — better than
  # nothing if no plain LAN interface was found at all).
  ip -4 route get 1.1.1.1 2>/dev/null | grep -oP '\bsrc \K[0-9.]+' | head -1 || true
}

# The address to PRINT and to point the browser's API calls at.
#
# On WSL2 (default NAT mode) Windows forwards localhost into the Linux VM, so a
# Windows browser reaches a WSL-hosted server at localhost:<port> — that is the
# documented, supported path. Guessing a LAN IP there is actively WRONG: the
# usual candidate is 10.255.255.254, the Windows-side DNS/host stub, where
# nothing of ours listens (the browser just times out). Prefer localhost.
host_ip() {
  if is_wsl; then echo "localhost"; return 0; fi
  local ip; ip="$(lan_ip)"
  echo "${ip:-localhost}"
}
LAN_IP="$(host_ip)"

# ---------- helpers ----------------------------------------------------------

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

read_pidfile() { # $1=name -> echoes pid if alive, else cleans stale file
  local f="$RUN_DIR/$1.pid" pid
  [ -f "$f" ] || return 1
  pid="$(cat "$f" 2>/dev/null || true)"
  if pid_alive "$pid"; then echo "$pid"; else rm -f "$f"; return 1; fi
}

# Find a process by listening port, verifying its cmdline (adopt ad-hoc starts).
# NOTE: parse pid from the whole ss line — the process field can contain spaces
# (e.g. "next-server (v1"), which breaks column-based extraction.
pid_by_port() { # $1=port $2=cmdline-substring
  local pid
  pid="$(ss -tlnp 2>/dev/null | grep -E "[:.]$1[[:space:]]" \
        | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
  [ -n "$pid" ] || return 1
  if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$2"; then
    echo "$pid"
  else
    return 1
  fi
}

# The workers have no port; find them by cmdline (safe in a script: our own
# cmdline is "bash ./dev.sh ...", so the patterns below can't match ourselves).
# $1 = the arq settings class to match (WorkerSettings | PdfWorkerSettings). The
# match is ANCHORED to the exact class so the two workers never adopt each other:
# "WorkerSettings" is a suffix of "PdfWorkerSettings", so a bare substring match
# would make the LLM-worker finder also catch the PDF worker.
arq_orphan_pids() {
  local cls="$1" pid exe cmd
  for pid in $(pgrep -f "arq server.app.worker.$cls" 2>/dev/null); do
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    case "$exe" in *python*) ;; *) continue ;; esac
    # Anchor: the cmdline must end with exactly this class (…worker.<cls>), not
    # merely contain it — so WorkerSettings doesn't also match PdfWorkerSettings.
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in *"server.app.worker.$cls "*|*"server.app.worker.$cls") echo "$pid" ;; esac
  done
}
# The LLM worker (ber:default) — matched exactly so it excludes the PDF worker.
worker_orphan_pids() { arq_orphan_pids WorkerSettings; }
worker_orphan_pid()  { worker_orphan_pids | head -1; }
# The non-LLM PDF worker (ber:pdf), a separate process on its own queue.
pdfworker_orphan_pids() { arq_orphan_pids PdfWorkerSettings; }
pdfworker_orphan_pid()  { pdfworker_orphan_pids | head -1; }

kill_tree() { # $1=pid $2=signal — signal pid and all descendants (children first)
  local p="$1" sig="${2:-TERM}" k
  for k in $(pgrep -P "$p" 2>/dev/null); do kill_tree "$k" "$sig"; done
  kill "-$sig" "$p" 2>/dev/null || true
}

stop_pid() { # $1=pid $2=label — TERM the whole tree, wait, then KILL leftovers
  local pid="$1" label="$2" i
  echo "  stopping $label (pid $pid)…"
  kill_tree "$pid" TERM
  for i in $(seq 1 16); do pid_alive "$pid" || { echo "  $label stopped."; return 0; }; sleep 0.5; done
  echo "  $label did not exit; killing tree."
  kill_tree "$pid" KILL
}

wait_http() { # $1=url $2=tries(x0.5s) [$3=label -> draw a progress bar while waiting]
  local i label="${3:-}" show=0
  [ -n "$label" ] && [ -t 2 ] && show=1
  for i in $(seq 1 "$2"); do
    if curl -sf -m 2 "$1" >/dev/null 2>&1; then
      [ "$show" = 1 ] && printf '\r\033[K' >&2
      return 0
    fi
    # A silent multi-minute wait is indistinguishable from a hang. The bar fills
    # against the TIMEOUT (we can't know real compile progress), so it doubles as
    # "how much patience is left before I give up".
    # Count in whole seconds, not half-second ticks, so the bar reads sensibly.
    [ "$show" = 1 ] && bar_render "$label" $((i / 2)) $(( $2 / 2 )) $((i / 2)) "s"
    sleep 0.5
  done
  [ "$show" = 1 ] && printf '\r\033[K' >&2
  return 1
}

# ---------- prereq bootstrap ---------------------------------------------------

# ---- auto-install support ----
# Installing system packages (sudo apt, a curl|bash of nvm) is OPT-IN: pass
# --install-deps or set BER_AUTO_INSTALL=1. By default a missing prerequisite is
# reported with the command that installs it, and nothing runs as root.
have_apt() { command -v apt-get >/dev/null 2>&1; }
auto_install_ok() { [ "$AUTO_INSTALL" = 1 ] && [ "${BER_NO_AUTO_INSTALL:-0}" != 1 ]; }

# Can we get root non-interactively, or at all? `sudo -n true` succeeds when a
# sudo timestamp is already active or NOPASSWD is set; otherwise sudo will
# prompt, which is fine here because dev.sh runs attached to a terminal.
sudo_prefix() {
  if [ "$(id -u)" = 0 ]; then echo ""; return 0; fi
  command -v sudo >/dev/null 2>&1 && { echo "sudo"; return 0; }
  return 1
}

APT_UPDATED=0
# apt_install <pkg...> — returns non-zero if we can't or the install fails.
apt_install() {
  auto_install_ok || return 1
  have_apt || return 1
  local SUDO; SUDO="$(sudo_prefix)" || return 1
  echo ">> installing missing package(s): $* …"
  if [ "$APT_UPDATED" = 0 ]; then
    $SUDO apt-get update -qq >/dev/null 2>&1 || true
    APT_UPDATED=1
  fi
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq "$@" >/dev/null 2>&1
}

# How does this machine manage services? WSL2 ships without systemd by default,
# where `systemctl start redis-server` fails with "Unit redis-server.service not
# found" / "System has not been booted with systemd". Probe, don't assume.
init_system() {
  if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    echo systemd
  elif command -v service >/dev/null 2>&1; then
    echo sysv          # WSL without systemd, and older Debian/Ubuntu
  else
    echo none
  fi
}

redis_up() { redis-cli ping >/dev/null 2>&1; }

# Try to start redis with whatever this machine actually uses. Non-fatal: the
# caller decides what to do if redis is still down afterwards.
try_start_redis() {
  local init; init="$(init_system)"
  case "$init" in
    systemd) sudo systemctl start redis-server >/dev/null 2>&1 || true ;;
    sysv)    sudo service redis-server start   >/dev/null 2>&1 || true ;;
  esac
  # Last resort: run the daemon directly (no service manager, or a redis
  # installed outside the packaging that registers a unit/init script).
  if ! redis_up && command -v redis-server >/dev/null 2>&1; then
    redis-server --daemonize yes >/dev/null 2>&1 || true
  fi
  local i
  for i in $(seq 1 10); do redis_up && return 0; sleep 0.5; done
  return 1
}

# ---- progress bar (tqdm-style) ----------------------------------------------
# Render one tqdm-like bar to stderr, rewritten in place:
#   Installing node deps  62%|█████████████▊        | 402/650 MB [02:41<01:38]
# $1=label $2=current $3=total $4=elapsed-seconds $5=unit
bar_render() {
  local label="$1" cur="$2" total="$3" elapsed="$4" unit="${5:-}"
  local width=22 pct filled i out="" eta_s rem
  [ "$total" -gt 0 ] 2>/dev/null || total=1
  [ "$cur" -gt "$total" ] && cur="$total"
  pct=$(( cur * 100 / total ))
  filled=$(( cur * width / total ))
  for ((i = 0; i < width; i++)); do
    if [ "$i" -lt "$filled" ]; then out+="█"; else out+=" "; fi
  done
  # ETA from the average rate so far (tqdm's own estimate).
  if [ "$cur" -gt 0 ]; then
    eta_s=$(( elapsed * (total - cur) / cur ))
  else
    eta_s=0
  fi
  printf '\r   %s %3d%%|%s| %s/%s%s [%02d:%02d<%02d:%02d]\033[K' \
    "$label" "$pct" "$out" "$cur" "$total" "${unit:+ $unit}" \
    $((elapsed / 60)) $((elapsed % 60)) $((eta_s / 60)) $((eta_s % 60)) >&2
}

# A background bar that tracks node_modules growing toward an expected total.
# pnpm's own "Progress:" line rewrites in place and stalls silently during the
# final link step, so a slow install is indistinguishable from a hang.
HEARTBEAT_PID=""
heartbeat_start() {
  [ -t 2 ] || return 0                      # only when attached to a terminal
  local target="$REPO/node_modules"
  # Expected install size in MB — the bar's denominator. Overshoot is clamped.
  local total_mb=650
  (
    local t=0 cur mb
    while :; do
      sleep 2; t=$((t + 2))
      cur=$(du -s "$target" 2>/dev/null | cut -f1); cur=${cur:-0}
      mb=$((cur / 1024))
      bar_render "Installing node deps" "$mb" "$total_mb" "$t" "MB"
    done
  ) &
  HEARTBEAT_PID=$!
  disown "$HEARTBEAT_PID" 2>/dev/null || true
}
heartbeat_stop() {
  [ -n "$HEARTBEAT_PID" ] && kill "$HEARTBEAT_PID" 2>/dev/null || true
  HEARTBEAT_PID=""
  [ -t 2 ] && printf '\r\033[K' >&2 || true   # erase the bar line
}
# Never leave the heartbeat running if dev.sh dies or is Ctrl-C'd.
trap 'heartbeat_stop' EXIT INT TERM

# Is the venv itself structurally sound? A `python3 -m venv` that dies partway
# (the classic ensurepip failure) leaves a directory with a python symlink but
# NO pip — so checking only for python and then calling pip fails with
# "No such file or directory". Both must work.
venv_ok() {
  [ -x "$PY" ] || return 1
  "$PY" -c 'import sys' >/dev/null 2>&1 || return 1
  # pip must be present AND runnable (a dangling symlink passes -x)
  "$PY" -m pip --version >/dev/null 2>&1
}

# Can the server venv actually run the app? Importing the entrypoint deps is the
# only honest test — a venv can exist and be empty.
server_deps_ok() {
  venv_ok || return 1
  "$PY" -c 'import uvicorn, fastapi, arq' >/dev/null 2>&1
}

# Are workspace deps actually usable? pnpm writes .modules.yaml only after a
# successful install, and next/ must resolve for the studio to start — so this
# distinguishes "complete" from "a partial tree left by a cancelled run".
node_deps_ok() {
  [ -f "$REPO/node_modules/.modules.yaml" ] || return 1
  [ -d "$REPO/studio/node_modules" ] || return 1
  [ -e "$REPO/node_modules/.pnpm" ] || return 1
  # the studio's main dependency must be resolvable
  [ -d "$REPO/node_modules/.pnpm" ] && ls "$REPO/node_modules/.pnpm" 2>/dev/null \
    | grep -q '^next@' || return 1
  return 0
}

# ---- node / pnpm discovery -----------------------------------------------
# Find a directory containing BOTH node and pnpm. Order: an explicit NODE_BIN,
# then whatever is on PATH, then the newest nvm install. Never hardcode a
# version — that only ever works on the machine it was written on.
node_bin_candidates() {
  [ -n "${NODE_BIN:-}" ] && echo "$NODE_BIN"
  command -v pnpm  >/dev/null 2>&1 && dirname "$(command -v pnpm)"
  command -v node  >/dev/null 2>&1 && dirname "$(command -v node)"
  command -v corepack >/dev/null 2>&1 && dirname "$(command -v corepack)"
  # nvm installs, newest version first (version-sorted).
  if [ -d "$HOME/.nvm/versions/node" ]; then
    local d
    for d in $(ls -1 "$HOME/.nvm/versions/node" 2>/dev/null | sort -Vr); do
      echo "$HOME/.nvm/versions/node/$d/bin"
    done
  fi
  # fnm, volta, and system installs
  [ -d "$HOME/.local/share/fnm" ] && find "$HOME/.local/share/fnm" -maxdepth 4 -name bin -type d 2>/dev/null
  [ -d "$HOME/.volta/bin" ] && echo "$HOME/.volta/bin"
  echo /usr/local/bin
  echo /usr/bin
}

# Sets NODE_BIN to a dir with node+pnpm, or returns 1.
resolve_node_bin() {
  local d
  for d in $(node_bin_candidates); do
    if [ -x "$d/pnpm" ] && [ -x "$d/node" ]; then NODE_BIN="$d"; return 0; fi
  done
  # node but no pnpm → corepack can provide the pinned pnpm (repo sets
  # packageManager), which is the documented route.
  for d in $(node_bin_candidates); do
    if [ -x "$d/node" ] && [ -x "$d/corepack" ]; then
      echo ">> node found at $d but pnpm is missing — enabling it via corepack…"
      # The sudo retry writes into a system Node install, so it is opt-in.
      if "$d/corepack" enable pnpm >/dev/null 2>&1 \
         || { auto_install_ok && $( [ "$(id -u)" = 0 ] || echo sudo ) "$d/corepack" enable pnpm >/dev/null 2>&1; }; then
        [ -x "$d/pnpm" ] && { NODE_BIN="$d"; return 0; }
      fi
      # corepack enable can land pnpm elsewhere on PATH
      if command -v pnpm >/dev/null 2>&1; then
        NODE_BIN="$(dirname "$(command -v pnpm)")"; return 0
      fi
    fi
  done
  return 1
}

# Install Node 22 + pnpm when nothing usable exists. Prefers nvm (no root, and
# matches .nvmrc); falls back to apt's nodejs if nvm can't be fetched.
install_node() {
  auto_install_ok || return 1
  local want; want="$(cat "$REPO/.nvmrc" 2>/dev/null | tr -d '[:space:]v')"
  want="${want:-22}"
  echo ">> Node/pnpm not found — installing Node $want via nvm (no root needed)…"
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [ ! -s "$NVM_DIR/nvm.sh" ]; then
    command -v curl >/dev/null 2>&1 || apt_install curl >/dev/null 2>&1 || true
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh \
      | bash >/dev/null 2>&1 || true
  fi
  if [ -s "$NVM_DIR/nvm.sh" ]; then
    # nvm is a shell function; source it in this subshell-safe way.
    # shellcheck disable=SC1090
    set +u; . "$NVM_DIR/nvm.sh"; set -u
    nvm install "$want" >/dev/null 2>&1 || true
    corepack enable pnpm >/dev/null 2>&1 || true
  fi
  resolve_node_bin
}

# ---- LLM CLI discovery ---------------------------------------------------
# Startup must never require a Claude subscription or launch an interactive
# login. Missing authentication leaves the studio usable for settings/browsing.
# Explicit Claude/BYO selections remain available for other deployments.
ensure_llm_on_path() {
  local provider="${BER_LLM_PROVIDER:-codex_cli}" found="" c
  case "$provider" in
    codex_cli)
      for c in "$(command -v codex 2>/dev/null || true)" \
          "$HOME/.local/bin/codex" "${NODE_BIN:-/usr/local/bin}/codex" \
          "$HOME"/.nvm/versions/node/*/bin/codex; do
        if [ -x "$c" ] && [[ "$c" != *.exe && "$c" != /mnt/?/* ]]; then
          found="$c"; break
        fi
      done
      if [ -z "$found" ]; then
        echo "  codex    MISSING — install Codex CLI, then run: codex login"
        return 0
      fi
      export PATH="$(dirname "$found"):$PATH"
      if env -u OPENAI_API_KEY -u CODEX_API_KEY timeout 10 "$found" login status >/dev/null 2>&1; then
        echo "  codex    ok  ->  $found"
      else
        echo "  codex    NOT LOGGED IN — run: codex login"
      fi
      ;;
    claude_cli|claude_token|anthropic_key)
      for c in $(claude_bin_candidates); do
        claude_bin_ok "$c" && { found="$c"; break; }
      done
      if [ -n "$found" ]; then
        export PATH="$(dirname "$found"):$PATH"
        echo "  claude   found  ->  $found (credentials checked when used)"
      else
        echo "  claude   MISSING — selected provider $provider requires Claude CLI"
      fi
      ;;
    openrouter) echo "  llm      OpenRouter (no CLI required)" ;;
    *) echo "!! Unknown BER_LLM_PROVIDER: $provider"; return 1 ;;
  esac
}

# Optional Claude discovery for explicitly selected Claude providers.
claude_bin_candidates() {
  command -v claude 2>/dev/null
  echo "$HOME/.local/bin/claude"          # the official install.sh location
  echo "$HOME/.claude/local/claude"
  echo "/usr/local/bin/claude"
  echo "/usr/bin/claude"
  # npm/nvm global installs (newest node version first)
  if [ -d "$HOME/.nvm/versions/node" ]; then
    local d
    for d in $(ls -1 "$HOME/.nvm/versions/node" 2>/dev/null | sort -Vr); do
      echo "$HOME/.nvm/versions/node/$d/bin/claude"
    done
  fi
  [ -d "$HOME/.volta/bin" ] && echo "$HOME/.volta/bin/claude"
  echo "$HOME/node_modules/.bin/claude"
}

# A usable Linux claude? On WSL, /mnt/c/... entries are the WINDOWS install
# (claude.exe under Windows' PATH, visible through interop). Those cannot run the
# pipeline's bare `claude` from Linux, so reject them explicitly — otherwise we'd
# "find" claude, put a /mnt/c dir on PATH, and fail confusingly at run time.
claude_bin_ok() { # $1 = candidate path
  [ -x "$1" ] || return 1
  case "$1" in
    /mnt/[a-z]/*|*.exe) return 1 ;;
  esac
  return 0
}

# On WSL, a repo under /mnt/c (the Windows drive) breaks PDF conversion — that
# filesystem can't do the symlinks step 6 creates — and is very slow. Warn once;
# don't block, since the studio itself works fine there.
warn_windows_mount() {
  case "$REPO" in
    /mnt/[a-z]/*)
      echo "!! WARNING: this repo lives on the Windows drive ($REPO)."
      echo "   PDF conversion creates symlinks, which fail on /mnt/c, and disk"
      echo "   access is much slower. Recommended: move it to the Linux home —"
      echo "     cp -r \"$REPO\" ~/ && cd ~/$(basename "$REPO")"
      echo ""
      ;;
  esac
}

# ---- machine sizing ---------------------------------------------------------
# Report what this box can take, and cap Node's heap to match.
#
# The pipeline sizes its own fan-out at job start (services/capability.py reads
# MemAvailable each time, so a shared host adapts to current load). This just
# makes that visible at startup and bounds `next dev`, which otherwise targets a
# fraction of visible RAM with no cgroup limit and competes with the pipeline.
report_machine() {
  local py="$PY" line heap
  [ -x "$py" ] || return 0
  line="$(PYTHONPATH="$REPO" "$py" -c \
    'from server.app.services import capability as c; print(c.summary())' 2>/dev/null || true)"
  [ -n "$line" ] && echo "  machine  $line"
  heap="$(PYTHONPATH="$REPO" "$py" -c \
    'from server.app.services import capability as c; print(c.node_heap_mb())' 2>/dev/null || true)"
  if [ -n "$heap" ]; then
    # Inherited by `next dev`; keeps the studio from ballooning on a small box.
    export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--max-old-space-size=$heap"
  fi
  # A machine this small will struggle no matter how we size the work.
  local avail
  avail="$(PYTHONPATH="$REPO" "$py" -c \
    'from server.app.services import capability as c; r=c.available_ram_gb(); print("" if r is None else int(r))' 2>/dev/null || true)"
  if [ -n "$avail" ] && [ "$avail" -lt 6 ] 2>/dev/null; then
    echo "!! Only ${avail}GB of memory is free — PDF conversion is memory-hungry."
    echo "   Work is being scaled down automatically, but expect it to be slow."
    if is_wsl; then
      echo "   WSL2 caps its VM at ~50% of host RAM. To raise it, create"
      echo "   %UserProfile%\\.wslconfig on Windows with:"
      echo "       [wsl2]"
      echo "       memory=12GB"
      echo "   then run 'wsl --shutdown' in PowerShell and start again."
    fi
    echo "   Closing other applications frees memory for the run."
    echo ""
  fi
}

ensure_prereqs() {
  warn_windows_mount
  # Redis: a system service on most machines. Check; if it's down, try to start
  # it the way THIS machine does it (systemd / service / bare daemon).
  if ! redis_up; then
    # redis-cli (redis-tools) and redis-server are SEPARATE packages: having the
    # client says nothing about whether the server is installed. Install either.
    if ! command -v redis-server >/dev/null 2>&1 || ! command -v redis-cli >/dev/null 2>&1; then
      if ! apt_install redis-server redis-tools; then
        echo "!! redis is not installed."
        auto_install_ok || echo "   (re-run with --install-deps to let dev.sh install it via apt)"
        echo "   Debian/Ubuntu/WSL:  sudo apt install -y redis-server redis-tools"
        echo "   macOS (Homebrew):   brew install redis"
        echo "   then re-run ./dev.sh up"
        exit 1
      fi
    fi
    echo ">> redis is not responding on localhost:6379 — trying to start it…"
    if ! try_start_redis; then
      echo "!! could not start redis automatically."
      case "$(init_system)" in
        systemd) echo "   try: sudo systemctl start redis-server" ;;
        sysv)    echo "   try: sudo service redis-server start   (WSL has no systemd by default)" ;;
        *)       echo "   try: redis-server --daemonize yes" ;;
      esac
      echo "   then re-run ./dev.sh up"
      echo "   if that says 'unrecognized service': sudo apt install -y redis-server"
      exit 1
    fi
    echo "   redis is up."
  fi
  # Server venv. Ubuntu ships `venv` as a SEPARATE package, so `python3 -m venv`
  # fails with "ensurepip is not available" on a stock image. Check first —
  # otherwise we leave a broken half-made .venv behind that poisons re-runs.
  if ! venv_ok; then
    # Rebuild rather than repair: a half-made venv (python but no pip) is the
    # usual leftover of an interrupted first run, and patching it is fiddlier
    # and less reliable than recreating it.
    if [ -e "$VENV" ]; then
      echo ">> server/.venv exists but is incomplete (no working pip) — rebuilding…"
      rm -rf "$VENV"
    fi
    if ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
      local pyver
      pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3)"
      # Ubuntu names it pythonX.Y-venv; Debian sometimes only ships python3-venv.
      if ! apt_install "python${pyver}-venv" && ! apt_install python3-venv; then
        echo "!! python3 cannot create virtualenvs (the venv/ensurepip module is"
        echo "   missing)."
        auto_install_ok || echo "   (re-run with --install-deps to let dev.sh install it via apt)"
        echo "   Debian/Ubuntu/WSL:  sudo apt install -y python${pyver}-venv"
        echo "   then re-run ./dev.sh up"
        exit 1
      fi
      if ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
        echo "!! installed python${pyver}-venv but python3 still can't make a venv."
        exit 1
      fi
    fi
    echo ">> creating server/.venv…"
    # A failed venv leaves a partial dir that makes every later run take the
    # "already exists" path and fail confusingly. Clean up on failure.
    if ! python3 -m venv "$VENV" || ! venv_ok; then
      echo "!! venv creation failed — removing the partial $VENV"
      rm -rf "$VENV"
      echo "   fix: sudo apt install -y python3-venv python3-pip, then ./dev.sh up"
      exit 1
    fi
  fi
  # Requirements are checked SEPARATELY from venv creation: an interrupted or
  # failed `pip install` leaves a venv whose python exists but has no deps, and
  # gating this on "does the venv exist" would never install them. pip is fast
  # when everything is already satisfied, so re-running is cheap.
  if ! server_deps_ok; then
    echo ">> installing server requirements into server/.venv…"
    case "$REPO" in
      /mnt/[a-z]/*) echo "   (on the Windows drive this takes a few minutes)" ;;
      *)            echo "   (usually under a minute)" ;;
    esac
    # A freshly-created venv can be missing pip entirely (a partial ensurepip).
    # Bootstrap it before trying to install anything, and say so out loud.
    if ! "$PY" -m pip --version >/dev/null 2>&1; then
      echo "   venv has no pip — bootstrapping via ensurepip…"
      if ! "$PY" -m ensurepip --upgrade 2>&1 | tail -3; then
        echo "!! could not bootstrap pip inside $VENV"
        echo "   fix: sudo apt install -y python3-venv python3-pip"
        echo "   then: rm -rf server/.venv && ./dev.sh up"
        exit 1
      fi
    fi
    # A fresh venv can ship an old pip that misses prebuilt wheels and falls
    # back to compiling from source — slow, and silent for minutes.
    "$PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    # `python -m pip`, not bin/pip: the console script can be missing even when
    # pip itself is importable.
    # NOT -q: quiet pip prints nothing for minutes, which is indistinguishable
    # from a hang (especially on /mnt/c). Show each package as it lands.
    if ! "$PY" -m pip install \
           --progress-bar on \
           --disable-pip-version-check \
           -r "$REPO/server/requirements.txt"; then
      echo "!! pip install failed — the real error is in the output above."
      echo "   Common causes: transient network errors (just re-run ./dev.sh up),"
      echo "   or a venv without pip (rm -rf server/.venv && ./dev.sh up)."
      exit 1
    fi
    if ! server_deps_ok; then
      echo "!! server requirements installed but uvicorn still isn't importable."
      echo "   Try a clean rebuild:  rm -rf server/.venv && ./dev.sh up"
      exit 1
    fi
  fi
  # Node/pnpm + studio deps
  # Node + pnpm: discover first, install only if genuinely absent.
  if ! resolve_node_bin; then
    if ! install_node; then
      echo "!! Node + pnpm not found."
      auto_install_ok || echo "   (re-run with --install-deps to let dev.sh install Node via nvm)"
      echo "   Install Node 22 and enable pnpm:"
      echo "     curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash"
      echo "     exec \$SHELL && nvm install 22 && corepack enable pnpm"
      echo "   …or point dev.sh at an existing install:  NODE_BIN=/path/to/bin ./dev.sh up"
      exit 1
    fi
  fi
  echo "   node/pnpm -> $NODE_BIN"
  # NOTE: test for a COMPLETE install, not merely an existing directory. A
  # cancelled/failed pnpm install leaves a partial node_modules behind; a bare
  # -d check would skip the install and fail later with missing modules.
  # `pnpm install` is idempotent and fast when already complete, so when in
  # doubt we re-run it rather than trusting the directory.
  if ! node_deps_ok; then
    echo ">> installing workspace node deps (pnpm install)…"
    case "$REPO" in
      /mnt/[a-z]/*)
        echo "   NOTE: on the Windows drive this writes ~650 MB across the"
        echo "   WSL/Windows boundary — expect 15-40 min. On the Linux home: ~2 min." ;;
      *) echo "   (first run downloads ~400 packages — a few minutes is normal)" ;;
    esac
    echo "   pnpm's 'Progress:' line rewrites IN PLACE, so it can look frozen —"
    echo "   the heartbeat below proves work is still happening."
    heartbeat_start
    if ! (cd "$REPO" && PATH="$NODE_BIN:$PATH" pnpm install); then
      heartbeat_stop
      echo "!! pnpm install failed."
      echo "   Transient registry errors (ENETUNREACH/ETIMEDOUT) are common —"
      echo "   just re-run ./dev.sh up; pnpm resumes from what it already fetched."
      case "$REPO" in
        /mnt/[a-z]/*) echo "   NOTE: node_modules on the Windows drive is slow and"
                      echo "   more failure-prone. Moving the repo to ~ helps a lot." ;;
      esac
      exit 1
    fi
    heartbeat_stop
    echo "   node deps installed."
  fi
  # Studio env: point the browser's API calls at this machine.
  #
  # This is VALIDATE-AND-CORRECT, not write-once. The old `[ ! -f ]` guard meant a
  # stale/wrong NEXT_PUBLIC_API_BASE (e.g. a WSL box that once recorded the bogus
  # 10.255.255.254 host stub, or a laptop whose LAN IP changed) survived forever:
  # the studio page would load but every API call hung, which looks like a broken
  # app rather than a bad config line.
  ensure_studio_env

  # When the checkout lives under a Dropbox folder, Dropbox races Next's atomic writes:
  # it deletes the .tmp files written into .next mid-build, and every page then
  # 500s with `ENOENT .next/static/development/_buildManifest.js.tmp.<rand>`.
  # The com.dropbox.ignored xattr excludes the dir from syncing, but is LOST
  # whenever .next is deleted — which is exactly what people do to fix a broken
  # Next — so re-apply it on every start rather than trusting it to persist.
  bash "$REPO/studio/scripts/dropbox-ignore.sh" 2>/dev/null || true
}

# Keeps studio/.env.local consistent with the same-origin API proxy.
#
# The studio used to need an absolute NEXT_PUBLIC_API_BASE pointing at this
# machine's LAN IP, and this function existed to keep that value correct as the
# IP moved. The browser now calls the same-origin path /api, which
# next.config.mjs rewrites to the API — so the correct state of this file is to
# have NO pinned base at all, and a leftover one would override the default and
# send the browser back to an address no outside visitor can reach.
#
# Sets ENV_CHANGED=1 so the caller restarts studio and drops .next: NEXT_PUBLIC_*
# is inlined at build time, so a running bundle keeps the old value otherwise.
ensure_studio_env() {
  local f="$REPO/studio/.env.local"
  [ -f "$f" ] || return 0
  grep -q '^NEXT_PUBLIC_API_BASE=' "$f" 2>/dev/null || return 0
  local cur
  cur="$(grep -m1 '^NEXT_PUBLIC_API_BASE=' "$f" | cut -d= -f2-)"
  echo ">> studio/.env.local pins the API at $cur; the app now proxies /api"
  echo "   same-origin. Commenting it out (see studio/.env.local.example)."
  local tmp="$f.tmp.$$"
  sed 's|^NEXT_PUBLIC_API_BASE=|# disabled by dev.sh — the app proxies /api same-origin\n# NEXT_PUBLIC_API_BASE=|' \
    "$f" > "$tmp" && mv "$tmp" "$f"
  ENV_CHANGED=1
}

# ---------- components ---------------------------------------------------------

start_api() {
  if pid=$(read_pidfile api) || pid=$(pid_by_port "$API_PORT" "uvicorn"); then
    echo "  api already running (pid $pid)"; echo "$pid" > "$RUN_DIR/api.pid"; return 0
  fi
  rotate_log api
  # Default: loopback only — the studio proxies /api here, so the API needs no
  # socket anything else can dial. --lan opts in to 0.0.0.0 (e.g. to browse
  # from a phone on the same network).
  # Public: loopback too, plus --proxy-headers so the rate limiter sees the real
  # client address instead of bucketing the whole internet under 127.0.0.1; it
  # is trusted only from loopback, the only place a request can come from.
  local bind_args=(--host "$(listen_host)")
  if [ "$PUBLIC_MODE" = 1 ]; then
    bind_args=(--host 127.0.0.1 --proxy-headers --forwarded-allow-ips=127.0.0.1)
  fi
  (cd "$REPO" && PYTHONPATH="$REPO" nohup "$PY" -m uvicorn server.app.main:app \
      "${bind_args[@]}" --port "$API_PORT" < /dev/null >> "$RUN_DIR/api.log" 2>&1 & echo $! > "$RUN_DIR/api.pid")
  # 10s is not enough on /mnt/c, where importing fastapi/sqlalchemy off the
  # Windows filesystem can take a minute — the old timeout reported failure
  # while uvicorn was still booting and went on to succeed.
  local api_tries=40
  case "$REPO" in /mnt/[a-z]/*) api_tries=240 ;; esac   # up to 2 min there
  if wait_http "http://127.0.0.1:$API_PORT/health" "$api_tries" "the API to start"; then
    if [ "$PUBLIC_MODE" = 1 ]; then
      echo "  api      up  ->  http://127.0.0.1:$API_PORT  (loopback only; docs disabled)"
    else
      echo "  api      up  ->  http://$(bind_host):$API_PORT  (docs: /docs)"
    fi
  else
    # Re-check once: the process may have answered between the last poll and
    # now, and reporting a false failure sends people debugging a working API.
    if curl -sf -m 5 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
      echo "  api      up  ->  http://$(bind_host):$API_PORT  (slow start, but healthy)"
      return 0
    fi
    echo "!! api failed to come up — tail .run/api.log:"; tail -5 "$RUN_DIR/api.log"; exit 1
  fi
}

# Start one arq worker, self-healing to exactly one instance of its settings
# class. $1=name (pidfile/log stem) $2=settings-class $3=finder-fn $4=one-line desc
_start_arq_worker() {
  local name="$1" cls="$2" finder="$3" desc="$4"
  local pids n keep pid
  # Self-heal duplicates: arq load-balances across every worker on the queue, so
  # a lingering worker running STALE code (e.g. after a file-sync revert or a
  # missed restart) can silently grab a job and fail it. Guarantee exactly one.
  pids=($("$finder"))
  n=${#pids[@]}
  keep="$(cat "$RUN_DIR/$name.pid" 2>/dev/null || true)"
  if [ "$n" -eq 1 ] && [ "${pids[0]}" = "$keep" ]; then
    echo "  $name already running (pid ${pids[0]})"; return 0
  fi
  if [ "$n" -gt 0 ]; then
    echo "  found $n $name(s) — restarting to exactly one (avoids stale-code duplicates)…"
    for pid in "${pids[@]}"; do kill_tree "$pid" TERM; done
    sleep 1
    for pid in $("$finder"); do kill_tree "$pid" KILL; done
  fi
  rotate_log "$name"
  (cd "$REPO" && PYTHONPATH="$REPO" nohup "$PY" -m arq "server.app.worker.$cls" \
      < /dev/null >> "$RUN_DIR/$name.log" 2>&1 &)
  sleep 1.5
  # Record the REAL arq pid (arq's process, not the launch subshell) so `status`
  # and pidfile checks are accurate.
  pid="$("$finder" | head -1)"
  if [ -n "$pid" ]; then
    echo "$pid" > "$RUN_DIR/$name.pid"
    echo "  $name   up  ->  $desc"
  else
    echo "!! $name failed — tail .run/$name.log:"; tail -5 "$RUN_DIR/$name.log"; exit 1
  fi
}

start_worker() {
  # LLM queue: pipeline steps, screening/extraction, the background full-text
  # eligibility screen (kind=fulltext_screen) — all share the single Claude CLI
  # subscription, so max_jobs=1 on this queue serializes them.
  _start_arq_worker worker WorkerSettings worker_orphan_pids \
    "runs pipeline + LLM jobs (queue ber:default)"
}

start_pdfworker() {
  # Non-LLM queue: the background full-text FETCH (download + convert). A separate
  # process so PDF work runs CONCURRENTLY with an LLM step instead of queueing
  # behind it — it uses no Claude CLI, only network + GROBID. Without this worker
  # the "Fetching full texts…" job silently never runs.
  _start_arq_worker pdfworker PdfWorkerSettings pdfworker_orphan_pids \
    "runs full-text fetch (queue ber:pdf)"
}

start_studio() {
  if pid=$(read_pidfile studio) || pid=$(pid_by_port "$STUDIO_PORT" "next"); then
    if [ "$ENV_CHANGED" = 1 ]; then
      # It's alive, but serving a bundle built against the OLD API base — that's
      # the "page loads, data never arrives" failure. Restart it.
      echo "  studio restarting (API base changed — the running bundle is stale)…"
      stop_pid "$pid" studio
      rm -f "$RUN_DIR/studio.pid"
    else
      echo "  studio already running (pid $pid)"; echo "$pid" > "$RUN_DIR/studio.pid"; return 0
    fi
  fi
  # NEXT_PUBLIC_* is inlined into the COMPILED chunks, so a changed API base
  # requires dropping .next — a restart alone can still serve the old value.
  # Done here (not only on the restart path) so a studio that was already
  # STOPPED when the base changed doesn't start from a stale cache either.
  # -e not -d so a symlink (even a broken one) is handled. Clear the CONTENTS,
  # never the directory itself: on /mnt/c users may symlink .next to the Linux
  # filesystem for speed, and `rm -rf` on the link would delete the link — Next
  # then recreates a real dir on the slow Windows drive and the speedup silently
  # evaporates. The glob form is correct for a real directory too.
  if [ "$ENV_CHANGED" = 1 ] && [ -e "$REPO/studio/.next" ]; then
    echo "  clearing studio/.next (stale API base compiled in)…"
    rm -rf "$REPO/studio/.next"/* "$REPO/studio/.next"/.[!.]* 2>/dev/null || true
  fi
  rotate_log studio
  if [ "$PUBLIC_MODE" = 1 ]; then
    # A production build, not `next dev`. The dev server serves error overlays
    # containing source, compiles on demand, and carries dev-only routes — none
    # of which belongs on an internet-facing host. Build in the FOREGROUND so a
    # compile error stops the deploy here, loudly, rather than leaving a dead
    # port and a confusing log.
    echo "  studio   building for production (this takes a minute)…"
    if ! (cd "$REPO/studio" && PATH="$NODE_BIN:$PATH" "$NODE_BIN/pnpm" run build \
            >> "$RUN_DIR/studio.log" 2>&1); then
      echo "  studio   BUILD FAILED — see ./dev.sh logs studio"
      return 1
    fi
    (cd "$REPO/studio" && PATH="$NODE_BIN:$PATH" nohup "$NODE_BIN/pnpm" exec next start \
        -p "$STUDIO_PORT" -H 127.0.0.1 < /dev/null >> "$RUN_DIR/studio.log" 2>&1 & echo $! > "$RUN_DIR/studio.pid")
  else
  # --turbopack goes LAST, deliberately. stop_component and the "still compiling"
  # pgrep below both match the literal substring "next dev -p $STUDIO_PORT";
  # writing `next dev --turbopack -p ...` splits that substring, so ./dev.sh down
  # would silently fail and orphan a process holding the port.
  (cd "$REPO/studio" && PATH="$NODE_BIN:$PATH" nohup "$NODE_BIN/pnpm" exec next dev \
      -p "$STUDIO_PORT" -H "$(listen_host)" --turbopack < /dev/null >> "$RUN_DIR/studio.log" 2>&1 & echo $! > "$RUN_DIR/studio.pid")
  fi
  # How long to wait for the port to answer. `next dev` compiles the app on first
  # request/boot, and on /mnt/c (the Windows drive, through the WSL filesystem
  # bridge) that is MINUTES, not seconds — the old 20s budget reported a healthy
  # first run as a failure. Give the Windows drive 5 minutes, native disk 90s.
  local tries=180 budget="90s"
  case "$REPO" in
    /mnt/[a-z]/*) tries=600; budget="5 min (slow because the repo is on /mnt/c)" ;;
  esac
  echo "  studio   starting — first compile can take a while: $budget"
  if wait_http "http://127.0.0.1:$STUDIO_PORT" "$tries" "studio to compile"; then
    echo "  studio   up  ->  http://$(bind_host):$STUDIO_PORT"
    return 0
  fi
  # NOT fatal. Next may still be compiling, and exiting here means the user never
  # sees the URL for the services that ARE up. Report and carry on.
  echo "!! studio hasn't answered on port $STUDIO_PORT yet."
  if pgrep -f "next \(dev\|start\) -p $STUDIO_PORT" >/dev/null 2>&1; then
    echo "   The next dev process is still ALIVE — it is probably still compiling."
    echo "   Watch it finish:   ./dev.sh logs studio"
    echo "   Then just open:    http://$(bind_host):$STUDIO_PORT"
  else
    echo "   The next dev process has EXITED — this is a real error:"
    tail -20 "$RUN_DIR/studio.log" | sed 's/^/   | /'
    echo "   Full log: .run/studio.log"
  fi
  STUDIO_SLOW=1
  return 0
}

stop_component() { # $1=name $2=port-or-"" $3=cmdline-substring $4=kill-pattern
  local name="$1" port="$2" pat="$3" kpat="$4" pid found=0 i
  if pid=$(read_pidfile "$name"); then found=1; stop_pid "$pid" "$name"; fi
  # Scoped pattern kill: wrappers + hand-started instances of THIS component only.
  # (Safe in a script: our own cmdline is "bash ./dev.sh …", never matches.)
  if [ -n "$kpat" ] && pgrep -f "$kpat" >/dev/null 2>&1; then
    found=1
    for pid in $(pgrep -f "$kpat"); do kill_tree "$pid" TERM; done
    sleep 1
    for pid in $(pgrep -f "$kpat"); do kill_tree "$pid" KILL; done
  fi
  # Port sweep: whatever still holds the port (verified by cmdline) dies too.
  if [ -n "$port" ]; then
    for i in 1 2 3; do
      pid="$(pid_by_port "$port" "$pat" || true)"
      [ -n "$pid" ] || break
      found=1; kill_tree "$pid" KILL; sleep 0.5
    done
  fi
  [ "$found" = 1 ] && echo "  $name stopped." || echo "  $name not running."
  rm -f "$RUN_DIR/$name.pid"
}

# ---------- commands -----------------------------------------------------------

# Export the repo's .env.local so the API + workers (and every pipeline
# subprocess they spawn) see the data-source API keys. Deliberately does NOT
# override a var already exported in the caller's shell, matching python-dotenv.
load_env_local() {
  local env_file="$REPO/.env.local"
  [ -f "$env_file" ] || { echo "  keys     --  no .env.local (API keys unset)"; return 0; }
  local n=0 line key val
  while IFS= read -r line || [ -n "$line" ]; do
    # Skip blanks, comments, and anything that isn't KEY=VALUE.
    case "$line" in ''|'#'*) continue ;; esac
    [ "${line#*=}" = "$line" ] && continue
    key=${line%%=*}
    val=${line#*=}
    # Trim surrounding whitespace and optional quotes around the value.
    key=$(printf '%s' "$key" | tr -d '[:space:]')
    val=${val#\"}; val=${val%\"}; val=${val#\'}; val=${val%\'}
    # Only valid shell variable names: [A-Za-z_][A-Za-z0-9_]*. A key starting
    # with a digit (or carrying any other character) is skipped, never
    # evaluated.
    case "$key" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac
    # Indirect expansion instead of eval: the value is never re-parsed.
    if [ -z "${!key:-}" ]; then
      export "$key=$val"
      n=$((n + 1))
    fi
  done < "$env_file"
  echo "  keys     ok  ->  exported $n var(s) from .env.local"
}

# True when server/.env has PUBLIC_MODE on (last assignment wins, matching
# pydantic-settings). Used to refuse `up` without --public.
_server_env_public_mode() {
  local f="$REPO/server/.env" v
  [ -f "$f" ] || return 1
  v=$(grep -E '^[[:space:]]*PUBLIC_MODE=' "$f" | tail -1 | cut -d= -f2- || true)
  v=$(printf '%s' "$v" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  v=${v#\"}; v=${v%\"}; v=${v#\'}; v=${v%\'}
  case "$v" in true|1|yes) return 0 ;; *) return 1 ;; esac
}

# Is JWT_SECRET (env first, then server/.env, last assignment wins) missing or
# the documented placeholder? The API itself refuses to start that way on a
# non-loopback bind (config.public_posture_errors); this says so up front.
_jwt_secret_is_placeholder() {
  local v="${JWT_SECRET:-}" f="$REPO/server/.env"
  if [ -z "$v" ] && [ -f "$f" ]; then
    v=$(grep -E '^[[:space:]]*JWT_SECRET=' "$f" | tail -1 | cut -d= -f2- || true)
    v=$(printf '%s' "$v" | tr -d '[:space:]')
    v=${v#\"}; v=${v%\"}; v=${v#\'}; v=${v%\'}
  fi
  case "$v" in ''|dev-insecure-change-me|change-me-in-prod) return 0 ;; *) return 1 ;; esac
}

cmd_up() {
  echo "== Bird's Eye Review studio — starting =="
  if [ "$PUBLIC_MODE" = 1 ] && [ "$LAN" = 1 ]; then
    echo "!! --public and --lan contradict each other (--public binds loopback"
    echo "   behind the tunnel). Pick one."
    exit 1
  fi
  if [ "$LAN" = 1 ]; then
    if _jwt_secret_is_placeholder; then
      echo "!! --lan would expose the API to your network, but JWT_SECRET is unset"
      echo "   or the documented placeholder — anyone on the network could forge"
      echo "   an admin token. Set one in server/.env first:"
      echo "     echo \"JWT_SECRET=\$(openssl rand -hex 32)\" >> server/.env"
      exit 1
    fi
    echo "!! WARNING: --lan binds the API and studio on 0.0.0.0. Anyone who can"
    echo "   reach this machine on the network can reach the sign-in page."
    export BIND_HOST=0.0.0.0
  else
    export BIND_HOST=127.0.0.1
  fi
  if [ "$PUBLIC_MODE" = 1 ]; then
    # Env outranks server/.env, so this is what the API/workers actually see.
    # Keep the shell flag as 1 so later `[ "$PUBLIC_MODE" = 1 ]` still holds;
    # pydantic-settings coerces "1" to True the same as "true".
    export PUBLIC_MODE=1
    export TRUST_PROXY_HEADERS=1
    echo "  public   on  ->  loopback bind, PUBLIC_MODE=true"
  elif _server_env_public_mode; then
    echo "!! server/.env has PUBLIC_MODE=true but this is not --public."
    echo "   A normal up runs the dev studio server with public-posture settings."
    echo "   Re-run: ./dev.sh up --public"
    exit 1
  fi
  ensure_prereqs
  # After ensure_prereqs: needs the server venv, and must precede start_studio
  # so `next dev` inherits the NODE_OPTIONS heap cap it sets.
  report_machine
  echo "  redis    ok  ->  localhost:6379"
  # Load provider selection before checking its CLI; workers inherit this PATH.
  load_env_local
  ensure_llm_on_path
  start_api
  start_worker
  start_pdfworker
  start_studio
  echo ""
  # Only --lan listens on the LAN IP; otherwise printing it would send the
  # reader to a port that is (correctly) refusing connections.
  local open_url="http://$(bind_host):$STUDIO_PORT"
  [ "$PUBLIC_MODE" = 1 ] && open_url="http://127.0.0.1:$STUDIO_PORT  (point the tunnel here)"
  if [ "$STUDIO_SLOW" = 1 ]; then
    echo "Everything except studio is up. Studio is still starting —"
    echo "  watch:  ./dev.sh logs studio      (wait for 'Ready in …')"
    echo "  then open:  $open_url"
  else
    echo "Open:   $open_url"
  fi
  echo "Stop:   ./dev.sh down     Logs: ./dev.sh logs api|worker|pdfworker|studio"
}

cmd_down() {
  echo "== Bird's Eye Review studio — stopping =="
  # Two patterns, because --public runs `next start` and normal dev runs
  # `next dev`; matching only one would leave the other holding the port.
  stop_component studio "$STUDIO_PORT" "next" "next dev -p $STUDIO_PORT"
  pkill -f "next start -p $STUDIO_PORT" >/dev/null 2>&1 || true
  # Anchor each worker pattern to its exact class ($ end-of-line): "WorkerSettings"
  # is a suffix of "PdfWorkerSettings", so an unanchored worker pattern would also
  # kill the PDF worker (and, worse, leave it half-adopted). arq invokes each as
  # "…worker.<Class>" with no trailing args, so $ anchors cleanly.
  stop_component pdfworker "" "" "arq server\.app\.worker\.PdfWorkerSettings\$"
  stop_component worker    "" "" "arq server\.app\.worker\.WorkerSettings\$"
  stop_component api "$API_PORT" "uvicorn" "uvicorn server\.app\.main:app"
  echo "Done. (redis is a system service and is left running.)"
}

cmd_status() {
  echo "== status =="
  redis_up && echo "  redis    up   localhost:6379" || echo "  redis    DOWN"
  load_env_local
  ensure_llm_on_path
  local pid
  if pid=$(read_pidfile api)    || pid=$(pid_by_port "$API_PORT" "uvicorn"); then
    echo "  api      up   pid $pid  http://$(bind_host):$API_PORT"
  else echo "  api      down"; fi
  if pid=$(read_pidfile worker) || { pid="$(worker_orphan_pid)"; [ -n "$pid" ]; }; then
    echo "  worker   up   pid $pid  (queue ber:default)"
  else echo "  worker   down"; fi
  if pid=$(read_pidfile pdfworker) || { pid="$(pdfworker_orphan_pid)"; [ -n "$pid" ]; }; then
    echo "  pdfworker up   pid $pid  (queue ber:pdf)"
  else echo "  pdfworker down"; fi
  if pid=$(read_pidfile studio) || pid=$(pid_by_port "$STUDIO_PORT" "next"); then
    echo "  studio   up   pid $pid  http://$(bind_host):$STUDIO_PORT"
  else echo "  studio   down"; fi
}

cmd_logs() {
  local name="${1:-api}"
  case "$name" in api|worker|pdfworker|studio) ;;
    *) echo "usage: ./dev.sh logs api|worker|pdfworker|studio"; exit 1;; esac
  exec tail -f "$RUN_DIR/$name.log"
}

# Split flags from positionals so `--public` can follow `up`/`restart` while
# `logs api` still gets its argument. A single loop over "$@" would swallow
# "api" as an unknown option.
_cmd="${1:-}"
shift || true
_args=()
for _arg in "$@"; do
  case "$_arg" in
    --public) PUBLIC_MODE=1 ;;
    --lan) LAN=1 ;;
    --install-deps) AUTO_INSTALL=1 ;;
    -*) echo "unknown option: $_arg"; exit 1 ;;
    *) _args+=("$_arg") ;;
  esac
done

case "$_cmd" in
  up)      cmd_up ;;
  down)    cmd_down ;;
  status)  cmd_status ;;
  logs)    cmd_logs "${_args[0]:-api}" ;;
  restart) cmd_down; echo ""; cmd_up ;;
  *) sed -n '2,24p' "$0"; exit 1 ;;
esac
