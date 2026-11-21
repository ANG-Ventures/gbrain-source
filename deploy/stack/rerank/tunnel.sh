#!/bin/sh
# rerank-tunnel (card t_ab2fcba3): the container twin of hermes-home scripts/gbrain-rerank-tunnel.sh.
# Forwards 0.0.0.0:18084 in this container to 127.0.0.1:8084 (zerank-native) on the reranker host, as
# $RERANK_TUNNEL_TARGET (user@host), with the rendered key + pinned known_hosts. Same pacing as the launchd wrapper:
# ServerAlive kills a dead forward, re-dial with backoff 5,10,20..300 s, a connection that lived >= 120 s resets it.
set -u
: "${RERANK_TUNNEL_TARGET:?RERANK_TUNNEL_TARGET unset: run gbrainctl render}"
KEY="${RERANK_TUNNEL_KEY:-/run/secrets/tunnel-ssh-key}"
KH="${RERANK_TUNNEL_KNOWN_HOSTS:-/run/secrets/tunnel-known-hosts}"
# ssh refuses a private key readable by others; the bind-mounted secret keeps the host file's mode.
install -m 0600 "$KEY" /tmp/tunnel-key
DELAY=5
while true; do
  START=$(date +%s)
  ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o BatchMode=yes \
    -o ConnectTimeout=10 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KH" \
    -o GatewayPorts=yes -i /tmp/tunnel-key -L "0.0.0.0:18084:127.0.0.1:${RERANK_REMOTE_PORT:-8084}" "$RERANK_TUNNEL_TARGET"
  rc=$?
  LIVED=$(( $(date +%s) - START ))
  [ "$LIVED" -ge 120 ] && DELAY=5
  echo "$(date '+%F %T') tunnel exited rc=$rc lived=${LIVED}s; retry in ${DELAY}s"
  sleep "$DELAY"
  DELAY=$(( DELAY * 2 )); [ "$DELAY" -gt 300 ] && DELAY=300
done
