#!/bin/bash
# t_0930e064 test instances on ace-ai GPU0 (transient user units; never touches comfyui.service / zerank-native.service)
# usage: inst.sh up-zerank <port> [GPU_YIELD_DIR] [k=v...] | up-comfy [GPU_YIELD_DIR] [k=v...]
#        down <unit-suffix> | status
set -u
T=/home/ace/t_0930e064
GPU0=GPU-31cbe110-66f1-da0c-dd4b-5cc60bcde854
cmd=$1; shift || true
case "$cmd" in
up-zerank)
  port=$1; shift; ydir=${1:-}; shift || true
  extra=(); for kv in "$@"; do extra+=(-E "$kv"); done
  systemd-run --user --unit=t0930-zerank-$port --collect -p RuntimeMaxSec=7200 -p WorkingDirectory=$T/srv \
    -E CUDA_VISIBLE_DEVICES=$GPU0 -E ZERANK_MODEL=zeroentropy/zerank-2-reranker \
    -E PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -E HF_HUB_OFFLINE=1 -E GPU_YIELD_DIR="$ydir" ${extra[@]+"${extra[@]}"} \
    /home/ace/zerank-serve/venv/bin/uvicorn zerank_yield:app --host 127.0.0.1 --port $port --app-dir $T/srv
  for i in $(seq 1 90); do curl -sf -m2 127.0.0.1:$port/health >/dev/null && { echo zerank $port up after ${i}s; exit 0; }; sleep 1; done
  echo zerank NOT up; journalctl --user -u t0930-zerank-$port -n 20 --no-pager; exit 1 ;;
up-comfy)
  ydir=${1:-}; shift || true
  extra=(); for kv in "$@"; do extra+=(-E "$kv"); done
  systemd-run --user --unit=t0930-comfy --collect -p RuntimeMaxSec=7200 -p WorkingDirectory=/home/ace/ComfyUI \
    -E GPU_YIELD_DIR="$ydir" ${extra[@]+"${extra[@]}"} \
    /home/ace/ComfyUI/.venv/bin/python main.py --listen 127.0.0.1 --port 18188 --cuda-device 0 \
    --base-directory $T/comfy --disable-auto-launch
  for i in $(seq 1 120); do curl -sf -m2 127.0.0.1:18188/system_stats >/dev/null && { echo comfy up after ${i}s; journalctl --user -u t0930-comfy --no-pager | grep -i gpu_yield | tail -2; exit 0; }; sleep 1; done
  echo comfy NOT up; journalctl --user -u t0930-comfy -n 30 --no-pager; exit 1 ;;
down) systemctl --user stop "t0930-$1"; echo "stopped t0930-$1" ;;
status)
  systemctl --user list-units --all 't0930-*' 't_bdc77bdc*' --no-pager --no-legend | cut -c1-90
  nvidia-smi -i $GPU0 --query-gpu=utilization.gpu,memory.used --format=csv,noheader
  curl -s -m3 127.0.0.1:8188/queue | python3 -c 'import json,sys;q=json.load(sys.stdin);print("live comfy running",len(q["queue_running"]),"pending",len(q["queue_pending"]))' ;;
esac
