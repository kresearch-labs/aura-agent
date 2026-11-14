#!/bin/sh
# Aura installer — macOS, Linux, WSL, BSD, containers (POSIX sh).
#
#   curl -fsSL https://github.com/kresearch-labs/aura-agent/releases/latest/download/install.sh | sh
#                                     the newest release, then `aura setup`
#   sh scripts/install.sh             from the Aura folder it sits in
#   sh install.sh --from=FILE.whl     from a downloaded package (or --from=FOLDER)
#   sh install.sh --version=0.2.0     a given release
#   sh install.sh --dry-run           show what would happen; change nothing
#   sh install.sh --yes               don't ask (scripts, containers, CI)
#
# What it does:
#   1. uv, pinned and checked: the release archive from GitHub, its SHA-256
#      compared with the value written below (not with a file from the same
#      server) — into ~/.aura/bin only, nothing on PATH.
#   2. a private Python (uv-managed) — your system Python is never touched,
#      so "externally-managed-environment" (PEP 668) and old system Pythons
#      (Ubuntu 22.04 has 3.10) don't matter.
#   3. Aura into its own environment (uv tool), the `aura` command into
#      ~/.local/bin. From a release, Aura's package is checked against the
#      release's SHA256SUMS first — a mismatch installs nothing.
#   4. checks `aura --version` really runs; writes ~/.aura/install.json so
#      `aura update` / `aura uninstall` know the layout.
# Nothing outside ~/.aura and ~/.local/bin/aura is changed, unless you say
# yes to adding ~/.local/bin to PATH. A log: ~/.aura/install.log
set -eu

UV_VERSION="0.12.24"
PYTHON_VERSION="3.12"
EXTRAS=""
AURA_REPO="kresearch-labs/aura-agent"
AURA_TAG=""                   # written into the copy attached to a release (vX.Y.Z); empty = the newest
FROM=""
VERSION=""
YES=0
DRY=0
SETUP=1

for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --dry-run) DRY=1 ;;
    --no-setup) SETUP=0 ;;
    --from=*) FROM="${a#--from=}" ;;
    --version=*) VERSION="${a#--version=}"; VERSION="${VERSION#v}" ;;
    --extras=*) EXTRAS="${a#--extras=}" ;;
    --python=*) PYTHON_VERSION="${a#--python=}" ;;
    -h|--help) if [ -f "$0" ]; then sed -n '2,26p' "$0"; else echo "options: --from= --version= --dry-run --yes --no-setup --extras= --python="; fi; exit 0 ;;
    *) echo "unknown option: $a (try --help)"; exit 2 ;;
  esac
done

AURA_HOME="${AURA_HOME:-$HOME/.aura}"
BIN_DIR="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
LOG="$AURA_HOME/install.log"
STEP=0
STEPS=5

STARTED=0                     # nothing is written before you say go
say() { printf '%s\n' "$*"; if [ "$STARTED" = 1 ]; then printf '%s\n' "$*" >>"$LOG" 2>/dev/null || true; fi; }
step() { STEP=$((STEP + 1)); detail ""; detail "[$STEP/$STEPS] $*"; }
# what the user sees: one calm line per part; everything else goes to the log
detail() { if [ "$STARTED" = 1 ]; then printf '%s\n' "$*" >>"$LOG" 2>/dev/null || true; fi; }
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$(printf '\033[32m'); C_DIM=$(printf '\033[2m'); C_HEAD=$(printf '\033[36m')
  C_ERR=$(printf '\033[31m'); C_END=$(printf '\033[0m')
else C_OK=""; C_DIM=""; C_HEAD=""; C_ERR=""; C_END=""; fi
ok() { printf '  %s✓%s %s\n' "$C_OK" "$C_END" "$*"; detail "ok: $*"; }
dim() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_END"; detail "$*"; }
card() {
  # what failed, why, what to do — never a bare stack
  say ""
  printf '  %s✗ %s%s\n' "$C_ERR" "$1" "$C_END"; detail "x $1"
  [ -n "${2:-}" ] && say "    why: $2"
  [ -n "${3:-}" ] && say "    fix: $3"
  if [ "$STARTED" = 1 ]; then dim "details: $LOG"; fi
  exit 1
}
# questions: from the terminal itself, so `curl … | sh` can still ask
CAN_ASK=0
if [ "$YES" = 0 ]; then
  if [ -t 0 ]; then CAN_ASK=1
  elif [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then CAN_ASK=2; fi
fi
ask() {   # ask "question" → yes unless the answer is no
  if [ "$CAN_ASK" = 1 ]; then printf '%s' "$1"; read -r ans || ans=""
  else printf '%s' "$1"; read -r ans </dev/tty || ans=""; fi
  case "$ans" in n|N|no|NO) return 1 ;; *) return 0 ;; esac
}
aura_folder() {   # a folder counts only if it really is Aura
  [ -n "$1" ] && [ -f "$1/pyproject.toml" ] && grep -q '^name = "aura-agent"' "$1/pyproject.toml" \
    && (cd "$1" && pwd)
}

