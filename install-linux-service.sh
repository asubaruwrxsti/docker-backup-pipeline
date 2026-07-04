#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="docker-volume-backup"
INSTALL_DIR="/opt/docker-volume-backup"
CONFIG_DIR="/etc/docker-volume-backup"
SYSTEMD_DIR="/etc/systemd/system"
PYTHON_BIN="$(command -v python3 || true)"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="$SOURCE_DIR/backup_docker_volumes.py"
EXAMPLE_CONFIG="$SOURCE_DIR/backup-config.example.json"
CONFIG_SOURCE=""
START_NOW="auto"
FORCE="false"
RCLONE_BIN="$(command -v rclone || true)"
RCLONE_CONFIG_PATH=""
WORK_DIR=""
TPS_LIMIT="4"

usage() {
  cat <<'EOF'
Install backup_docker_volumes.py as a systemd service.

Usage:
  sudo ./install-linux-service.sh [options]

Options:
  --service-name NAME        Service name (default: docker-volume-backup)
  --install-dir PATH         Install dir for script (default: /opt/docker-volume-backup)
  --config-dir PATH          Config dir (default: /etc/docker-volume-backup)
  --script-source PATH       Source backup script path
  --config-source PATH       Source config JSON path (optional)
  --python-bin PATH          Python binary to use (default: auto-detect python3)
  --rclone-bin PATH          rclone binary to use (default: auto-detect rclone)
  --rclone-config PATH       rclone config path for the service (default: auto-detect)
  --work-dir PATH            Temp/work dir for archives (default: auto-detect)
  --tpslimit N               rclone transactions/sec limit; 0 disables (default: 4)
  --start-now                Start service immediately after install
  --no-start-now             Do not start service immediately
  --force                    Overwrite existing installed script and unit file
  -h, --help                 Show this help

Notes:
  - This installer expects systemd.
  - The service runs the script in continuous mode using interval_minutes from config.
EOF
}

log() {
  printf '[install] %s\n' "$*"
}

die() {
  printf '[install] ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-name)
      shift
      SERVICE_NAME="${1:-}"
      ;;
    --install-dir)
      shift
      INSTALL_DIR="${1:-}"
      ;;
    --config-dir)
      shift
      CONFIG_DIR="${1:-}"
      ;;
    --script-source)
      shift
      SOURCE_SCRIPT="${1:-}"
      ;;
    --config-source)
      shift
      CONFIG_SOURCE="${1:-}"
      ;;
    --python-bin)
      shift
      PYTHON_BIN="${1:-}"
      ;;
    --rclone-bin)
      shift
      RCLONE_BIN="${1:-}"
      ;;
    --rclone-config)
      shift
      RCLONE_CONFIG_PATH="${1:-}"
      ;;
    --work-dir)
      shift
      WORK_DIR="${1:-}"
      ;;
    --tpslimit)
      shift
      TPS_LIMIT="${1:-}"
      ;;
    --start-now)
      START_NOW="true"
      ;;
    --no-start-now)
      START_NOW="false"
      ;;
    --force)
      FORCE="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
  shift
done

[[ -n "$SERVICE_NAME" ]] || die "service name cannot be empty"
[[ -n "$INSTALL_DIR" ]] || die "install dir cannot be empty"
[[ -n "$CONFIG_DIR" ]] || die "config dir cannot be empty"
[[ -n "$PYTHON_BIN" ]] || die "python3 was not found; pass --python-bin"
[[ -x "$PYTHON_BIN" ]] || die "python binary is not executable: $PYTHON_BIN"
[[ -f "$SOURCE_SCRIPT" ]] || die "source script not found: $SOURCE_SCRIPT"

if [[ $EUID -ne 0 ]]; then
  die "run this installer as root (use sudo)"
fi

command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this installer requires systemd"

# Detect whether rclone is the snap build; snap confinement restricts which
# filesystem paths rclone can read, so we pick snap-friendly defaults below.
rclone_is_snap="false"
if [[ -n "$RCLONE_BIN" ]] && readlink -f "$RCLONE_BIN" 2>/dev/null | grep -q '/snap/'; then
  rclone_is_snap="true"
fi

# Ask rclone (running as root here) where it will read its config from.
if [[ -z "$RCLONE_CONFIG_PATH" && -n "$RCLONE_BIN" ]]; then
  RCLONE_CONFIG_PATH="$("$RCLONE_BIN" config file 2>/dev/null | awk '/rclone\.conf/ {print $NF}' | tail -n1 || true)"
fi

# Choose a work_dir that rclone can actually read from.
if [[ -z "$WORK_DIR" ]]; then
  if [[ "$rclone_is_snap" == "true" ]]; then
    WORK_DIR="/root/snap/rclone/common/backups"
  else
    WORK_DIR="/var/lib/docker-volume-backup"
  fi
fi

