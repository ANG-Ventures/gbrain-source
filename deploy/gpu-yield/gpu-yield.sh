#!/usr/bin/env bash
# GPU0 priority for the zerank reranker on ACE-AI (t_0930e064): ComfyUI pauses between layer ops while a
# rerank is in flight. ONE switch: `enable` / `disable`. Run as ace on ACE-AI from a checkout of this repo.
#
#   deploy/gpu-yield/gpu-yield.sh status
#   deploy/gpu-yield/gpu-yield.sh enable  [--force]   # installs files + drop-ins, restarts both units
#   deploy/gpu-yield/gpu-yield.sh disable [--force]   # removes drop-ins, restarts both units (rollback)
#
# Restarting comfyui.service kills a running ComfyUI job, so enable/disable refuse while its queue is
# non-empty unless --force. Files it installs (harmless while disabled):
#   ~/ComfyUI/custom_nodes/comfyui_gpu_yield/   (logs "gpu_yield: off" when GPU_YIELD_DIR is unset)
#   ~/zerank-serve/zerank_yield.py              (ASGI wrapper; only used when the drop-in points ExecStart at it)
# Drop-ins it writes on enable / removes on disable:
#   ~/.config/systemd/user/comfyui.service.d/60-gpu-yield.conf
#   ~/.config/systemd/user/zerank-native.service.d/60-gpu-yield.conf
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UNITDIR="$HOME/.config/systemd/user"
COMFY_DROP="$UNITDIR/comfyui.service.d/60-gpu-yield.conf"
ZR_DROP="$UNITDIR/zerank-native.service.d/60-gpu-yield.conf"
NODE_DST="$HOME/ComfyUI/custom_nodes/comfyui_gpu_yield"
ZR_DST="$HOME/zerank-serve/zerank_yield.py"
cmd="${1:-status}"; force="${2:-}"

queue_empty() {
  curl -s -m5 http://127.0.0.1:8188/queue | python3 -c \
    'import json,sys;q=json.load(sys.stdin);n=len(q["queue_running"])+len(q["queue_pending"]);print(n);sys.exit(0 if n==0 else 1)'
}

guard() {
  if [ "$force" != "--force" ] && ! n=$(queue_empty); then
    echo "refusing: live ComfyUI queue has ${n:-?} job(s); a restart would kill the running one. Re-run with --force." >&2
    exit 2
  fi
}

install_files() {
  mkdir -p "$NODE_DST"
  install -m 0644 "$HERE/comfyui_gpu_yield/__init__.py" "$HERE/comfyui_gpu_yield/gate.py" "$NODE_DST/"
  install -m 0644 "$REPO/deploy/zerank/zerank_yield.py" "$ZR_DST"
}

restart_both() {
  systemctl --user daemon-reload
  systemctl --user restart zerank-native.service
  for _ in $(seq 1 90); do curl -sf -m2 http://127.0.0.1:8084/health >/dev/null && break; sleep 1; done
  curl -sf -m2 http://127.0.0.1:8084/health >/dev/null || { echo "zerank-native did not come back" >&2; exit 1; }
  # a job may have been queued while zerank restarted; check again right before the ComfyUI restart.
  # Refusing here leaves only one side switched, which is inert (flags with no reader, or a gate with no
  # writer); re-run the same command when the queue is empty.
  guard
  systemctl --user restart comfyui.service
  for _ in $(seq 1 120); do curl -sf -m2 http://127.0.0.1:8188/system_stats >/dev/null && break; sleep 1; done
  curl -sf -m2 http://127.0.0.1:8188/system_stats >/dev/null || { echo "comfyui did not come back" >&2; exit 1; }
}

case "$cmd" in
enable)
  guard
  install_files
  mkdir -p "$(dirname "$COMFY_DROP")" "$(dirname "$ZR_DROP")"
  printf '%s\n' '# t_0930e064: pause sampling while a zerank rerank is in flight. Remove (gpu-yield.sh disable) to roll back.' \
    '[Service]' 'Environment=GPU_YIELD_DIR=%t/gpu-yield' >"$COMFY_DROP"
  printf '%s\n' '# t_0930e064: hold a flag in %t/gpu-yield for each in-flight /v1/rerank. Remove (gpu-yield.sh disable) to roll back.' \
    '[Service]' 'Environment=GPU_YIELD_DIR=%t/gpu-yield' 'ExecStart=' \
    'ExecStart=/home/ace/zerank-serve/venv/bin/uvicorn zerank_yield:app --host 127.0.0.1 --port 8084 --app-dir /home/ace/zerank-serve' \
    >"$ZR_DROP"
  restart_both
  "$0" status ;;
disable)
  guard
  rm -f "$COMFY_DROP" "$ZR_DROP"
  restart_both
  "$0" status ;;
status)
  for f in "$COMFY_DROP" "$ZR_DROP"; do [ -f "$f" ] && echo "drop-in present: $f" || echo "drop-in absent:  $f"; done
  echo "zerank ExecStart: $(systemctl --user show -p ExecStart --value zerank-native.service | grep -o 'zerank_[a-z]*:app' | head -1)"
  echo "comfyui GPU_YIELD_DIR: $(systemctl --user show -p Environment --value comfyui.service | tr ' ' '\n' | grep '^GPU_YIELD_DIR=' || echo unset)"
  echo "last gate line in ~/comfyui.log: $(grep 'gpu_yield:' "$HOME/comfyui.log" 2>/dev/null | tail -1 || true)"
  echo "flags now: $(ls "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/gpu-yield" 2>/dev/null | wc -l)" ;;
*) echo "usage: $0 enable|disable|status [--force]" >&2; exit 64 ;;
esac
