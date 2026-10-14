#!/bin/bash
# vm120_batch.sh LANG... -- full VM 120 capture per language, borrowing the eBUS adapter from production.
# Production's add-on is stopped only for the configure/first_look part and always restarted (trap).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
S="$HERE/tools"
PY=~/venvs/genia-video/bin/python
SLUG=cf9da99f_genia_air

prod() {  # prod start|stop, retried while the Supervisor is busy (e.g. a backup)
  for i in $(seq 1 120); do
    "$S/prod_addon.sh" "$1" >/dev/null 2>&1
    want=$([ "$1" = start ] && echo started || echo stopped)
    [ "$("$S/prod_addon.sh" state)" = "$want" ] && { echo "prod $want $(date +%T)"; return 0; }
    sleep 15
  done
  echo "prod $1 FAILED"; return 1
}
trap 'prod start' EXIT

# helpers the capture runs inside the HA OS VM (idempotent)
for f in setlat.sh setopt.sh scanwait.sh; do
  "$S/gx.sh" 120 "echo $(base64 -w0 "$S/$f") | base64 -d > /mnt/data/$f && chmod +x /mnt/data/$f" 20 >/dev/null
done

for L in "$@"; do
  echo "=== $L $(date +%T)"
  cd "$HERE"
  $PY vm120.py --lang "$L" --out "../raw/$L" --reset --scenes mosquitto,install || { echo "FAIL $L install"; continue; }
  prod stop || exit 1
  $PY vm120.py --lang "$L" --out "../raw/$L" --scenes configure || echo "FAIL $L configure"
  "$S/gx.sh" 120 "/mnt/data/setlat.sh $SLUG 50" 120 | tail -1
  "$S/gx.sh" 120 "/mnt/data/scanwait.sh app_$SLUG" 340
  $PY vm120.py --lang "$L" --out "../raw/$L" --scenes first_look,outro_sim || echo "FAIL $L first_look"
  "$S/gx.sh" 120 "ha apps stop $SLUG" 60 | tail -1
  prod start
done
echo "BATCH DONE $(date +%T)"
