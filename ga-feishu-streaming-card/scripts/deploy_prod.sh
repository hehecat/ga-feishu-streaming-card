#!/usr/bin/env bash
# Build and deploy ga-feishu-streaming-card into a running GenericAgent checkout.
# Usage: scripts/deploy_prod.sh [GA_ROOT]
set -Eeuo pipefail

DELIVERY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GA_ROOT="${1:-${HFC_GA_ROOT:-}}"
if [[ -z "$GA_ROOT" && -f "$DELIVERY_ROOT/../../frontends/fsapp.py" ]]; then
  GA_ROOT="$(cd "$DELIVERY_ROOT/../.." && pwd)"
fi
[[ -n "$GA_ROOT" ]] || { echo "ERROR: pass GA_ROOT or set HFC_GA_ROOT" >&2; exit 2; }
GA_ROOT="$(cd "$GA_ROOT" && pwd)"
PYTHON="$GA_ROOT/.venv/bin/python"
LOG="$GA_ROOT/temp/fsapp_prod_20260809.log"
PID_FILE="$GA_ROOT/temp/fsapp_prod_20260809.pid"
REPORT="$GA_ROOT/temp/hfc_deploy_report_$(date +%Y%m%d_%H%M%S).txt"
PORT="${HFC_PROD_PORT:-8898}"
START_EPOCH="$(date +%s)"
OLD_PID=""
NEW_PID=""
OLD_VERSION=""
OLD_WHEEL=""
DIST_BACKUP=""
INSTALL_STARTED=0