# ---------------------------------------------------------------- checks --
OS=$(uname -s)
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) card "this processor ($ARCH) has no uv build" "uv ships x86_64 and aarch64" \
          "install Python 3.11+ yourself, then: pip install aura-agent" ;;
esac
case "$OS" in
  Linux)
    LIBC=gnu
    if ldd --version 2>&1 | grep -qi musl || ls /lib/ld-musl-* >/dev/null 2>&1; then LIBC=musl; fi
    TARGET="$ARCH-unknown-linux-$LIBC" ;;
  Darwin) TARGET="$ARCH-apple-darwin" ;;
  MINGW*|MSYS*|CYGWIN*)
    card "this is Windows (a Git Bash / MSYS shell)" "Windows has its own installer" \
         "in PowerShell: irm https://github.com/$AURA_REPO/releases/latest/download/install.ps1 | iex" ;;
  *) card "this system ($OS) isn't one uv builds for" "uv ships Linux, macOS and Windows" \
          "install Python 3.11+ yourself, then: pip install aura-agent" ;;
esac

# the published SHA-256 of each uv $UV_VERSION archive (github.com/astral-sh/uv releases)
case "$TARGET" in
  x86_64-unknown-linux-gnu)   SUM=b4dfaef47d491a7296981f8374a4595f55dbf84e8937c8ecd2983574d8bb3da6 ;;
  aarch64-unknown-linux-gnu)  SUM=5231be65f496304623895dacdbf1de8504fec90303684bdf05805aa34414dd21 ;;
  x86_64-unknown-linux-musl)  SUM=48170bd200a5430298c18f3b264485a1f3a8605f01088277daf6e37633edf0f5 ;;
  aarch64-unknown-linux-musl) SUM=7f9ab4726d743b6a92e310477a7840c8a8a7b3938cb3c4657d541342fca75011 ;;
  x86_64-apple-darwin)        SUM=4fa82e37cb94767661f532b001e470b67a186c7260e305bd84ddb78fd545c0b6 ;;
  aarch64-apple-darwin)       SUM=0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732 ;;
esac

if command -v curl >/dev/null 2>&1; then FETCH="curl -fsSL --retry 3 -o"
elif command -v wget >/dev/null 2>&1; then FETCH="wget -q -O"
else card "neither curl nor wget is here" "one is needed to download uv" \
          "install curl (e.g. apt install curl, apk add curl)"; fi
if command -v sha256sum >/dev/null 2>&1; then HASH="sha256sum"
elif command -v shasum >/dev/null 2>&1; then HASH="shasum -a 256"
else card "no SHA-256 tool (sha256sum / shasum)" "downloads are checked before they are used" \
          "install coreutils (or perl's shasum)"; fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# where Aura comes from: --from (a folder or a package), the Aura folder this
# script sits in, or a release (checked against its SHA256SUMS)
KIND=""; SRC=""; WHEEL=""; BASE=""; WHEEL_SUM=""; AURA_VER=""
if [ -n "$FROM" ]; then
  case "$FROM" in
    *.whl) [ -f "$FROM" ] || card "no package at $FROM" "" "check the path"
           KIND=wheel; WHEEL=$(cd "$(dirname "$FROM")" && pwd)/$(basename "$FROM") ;;
    *) SRC=$(aura_folder "$FROM") || card "no Aura at $FROM" "the folder has no Aura pyproject.toml" \
                                          "give the unzipped Aura folder, or a .whl package"
       KIND=folder ;;
  esac
elif [ -f "$0" ] && SRC=$(aura_folder "$(dirname "$0")/.."); then
  KIND=folder
