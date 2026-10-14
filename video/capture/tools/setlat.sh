#!/bin/sh
# setlat.sh SLUG LATENCY -- set ebusd_latency_ms on an installed app and restart it
OPTS=$(ha apps info "$1" --raw-json | sed -n 's/.*"options":\({[^}]*}\).*/\1/p' | sed "s/\"ebusd_latency_ms\":[0-9]*/\"ebusd_latency_ms\":$2/")
echo "{\"options\":$OPTS}" > /mnt/data/opts.json
docker cp /mnt/data/opts.json hassio_cli:/tmp/opts.json
docker exec hassio_cli sh -c 'curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" -d @/tmp/opts.json http://supervisor/addons/'"$1"'/options'
echo; ha apps restart "$1"