# For snap rclone, root's config must live under root's snap dir. If it is
# missing but the invoking user has one, copy it so the service can authenticate.
if [[ "$rclone_is_snap" == "true" && -n "$RCLONE_CONFIG_PATH" && ! -f "$RCLONE_CONFIG_PATH" && -n "${SUDO_USER:-}" ]]; then
  user_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
  if [[ -n "$user_home" ]]; then
    user_conf="$(ls -1 "$user_home"/snap/rclone/*/.config/rclone/rclone.conf 2>/dev/null | tail -n1 || true)"
    if [[ -n "$user_conf" && -f "$user_conf" ]]; then
      mkdir -p "$(dirname "$RCLONE_CONFIG_PATH")"
      install -m 0600 "$user_conf" "$RCLONE_CONFIG_PATH"
      log "copied rclone config for root -> $RCLONE_CONFIG_PATH (from $user_conf)"
    fi
  fi
fi

if [[ -n "$RCLONE_CONFIG_PATH" ]]; then
  log "service rclone config: $RCLONE_CONFIG_PATH"
else
  log "could not auto-detect rclone config path; set it later with --rclone-config"
fi
log "service work_dir: $WORK_DIR"

INSTALL_SCRIPT="$INSTALL_DIR/backup_docker_volumes.py"
CONFIG_PATH="$CONFIG_DIR/config.json"
UNIT_PATH="$SYSTEMD_DIR/${SERVICE_NAME}.service"

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$WORK_DIR"

if [[ -f "$INSTALL_SCRIPT" && "$FORCE" != "true" ]]; then
  log "installed script already exists: $INSTALL_SCRIPT"
  log "use --force to overwrite"
else
  install -m 0755 "$SOURCE_SCRIPT" "$INSTALL_SCRIPT"
  log "installed script -> $INSTALL_SCRIPT"
fi

if [[ -n "$CONFIG_SOURCE" ]]; then
  [[ -f "$CONFIG_SOURCE" ]] || die "config source not found: $CONFIG_SOURCE"
  install -m 0644 "$CONFIG_SOURCE" "$CONFIG_PATH"
  log "installed config from --config-source -> $CONFIG_PATH"
elif [[ ! -f "$CONFIG_PATH" ]]; then
  if [[ -f "$EXAMPLE_CONFIG" ]]; then
    install -m 0644 "$EXAMPLE_CONFIG" "$CONFIG_PATH"
    log "installed config from example -> $CONFIG_PATH"
  else
    cat > "$CONFIG_PATH" <<EOF
{
  "remote": "",
  "remote_prefix": "docker-backups",
  "docker_volumes": [],
  "extra_paths": [],
  "interval_minutes": 1440,
  "retention_days": 14,
  "work_dir": "$WORK_DIR",
  "compression_level": 6,
  "keep_archives_local": false,
  "rclone_bin": "rclone",
  "docker_bin": "docker"
}
EOF
    chmod 0644 "$CONFIG_PATH"
    log "installed generated config -> $CONFIG_PATH"
  fi
else
  log "config already exists, keeping current file: $CONFIG_PATH"
fi

# Build the Environment= lines for the unit, including rclone wiring.
env_lines="Environment=PYTHONUNBUFFERED=1"
if [[ "$rclone_is_snap" == "true" ]]; then
  env_lines+=$'\n'"Environment=HOME=/root"
fi
if [[ -n "$RCLONE_CONFIG_PATH" ]]; then
  env_lines+=$'\n'"Environment=RCLONE_CONFIG=$RCLONE_CONFIG_PATH"
fi
if [[ -n "$TPS_LIMIT" && "$TPS_LIMIT" != "0" ]]; then
  env_lines+=$'\n'"Environment=RCLONE_TPSLIMIT=$TPS_LIMIT"
  env_lines+=$'\n'"Environment=RCLONE_TPSLIMIT_BURST=$TPS_LIMIT"
fi

if [[ -f "$UNIT_PATH" && "$FORCE" != "true" ]]; then
  log "unit file already exists: $UNIT_PATH"
  log "use --force to overwrite"
else
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Docker Volume Backup Service
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN $INSTALL_SCRIPT --config $CONFIG_PATH
Restart=always
RestartSec=30
User=root
Group=root
$env_lines

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$UNIT_PATH"
  log "installed unit -> $UNIT_PATH"
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
log "enabled service: $SERVICE_NAME"

if [[ "$START_NOW" == "auto" ]]; then
  if grep -q 'my-backup-bucket' "$CONFIG_PATH" || grep -q '"remote"[[:space:]]*:[[:space:]]*""' "$CONFIG_PATH"; then
    START_NOW="false"
    log "detected placeholder/empty remote in config; not starting automatically"
  else
    START_NOW="true"
  fi
fi

if [[ "$START_NOW" == "true" ]]; then
  systemctl restart "$SERVICE_NAME"
  log "started service: $SERVICE_NAME"
else
  log "service is installed but not started"
fi

log "done"
log "edit config: $CONFIG_PATH"
log "service rclone config: ${RCLONE_CONFIG_PATH:-<unset>}"
log "service work_dir: $WORK_DIR"
log "inspect status: systemctl status $SERVICE_NAME"
log "view logs: journalctl -u $SERVICE_NAME -f"
if [[ "$rclone_is_snap" == "true" ]]; then
  log "note: snap rclone in use; if a snap update changes its revision, re-run 'sudo rclone config file' and update RCLONE_CONFIG"
fi
