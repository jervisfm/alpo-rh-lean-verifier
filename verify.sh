#!/usr/bin/env bash
# Native automated verifier for the Lean 4 Riemann Hypothesis / GRH proof claim
# linked from https://alpo.ge/remark.pdf
#
# Designed to start cleanly on a fresh Ubuntu 20.04 LTS (Focal Fossa) system,
# running natively (no Docker/VM overhead) and politely to system resources.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Quick pass-through for --help / -h without touching apt or elan
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      if command -v python3 >/dev/null 2>&1; then
        exec python3 "$SCRIPT_DIR/verify_rh.py" --help
      fi
      echo "Usage: ./verify.sh [OPTIONS]"
      echo "Installs native prerequisites (on Ubuntu 20.04+), installs elan/Lean 4,"
      echo "downloads the alpo.ge Lean archive, compiles the proof with progress/ETA,"
      echo "audits axioms & theorem statements, and replays through leanchecker."
      exit 0
      ;;
  esac
done

NO_APT=0
NICE_LEVEL=19
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    --no-apt)
      NO_APT=1
      ;;
    --nice)
      if (( i + 1 < ${#args[@]} )); then
        NICE_LEVEL="${args[$((i + 1))]}"
      fi
      ;;
    --nice=*)
      NICE_LEVEL="${args[$i]#--nice=}"
      ;;
  esac
done

log() {
  printf '[verify.sh] %s\n' "$*"
}

warn() {
  printf '[verify.sh] WARNING: %s\n' "$*" >&2
}

# Raise open-file descriptor limit for building 175,000+ Lean modules on Ubuntu 20.04
# (Ubuntu 20.04 defaults to ulimit -n 1024, which can trigger EMFILE during large builds.)
raise_nofile_limit() {
  local hard_limit target
  hard_limit="$(ulimit -Hn 2>/dev/null || echo 1024)"
  if [[ "$hard_limit" == "unlimited" ]]; then
    ulimit -Sn 65536 2>/dev/null || true
  elif [[ "$hard_limit" =~ ^[0-9]+$ ]]; then
    target=65536
    if (( hard_limit < target )); then
      target="$hard_limit"
    fi
    ulimit -Sn "$target" 2>/dev/null || true
  fi
}

# Install missing native system packages on Ubuntu/Debian
ensure_system_packages() {
  local missing=()
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  command -v git >/dev/null 2>&1 || missing+=(git)
  command -v tar >/dev/null 2>&1 || missing+=(tar)
  command -v gzip >/dev/null 2>&1 || missing+=(gzip)
  command -v python3 >/dev/null 2>&1 || missing+=(python3)
  command -v nice >/dev/null 2>&1 || missing+=(coreutils)

  local optional_missing=()
  command -v pigz >/dev/null 2>&1 || optional_missing+=(pigz)
  command -v ionice >/dev/null 2>&1 || optional_missing+=(util-linux)

  if (( ${#missing[@]} == 0 && ${#optional_missing[@]} == 0 )); then
    return 0
  fi

  if (( NO_APT == 1 )); then
    if (( ${#missing[@]} > 0 )); then
      warn "Missing required tools (${missing[*]}) and --no-apt was specified."
      exit 1
    fi
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    if (( ${#missing[@]} > 0 )); then
      warn "Missing required tools (${missing[*]}), and apt-get is unavailable. Please install them manually."
      exit 1
    fi
    return 0
  fi

  local apt_prefix=()
  if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      apt_prefix=(sudo)
    else
      if (( ${#missing[@]} > 0 )); then
        warn "Missing required tools (${missing[*]}), not root, and sudo is unavailable."
        exit 1
      fi
      return 0
    fi
  fi

  local to_install=(ca-certificates "${missing[@]}" "${optional_missing[@]}")
  log "Installing native Ubuntu packages: ${to_install[*]}"
  DEBIAN_FRONTEND=noninteractive "${apt_prefix[@]}" apt-get update -qq
  DEBIAN_FRONTEND=noninteractive "${apt_prefix[@]}" apt-get install -y -qq --no-install-recommends "${to_install[@]}" || {
    if (( ${#missing[@]} > 0 )); then
      warn "Failed to install required packages: ${missing[*]}"
      exit 1
    fi
    warn "Optional package installation failed; continuing with standard tools."
  }
}

# Install native elan (Lean 4 toolchain manager) if not already installed.
# elan-init is statically linked and runs natively on Ubuntu 20.04 (glibc 2.31).
ensure_elan() {
  export ELAN_HOME="${ELAN_HOME:-$HOME/.elan}"
  if [[ -d "$ELAN_HOME/bin" ]]; then
    export PATH="$ELAN_HOME/bin:$PATH"
  fi

  if command -v elan >/dev/null 2>&1; then
    log "Using native elan: $(elan --version 2>/dev/null || echo 'installed')"
    return 0
  fi

  log "Installing native elan (Lean toolchain manager) into $ELAN_HOME ..."
  local tmp_installer
  tmp_installer="$(mktemp /tmp/elan-init.XXXXXX.sh)"
  trap 'rm -f "$tmp_installer"' EXIT

  curl --proto '=https' --tlsv1.2 -fsSL \
    --retry 5 --retry-delay 2 \
    https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    -o "$tmp_installer"

  sh "$tmp_installer" -y --default-toolchain none --no-modify-path
  rm -f "$tmp_installer"
  trap - EXIT

  export PATH="$ELAN_HOME/bin:$PATH"
  if ! command -v elan >/dev/null 2>&1; then
    warn "elan installation completed but elan was not found in $ELAN_HOME/bin"
    exit 1
  fi
  log "Installed native elan: $(elan --version)"
}

raise_nofile_limit
ensure_system_packages
ensure_elan

# Run the orchestrator nicely so the host system stays responsive
exec nice -n "$NICE_LEVEL" python3 "$SCRIPT_DIR/verify_rh.py" "$@"
