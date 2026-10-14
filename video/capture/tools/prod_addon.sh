#!/bin/bash
# prod_addon.sh start|stop|state  -- production Genia Air add-on on VM 100
case "$1" in
  state) ssh zeratul "ssh root@192.168.1.131 'ha apps info 33c0b081_genia_air --raw-json'" | python3 -I -c "import sys,json;print(json.load(sys.stdin)['data']['state'])" ;;
  *) ssh zeratul "ssh root@192.168.1.131 'ha apps $1 33c0b081_genia_air'" ;;
esac