else
  KIND=release
  if [ -n "${AURA_RELEASE_URL:-}" ]; then BASE="${AURA_RELEASE_URL%/}"
  elif [ -n "$VERSION" ]; then BASE="https://github.com/$AURA_REPO/releases/download/v$VERSION"
  elif [ -n "$AURA_TAG" ]; then BASE="https://github.com/$AURA_REPO/releases/download/$AURA_TAG"
  else BASE="https://github.com/$AURA_REPO/releases/latest/download"; fi
  $FETCH "$TMP/SHA256SUMS" "$BASE/SHA256SUMS" 2>/dev/null || card "couldn't read the release" "$BASE/SHA256SUMS" \
    "check the connection (a proxy? set HTTPS_PROXY) and the version, then run again"
  LINES=$(grep -E '^[0-9a-f]{64} +\*?aura_agent-[0-9][^ ]*-py3-none-any\.whl$' "$TMP/SHA256SUMS" | tr -d '\r' || true)
  [ -n "$LINES" ] && [ "$(printf '%s\n' "$LINES" | wc -l | tr -d ' ')" = 1 ] || \
    card "the release's SHA256SUMS doesn't name exactly one Aura package" "$BASE/SHA256SUMS" "this release looks broken — try another version"
  WHEEL_SUM=$(printf '%s' "$LINES" | cut -d' ' -f1)
  WHEEL=$(printf '%s' "$LINES" | sed 's/^[0-9a-f]* *\*\{0,1\}//')
  AURA_VER=$(printf '%s' "$WHEEL" | sed 's/^aura_agent-//; s/-py3-none-any\.whl$//')
fi

UV="$AURA_HOME/bin/uv"
HAVE_UV=0
if [ -x "$UV" ] && "$UV" --version 2>/dev/null | grep -q " $UV_VERSION"; then HAVE_UV=1; fi

case "$KIND" in
  folder)  FROM_LINE="from: $SRC" ;;
  wheel)   FROM_LINE="from: $WHEEL" ;;
  *)       FROM_LINE="from: the release $AURA_VER — $BASE (checked against SHA256SUMS)" ;;
esac
INFO="Aura installer — $OS $ARCH
$FROM_LINE
into: $AURA_HOME (program: bin/ python/ tools/ cache/ releases/ — your data stays apart)
command: $BIN_DIR/aura
uv: $UV_VERSION ($TARGET)$( [ $HAVE_UV = 1 ] && echo ' — already here' || echo ', about 20 MB, SHA-256 checked')
Python: $PYTHON_VERSION, private to Aura
extras: ${EXTRAS:-none}"
printf '\n  %sAura%s%s\n\n' "$C_HEAD" "${AURA_VER:+ $AURA_VER}" "$C_END"
say "  Installs for you only — no admin needed. About 60 MB."
dim "It adds the 'aura' command, and keeps your data apart from the program."
if [ "$DRY" = 1 ]; then
  say ""
  printf '%s\n' "$INFO" | sed 's/^/  /'
  say ""
  say "  dry run — nothing was installed or changed"
  exit 0
fi
if [ "$CAN_ASK" != 0 ]; then
  say ""
  ask '  Install now? [Y/n] ' || { echo "  nothing changed."; exit 1; }
fi
say ""
mkdir -p "$AURA_HOME/bin"
: >"$LOG"
STARTED=1
detail "install started $(date)"
detail "$INFO"

# -------------------------------------------------------------------- uv --
step "uv $UV_VERSION"
if [ "$HAVE_UV" = 1 ]; then
  detail "  already here: $UV"
else
  URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$TARGET.tar.gz"
  $FETCH "$TMP/uv.tar.gz" "$URL" >>"$LOG" 2>&1 || card "couldn't download uv" "$URL" \
    "check the connection (a proxy? set HTTPS_PROXY), then run the installer again"
  GOT=$($HASH "$TMP/uv.tar.gz" | cut -d' ' -f1)
  [ "$GOT" = "$SUM" ] || card "the uv download doesn't match its published checksum" \
    "expected $SUM, got $GOT — it may be damaged or not the real file" "run again; if it repeats, don't use this network"
  mkdir -p "$TMP/uvx"
  tar -xzf "$TMP/uv.tar.gz" -C "$TMP/uvx" >>"$LOG" 2>&1 || card "couldn't unpack uv" "" "run again"
  FOUND=$(find "$TMP/uvx" -type f -name uv | head -n 1)
  [ -n "$FOUND" ] || card "uv wasn't in the archive" "" "run again"
  cp "$FOUND" "$UV"
  chmod +x "$UV"
  detail "  checked (SHA-256) and placed: $UV"
fi

