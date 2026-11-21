#!/bin/bash
# jobs/deploy-parity.sh: the in-stack half of the gbrain deploy-parity monitor (card t_ab2fcba3).
# The launchd-era hermes-home scripts/gbrain-deploy-parity.sh (cron row 7d0b9686f64e) checked a LIVE CHECKOUT
# (~/gbrain/app == gh/master, clean tree, serve started after HEAD, /health). In the stack the code is the image:
#   (1) this cron image's GBRAIN_REVISION == GBRAIN_EXPECTED_REVISION (the host profile's pinned build)
#   (2) the server answers /health with engine=postgres over the compose network
# The checks that need the docker socket or GitHub credentials (every running stack container carries the pinned
# revision label; the pin == ANG-Ventures/gbrain-source master; no source bind mounts) run host-side in
# deploy/stack/bin/gbrain-stack-watch, which also pages on this job's failed result line.
# Exit 0 green, 1 with the findings on stdout.
set -u
OUT=""
add() { OUT+="  $1"$'\n'; }
EXP="${GBRAIN_EXPECTED_REVISION:-}"
if [ -z "$EXP" ]; then
  add "GBRAIN_EXPECTED_REVISION unset (render the host profile)"
elif [ "${GBRAIN_REVISION:-unknown}" != "$EXP" ]; then
  add "cron image revision ${GBRAIN_REVISION:-unknown} != pinned $EXP (gbrainctl build, then up)"
fi
H=$(curl -s -m 8 "${GBRAIN_SERVE_URL:-http://server:8199}/health" 2>/dev/null)
echo "$H" | grep -q '"engine":"postgres"' || add "/health not ok: ${H:-no answer}"
if [ -z "$OUT" ]; then
  echo 'EFFECT {"checks_ok": 2}'
  exit 0
fi
printf 'gbrain deploy parity (%s):\n%s' "$(date '+%Y-%m-%d %H:%M %Z')" "$OUT"
exit 1
