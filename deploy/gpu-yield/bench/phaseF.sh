#!/bin/bash
# t_0930e064 phase F: gate-only (every rerank flagged, 5 s gap between reranks) with the ComfyUI load running,
# for the ComfyUI cost at a known rerank duty cycle.
set -u
cd /home/ace/t_0930e064
VZ=/home/ace/zerank-serve/venv/bin/python
VC=/home/ace/ComfyUI/.venv/bin/python
log() { echo "$(date +%T) $*"; }
while pgrep -u ace -f '^[^ ]*python[^ ]* (bench_inproc|replay)[.]py' >/dev/null; do log "waiting: sibling bench running"; sleep 20; done
log "GPU0: $(nvidia-smi -i 0 --query-gpu=utilization.gpu,memory.used --format=csv,noheader); live comfy queue: $(curl -s -m3 127.0.0.1:8188/queue | tr -d '\n' | head -c 80)"
s0=$(journalctl --user -u t0930-comfy --no-pager | grep -c 'gpu_yield: paused')
log "F start"; rm -f stop-F
$VC comfy_load.py 18188 F comfy.jsonl /home/ace/t_0930e064/stop-F >logs/load-F.log 2>&1 & L=$!
sleep 15
GAP_S=5 $VZ bench_pair.py F-busy pair.jsonl 18095:gate-only
touch stop-F; wait $L
log "load F: $(tr '\n' '|' < logs/load-F.log)"
journalctl --user -u t0930-comfy --no-pager | grep -E 'gpu_yield: (paused|pause hit)' | tail -n +$((s0+1)) > logs/gate-pauses-F.log
log "F pauses: $(wc -l < logs/gate-pauses-F.log), capped: $(grep -c 'cap' logs/gate-pauses-F.log)"
log F_DONE