# ------------------------------------------------------- python + aura --
step "Python $PYTHON_VERSION and Aura"
if [ "$KIND" = release ]; then
  # kept in releases/<version>/ — `aura update` can go back to it
  KEEP="$AURA_HOME/releases/$AURA_VER"
  mkdir -p "$KEEP"
  $FETCH "$KEEP/$WHEEL" "$BASE/$WHEEL" >>"$LOG" 2>&1 || card "couldn't download Aura's package" "$BASE/$WHEEL" \
    "check the connection, then run again"
  GOT=$($HASH "$KEEP/$WHEEL" | cut -d' ' -f1)
  if [ "$GOT" != "$WHEEL_SUM" ]; then
    rm -f "$KEEP/$WHEEL"
    card "Aura's package doesn't match the release's checksum" \
      "expected $WHEEL_SUM, got $GOT — it may be damaged or not the real file" \
      "nothing was installed; run again, and if it repeats, don't use this network"
  fi
  detail "  checked (SHA-256): $WHEEL"
  ok "Downloaded and checked"
  WHEEL="$KEEP/$WHEEL"
fi
export UV_TOOL_DIR="$AURA_HOME/tools"
export UV_PYTHON_INSTALL_DIR="$AURA_HOME/python"
export UV_PYTHON_INSTALL_BIN=0          # no python on PATH from us
export UV_CACHE_DIR="$AURA_HOME/cache"
export UV_TOOL_BIN_DIR="$BIN_DIR"
# a file:// address can't hold a space ("My Projects") — encode it
if [ "$KIND" = folder ]; then P="$SRC"; else P="$WHEEL"; fi
P_URL=$(printf '%s' "$P" | sed 's/%/%25/g; s/ /%20/g')
SPEC="aura-agent[$EXTRAS] @ file://$P_URL"
[ -n "$EXTRAS" ] || SPEC="aura-agent @ file://$P_URL"
"$UV" tool install --force --managed-python --python "$PYTHON_VERSION" "$SPEC" >>"$LOG" 2>&1 || \
  card "Aura didn't install" "see the log's last lines" "tail -n 30 '$LOG'"
ok "Installed"

# ------------------------------------------------------------------ PATH --
step "the aura command"
NEW_TERMINAL=0
case ":$PATH:" in
  *":$BIN_DIR:"*) detail "  $BIN_DIR is on PATH" ;;
  *)
    # "Install now?" said it adds the aura command — that yes covers it
    if [ "$YES" = 1 ] || [ "$CAN_ASK" != 0 ]; then
      if "$UV" tool update-shell >>"$LOG" 2>&1; then NEW_TERMINAL=1; detail "  added $BIN_DIR to the shell start-up file"
      else say "  ! couldn't add $BIN_DIR to PATH — add it yourself"; fi
    else
      detail "  $BIN_DIR isn't on PATH (no terminal to ask)"
    fi ;;
esac

# ---------------------------------------------------------------- verify --
step "check"
VER=$("$BIN_DIR/aura" --version 2>>"$LOG") || card "the aura command doesn't run" "" "tail -n 30 '$LOG'"
detail "  $VER runs"
if [ "$NEW_TERMINAL" = 1 ]; then ok "The 'aura' command is ready (in new terminals)"
else ok "The 'aura' command is ready"; fi
NOW=$(date +%s)
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
if [ "$KIND" = folder ]; then SOURCE="$SRC"; else SOURCE="$WHEEL"; fi
cat >"$AURA_HOME/install.json" <<EOF
{"method": "uv-tool", "kind": "$KIND", "source": "$(esc "$SOURCE")",
 "release_base": "$(esc "$BASE")", "version": "$(esc "$AURA_VER")", "repo": "$AURA_REPO",
 "uv": "$(esc "$UV")", "uv_version": "$UV_VERSION",
 "python": "$PYTHON_VERSION", "extras": "$(esc "$EXTRAS")", "bin_dir": "$(esc "$BIN_DIR")",
 "tool_dir": "$(esc "$UV_TOOL_DIR")", "python_dir": "$(esc "$UV_PYTHON_INSTALL_DIR")",
 "cache_dir": "$(esc "$UV_CACHE_DIR")", "at": $NOW}
EOF
detail "  layout saved: $AURA_HOME/install.json (aura update / aura uninstall read it)"

# ----------------------------------------------------------------- setup --
step "setup"
if [ "$SETUP" = 1 ] && [ "$CAN_ASK" != 0 ]; then
  if [ "$CAN_ASK" = 1 ]; then "$BIN_DIR/aura" setup || dim "setup stopped — run 'aura setup' any time"
  else "$BIN_DIR/aura" setup </dev/tty || dim "setup stopped — run 'aura setup' any time"; fi
else
  say ""
  say "  Next: aura setup"
fi
say ""
if [ "$NEW_TERMINAL" = 1 ]; then printf '  %sOpen a new terminal, then type:  aura chat%s\n' "$C_HEAD" "$C_END"
else printf '  %sStart with:  aura chat%s\n' "$C_HEAD" "$C_END"; fi
dim "update: aura update  ·  remove: aura uninstall (your data stays unless --purge)"
