# GPU0 priority: ComfyUI pauses while the zerank reranker runs (t_0930e064)

ACE-AI's GPU0 (RTX PRO 6000, 96 GB) is shared by ComfyUI and the zerank reranker. With a ComfyUI job
running, a rerank takes 2-3x longer and gbrain search hits its 12 s cap. Ace's ruling: the reranker has
priority and ComfyUI yields (pause and resume, never a cancelled job).

## How it works

* `deploy/zerank/zerank_yield.py` wraps `zerank_server:app`. While a `POST /v1/rerank` is in flight
  (including the wait behind the server's lock) it keeps one file `rerank-<pid>-<n>` in `$GPU_YIELD_DIR`.
* `comfyui_gpu_yield/` is a ComfyUI custom node (it registers no nodes). It wraps `comfy.ops.run_every_op`,
  which ComfyUI calls at the start of every layer op. A poller thread scans `$GPU_YIELD_DIR` every 10 ms;
  while a fresh flag is present the sampling thread sleeps before its next op, then carries on. Same
  ops, same order: the output is bit-identical.
* Off switch: `GPU_YIELD_DIR` unset in both units (the default). The node then logs `gpu_yield: off` and
  patches nothing; the wrapper is not in ExecStart.

## Bounds (a stuck flag cannot freeze ComfyUI)

| knob (env on comfyui.service) | default | meaning |
|---|---|---|
| `GPU_YIELD_MAX_PAUSE_S` | 30 | one pause never lasts longer; on hitting it the node logs `WARNING gpu_yield: pause hit the 30 s cap` |
| `GPU_YIELD_RESUME_S` | 2 | after a capped pause, run this long before pausing again |
| `GPU_YIELD_STALE_S` | 60 | a flag file older than this is ignored (`WARNING gpu_yield: ignoring stale flag ...`, once per file) |
| `GPU_YIELD_POLL_S` | 0.01 | flag-dir scan interval |

So a reranker that dies mid-request pauses ComfyUI for at most 30 s, then at most 30 s again after 2 s of
running, and not at all once the file is 60 s old. A burst of back-to-back reranks longer than 30 s gives
ComfyUI 2 s of every 32 s. Every pause logs one INFO line (`gpu_yield: paused 4.21 s for a rerank`).

## Enable / rollback (as ace on ACE-AI, from a checkout of this repo)

    deploy/gpu-yield/gpu-yield.sh status
    deploy/gpu-yield/gpu-yield.sh enable     # copies the node + wrapper, writes two drop-ins, restarts both units
    deploy/gpu-yield/gpu-yield.sh disable    # rollback: removes the drop-ins, restarts both units

Both refuse while the live ComfyUI queue is not empty (a restart kills the running job); `--force` overrides.
The drop-ins are `~/.config/systemd/user/{comfyui,zerank-native}.service.d/60-gpu-yield.conf`; the
files they point at stay installed after `disable` and do nothing.

## Tests

    python3 deploy/gpu-yield/tests/test_gate.py -v

`test_stuck_flag_times_out_and_logs` is the D-4 timeout test.
