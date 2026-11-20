#!/bin/bash
# t_0930e064: run a command detached on ace-ai as a transient user unit; log to ~/t_0930e064/logs/<name>.log
# usage: run.sh <name> <cmd...>
set -u
name=$1; shift
mkdir -p /home/ace/t_0930e064/logs
systemd-run --user --unit="t0930-job-$name" --collect -p RuntimeMaxSec=5400 -p WorkingDirectory=/home/ace/t_0930e064 \
  /bin/bash -c 'exec "$@" >"/home/ace/t_0930e064/logs/'"$name"'.log" 2>&1' _ "$@"
