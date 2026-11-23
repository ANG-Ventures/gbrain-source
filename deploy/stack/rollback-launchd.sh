#!/bin/bash
# rollback-launchd.sh: gbrain-rollback step R4, the launchd re-registration (card t_bb0e9194).
# Never run inline from an agent session: `launchctl bootstrap` is refused inside the gateway. gbrain-rollback queues
# it with ~/.hermes/scripts/detached-run.py, so launchd's detached runner executes it outside any gateway, and then
# waits on its own readback. By hand from a plain terminal it is the same three enable + bootstrap pairs.
# bash 3.2 safe (macOS /bin/bash). Prints one `state =` line per label; exit 0 only when all three are running.
set -u
U=$(id -u)
LABELS="ai.gbrain.rerank-tunnel ai.gbrain.rerank-proxy ai.gbrain.serve"   # tunnel first: the proxy's primary
rc=0
for l in $LABELS; do
  launchctl enable "gui/$U/$l" || rc=1
  if launchctl print "gui/$U/$l" >/dev/null 2>&1; then
    echo "$l: already loaded"
  else
    launchctl bootstrap "gui/$U" "$HOME/Library/LaunchAgents/$l.plist" || { echo "$l: bootstrap rc=$?"; rc=1; }
  fi
done
for i in 1 2 3 4 5 6 7 8 9 10; do
  n=0
  for l in $LABELS; do
    launchctl print "gui/$U/$l" 2>/dev/null | grep -q 'state = running' && n=$((n + 1))
  done
  [ "$n" = 3 ] && break
  sleep 2
done
for l in $LABELS; do
  echo "$l $(launchctl print "gui/$U/$l" 2>/dev/null | grep -m1 'state =' | tr -s ' ' || echo 'not loaded')"
done
[ "$n" = 3 ] || rc=1
exit $rc
