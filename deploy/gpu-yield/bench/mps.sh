#!/bin/bash
# t_0930e064: private MPS control daemon for GPU0 (pipe dir under ~/t_0930e064, so only processes that set
# CUDA_MPS_PIPE_DIRECTORY to it become clients; the live services are untouched).
# usage: mps.sh start | stop | ps
set -u
M=/home/ace/t_0930e064/mps
export CUDA_MPS_PIPE_DIRECTORY=$M/pipe CUDA_MPS_LOG_DIRECTORY=$M/log
export CUDA_VISIBLE_DEVICES=GPU-31cbe110-66f1-da0c-dd4b-5cc60bcde854
case "$1" in
start) mkdir -p $M/pipe $M/log; nvidia-cuda-mps-control -d && echo "mps control started"; sleep 1; pgrep -u ace -a nvidia-cuda-mps ;;
stop) echo quit | nvidia-cuda-mps-control; sleep 2; pgrep -u ace -a nvidia-cuda-mps || echo "mps gone" ;;
ps) echo ps | nvidia-cuda-mps-control; echo get_default_client_priority | nvidia-cuda-mps-control; tail -5 $M/log/control.log; tail -5 $M/log/server.log 2>/dev/null ;;
esac