exec > >(tee -a "$REPORT") 2>&1
step() { printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail() { echo "DEPLOY_RESULT=FAIL ($*)"; exit 1; }
listener_pid() {
  ss -H -ltnp "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1
}
wait_until() {
  local timeout="$1"; shift
  local end=$((SECONDS + timeout))
  until "$@"; do (( SECONDS >= end )) && return 1; sleep 1; done
}
port_is_ours() {
  local p; p="$(listener_pid)"; [[ -n "$p" ]] || return 1
  [[ "$(readlink "/proc/$p/cwd" 2>/dev/null || true)" == "$GA_ROOT" ]] || return 1
  tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | grep -Fq 'frontends/fsapp.py'
}
stop_our_listener() {
  local p
  p="$(listener_pid)"
  [[ -n "$p" ]] || return 0
  port_is_ours || { echo "ROLLBACK_ERROR: port $PORT is occupied by a process outside GA_ROOT"; return 1; }
  kill -TERM "$p" 2>/dev/null || true
  wait_until 20 bash -c '! kill -0 "$1" 2>/dev/null' _ "$p" || {
    echo "ROLLBACK_ERROR: fsapp PID $p did not stop within 20s"
    return 1
  }
}
start_fsapp() {
  cd "$GA_ROOT"
  setsid "$PYTHON" -u frontends/fsapp.py >>"$LOG" 2>&1 </dev/null &
  NEW_PID=$!
  echo "$NEW_PID" >"$PID_FILE"
  wait_until 30 port_is_ours || return 1
  local real_pid; real_pid="$(listener_pid)"
  [[ "$real_pid" == "$NEW_PID" ]] || {
    echo "ROLLBACK_ERROR: listener PID $real_pid differs from started PID $NEW_PID"
    return 1
  }
}
rollback_installed_version() {
  step "rollback installed package"
  echo "rollback_target_version=$OLD_VERSION"
  echo "rollback_wheel=$OLD_WHEEL"
  if [[ ! -f "$OLD_WHEEL" ]]; then
    echo "ROLLBACK_ERROR: old wheel is missing: $OLD_WHEEL"
    return 1
  fi
  uv pip install --python "$PYTHON" --force-reinstall "$OLD_WHEEL" || {
    echo "ROLLBACK_ERROR: reinstalling old wheel failed; retry manually: uv pip install --python '$PYTHON' --force-reinstall '$OLD_WHEEL'"
    return 1
  }
  local restored_version
  restored_version="$(uv pip show --python "$PYTHON" ga-feishu-streaming-card 2>/dev/null | sed -n 's/^Version: //p' | head -1)"
  [[ "$restored_version" == "$OLD_VERSION" ]] || {
    echo "ROLLBACK_ERROR: restored version '$restored_version' differs from expected '$OLD_VERSION'"
    return 1
  }
  stop_our_listener || return 1
  start_fsapp || {
    echo "ROLLBACK_ERROR: restored package installed but fsapp did not become ready on port $PORT; inspect $LOG"
    return 1
  }
  echo "RECOVERY=ROLLED_BACK version=$restored_version pid=$(listener_pid)"
}
cleanup_on_error() {
  local rc=$?
  trap - EXIT
  if (( rc != 0 )); then
    echo "DEPLOY_RESULT=FAIL (exit=$rc)"
    if (( INSTALL_STARTED )); then
      if rollback_installed_version; then
        :
      else
        echo "RECOVERY=ROLLBACK_FAILED; old_wheel=$OLD_WHEEL report=$REPORT log=$LOG"
        echo "ROLLBACK_ACTION: restore manually with: uv pip install --python '$PYTHON' --force-reinstall '$OLD_WHEEL'; then restart frontends/fsapp.py"
      fi
    elif ! port_is_ours; then
      if [[ -n "$NEW_PID" ]] && kill -0 "$NEW_PID" 2>/dev/null; then
        kill -TERM "$NEW_PID" 2>/dev/null || true
        wait_until 10 bash -c '! kill -0 "$1" 2>/dev/null' _ "$NEW_PID" || true
      fi
      echo "RECOVERY: fsapp listener absent; attempting a clean restart"
      if start_fsapp; then
        echo "RECOVERY=PASS pid=$(listener_pid)"
      else
        echo "RECOVERY=FAIL; manual intervention required; inspect $LOG"
      fi
    else
      echo "RECOVERY=NOT_NEEDED listener_pid=$(listener_pid)"
    fi
    echo "REPORT=$REPORT"
  fi
}
trap cleanup_on_error EXIT

step "preflight"
command -v uv >/dev/null || fail "uv not found"
command -v ss >/dev/null || fail "ss not found"
[[ -x "$PYTHON" ]] || fail "GA Python missing: $PYTHON"
[[ -f "$GA_ROOT/frontends/fsapp.py" ]] || fail "fsapp missing under GA_ROOT"
mkdir -p "$GA_ROOT/temp"
OLD_PID="$(listener_pid)"
if [[ -n "$OLD_PID" ]]; then
  port_is_ours || fail "port $PORT is occupied by a process outside GA_ROOT"
  echo "old_pid=$OLD_PID"
else
  echo "old_pid=none"
fi
echo "delivery_head=$(git -C "$DELIVERY_ROOT" rev-parse --short HEAD)"
echo "ga_head=$(git -C "$GA_ROOT" rev-parse --short HEAD)"

step "preserve installed version and previous dist"
OLD_VERSION="$(uv pip show --python "$PYTHON" ga-feishu-streaming-card 2>/dev/null | sed -n 's/^Version: //p' | head -1)"
[[ -n "$OLD_VERSION" ]] || fail "ga-feishu-streaming-card is not installed; refusing a deployment without a rollback target"
echo "installed_version_before=$OLD_VERSION"
cd "$DELIVERY_ROOT"
if [[ -d dist ]]; then
  DIST_BACKUP="$DELIVERY_ROOT/dist.bak.$(date +%Y%m%d_%H%M%S).$$"
  mv "$DELIVERY_ROOT/dist" "$DIST_BACKUP"
  echo "dist_backup=$DIST_BACKUP"
else
  fail "dist directory is absent; build/preserve the currently installed version $OLD_VERSION before deploying"
fi
OLD_WHEEL="$($PYTHON - "$DIST_BACKUP" "$OLD_VERSION" <<'PY'
import email, pathlib, sys, zipfile
root, expected = pathlib.Path(sys.argv[1]), sys.argv[2]
for wheel in sorted(root.glob("*.whl")):
    try:
        with zipfile.ZipFile(wheel) as zf:
            metadata_name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
            metadata = email.message_from_bytes(zf.read(metadata_name))
        if metadata.get("Name", "").lower().replace("_", "-") == "ga-feishu-streaming-card" and metadata.get("Version") == expected:
            print(wheel)
            break
    except (OSError, StopIteration, zipfile.BadZipFile):
        pass
PY
)"
[[ -n "$OLD_WHEEL" && -f "$OLD_WHEEL" ]] || fail "dist backup has no wheel matching installed version $OLD_VERSION; no package was changed"
echo "rollback_wheel=$OLD_WHEEL"

