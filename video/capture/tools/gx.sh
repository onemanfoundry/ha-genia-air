#!/bin/bash
# gx.sh VMID 'command'  -- run a shell command inside an HA OS VM via qemu guest agent (on zeratul)
ssh zeratul "pid=\$(qm guest exec $1 --synchronous 0 -- sh -c '$2' | grep -o '[0-9]\+'); for i in \$(seq 1 ${3:-60}); do r=\$(qm guest exec-status $1 \$pid); echo \"\$r\" | grep -q '\"exited\" : 1' && { echo \"\$r\"; break; }; sleep 2; done" | python3 -I -c "
import json,sys
t=sys.stdin.read()
try:
  d=json.loads(t); print(d.get('out-data',''),end=''); print(d.get('err-data',''),end='',file=sys.stderr)
except Exception: print(t)"
