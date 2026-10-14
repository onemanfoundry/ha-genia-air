#!/bin/sh
# scanwait.sh CONTAINER -- wait until ebusd's scan result lists HMU (08), CTLS2 (15) and 76; re-scan 08 when missing. Max ~10 min
cat > /tmp/sq.py <<'PY'
import socket, sys
s = socket.create_connection(("127.0.0.1", 8888), 5); s.sendall((sys.argv[1] + "\n").encode()); s.settimeout(20); o = b""
try:
    while not o.endswith(b"\n\n"):
        d = s.recv(4096)
        if not d: break
        o += d
except Exception: pass
print(o.decode(errors="replace").strip().replace("\n", " | "))
PY
docker cp /tmp/sq.py "$1":/tmp/sq.py
for i in $(seq 1 20); do
  r=$(docker exec "$1" python3 /tmp/sq.py "scan result" 2>/dev/null)
  case "$r" in *"08;"*"15;"*"76;"*) echo "scan ok after $((i*30))s: $r"; exit 0;; esac
  for a in 08 15 76; do case "$r" in *"$a;"*) ;; *) docker exec "$1" python3 /tmp/sq.py "scan $a" >/dev/null 2>&1;; esac; done
  sleep 30
done
echo "scan incomplete: $r"; exit 1