step "uv build"
uv build
WHEEL="$(find "$DELIVERY_ROOT/dist" -maxdepth 1 -type f -name '*.whl' | sort | tail -1)"
[[ -f "$WHEEL" ]] || fail "wheel not produced"
echo "wheel=$WHEEL"

step "install wheel into GA virtual environment"
INSTALL_STARTED=1
uv pip install --python "$PYTHON" --force-reinstall "$WHEEL"
"$PYTHON" - <<'PY'
from ga_feishu_streaming_card.command import render_command_result_card
assert callable(render_command_result_card)
print("import_smoke=PASS")
PY

step "restart fsapp precisely"
if [[ -n "$OLD_PID" ]]; then
  kill -TERM "$OLD_PID"
  wait_until 20 bash -c '! kill -0 "$1" 2>/dev/null' _ "$OLD_PID" || fail "old PID $OLD_PID did not stop"
fi
LOG_MARK="$(wc -c <"$LOG" 2>/dev/null || echo 0)"
cd "$GA_ROOT"
setsid "$PYTHON" -u frontends/fsapp.py >>"$LOG" 2>&1 </dev/null &
NEW_PID=$!
echo "$NEW_PID" >"$PID_FILE"
echo "new_pid=$NEW_PID"
wait_until 30 port_is_ours || fail "port $PORT did not become ready"
REAL_PID="$(listener_pid)"
[[ "$REAL_PID" == "$NEW_PID" ]] || fail "listener PID $REAL_PID differs from started PID $NEW_PID"

step "smoke: listener + fresh WebSocket connection"
echo "listener=PASS pid=$REAL_PID port=$PORT"
ws_connected() { tail -c +$((LOG_MARK + 1)) "$LOG" 2>/dev/null | grep -Eq 'connected to wss://msg-frontier\.feishu\.cn|飞书 Agent 已启动（长连接模式）'; }
wait_until 45 ws_connected || fail "fresh WebSocket connection not observed in log"
echo "websocket=PASS"

step "smoke: plaintext /llms renders a model-selector select_static dropdown"
"$PYTHON" - <<'PY'
from ga_feishu_streaming_card.command import render_command_result_card
plaintext = "LLMs:\n→ [0] deepseek-v4-flash\n  [1] gpt-5.6-luna"
card = render_command_result_card(plaintext, "/llms")
selects = [e for e in card.get("body", {}).get("elements", [])
           if e.get("tag") == "select_static"]
assert selects, card
options = selects[0].get("options", [])
values = [o.get("value") for o in options]
assert "0" in values, values  # T27-G6: option.value=渠道索引(/llms序号)
assert "1" in values, values
texts = [o.get("text", {}).get("content") for o in options]
assert "gpt-5.6-luna" in texts, texts  # 展示名仍是模型名，索引协议只在 value 层
assert "deepseek-v4-flash" in texts, texts
print("llms_plaintext=", repr(plaintext))
print("fixture_current_model=deepseek-v4-flash")
print("model_selector_select_static=PASS values=", values)
PY

step "post-deploy health"
kill -0 "$REAL_PID"
port_is_ours
echo "duration_seconds=$(($(date +%s)-START_EPOCH))"
echo "DEPLOY_RESULT=PASS"
echo "REPORT=$REPORT"
trap - EXIT
