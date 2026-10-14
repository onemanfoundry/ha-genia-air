#!/bin/bash
# build_all.sh LANG... -- build each language in turn (low priority), log to ../build/build_<lang>.log
cd "$(dirname "$0")"
for L in "$@"; do
  echo "=== $L $(date +%T)"
  nice -n 19 ~/venvs/genia-video/bin/python build.py "$L" > "../build/build_$L.log" 2>&1 && tail -1 "../build/build_$L.log" || { echo "FAIL $L"; tail -5 "../build/build_$L.log"; }
done
echo "BUILDS DONE $(date +%T)"
