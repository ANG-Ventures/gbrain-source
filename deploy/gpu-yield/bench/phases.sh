#!/bin/bash
# t_0930e064 measurement phases (run detached: ./run.sh phases ./phases.sh). Each phase waits until no
# sibling-card (t_bdc77bdc) bench process is running on GPU0, and logs that wait.
set -u
cd /home/ace/t_0930e064
Y=/run/user/1000/t0930-yield
P=/home/ace/t_0930e064/mps/pipe
VZ=/home/ace/zerank-serve/venv/bin/python
VC=/home/ace/ComfyUI/.venv/bin/python
log() { echo "$(date +%T) $*"; }
waitclear() {
  # sibling's GPU work = its bench_inproc.py / replay.py processes (its gate.sh waits while t0930-job-* runs,
  # so checking its unit state would deadlock us both)
  while pgrep -u ace -f '^[^ ]*python[^ ]* (bench_inproc|replay)[.]py' >/dev/null; do
    log "waiting: sibling bench running"; sleep 20; done
  log "GPU0: $(nvidia-smi -i 0 --query-gpu=utilization.gpu,memory.used --format=csv,noheader); live comfy queue: $(curl -s -m3 127.0.0.1:8188/queue | tr -d '\n' | head -c 80)"
}
loaded() {  # <label> : start comfy load in background until stop file
  rm -f stop-$1; $VC comfy_load.py 18188 $1 comfy.jsonl /home/ace/t_0930e064/stop-$1 >logs/load-$1.log 2>&1 &
  LOADPID=$!; sleep 20; }
unload() { touch stop-$1; wait $LOADPID; log "load $1 done: $(cat logs/load-$1.log | tr '\n' '|')"; }

# comfy is up in MPS mode (CUDA_MPS_CLIENT_PRIORITY=1), no gate
waitclear; log "A idle start"; $VZ bench_pair.py A-idle pair.jsonl 18094:plain 18096:mps; log "A idle end"
waitclear; log "C mps-prio1 start"; loaded C; $VZ bench_pair.py C-busy pair.jsonl 18096:mps-prio1; unload C; log "C end"

./inst.sh down comfy; sleep 3
./inst.sh up-comfy "" CUDA_MPS_PIPE_DIRECTORY=$P CUDA_MPS_CLIENT_PRIORITY=1 CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=50
waitclear; log "D mps-prio1-atp50 start"; loaded D; $VZ bench_pair.py D-busy pair.jsonl 18096:mps-atp50; unload D; log "D end"

./inst.sh down comfy; sleep 3
./inst.sh up-comfy $Y
waitclear; log "E comfy alone start"; $VC comfy_load.py 18188 E comfy.jsonl /nonexistent 3; log "E end"
waitclear; log "B gate start"; loaded B; $VZ bench_pair.py B-busy pair.jsonl 18094:nogate 18095:gate; unload B; log "B end"
journalctl --user -u t0930-comfy --no-pager | grep -E "gpu_yield: (paused|pause hit)" | tail -n 400 > logs/gate-pauses.log
log ALL_DONE
