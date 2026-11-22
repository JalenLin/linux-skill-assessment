#!/bin/bash
# PVE hookscript：作答者實例啟動後，把它在 vmbr1 上的 port 設為 isolated，
# isolated port 之間無法互通，只能和非 isolated 的 port（lsa-web）通訊。
vmid="$1"; phase="$2"
[ "$phase" = "post-start" ] || exit 0
for _ in $(seq 1 40); do
  found=0
  for dev in /sys/class/net/veth${vmid}i* /sys/class/net/tap${vmid}i*; do
    [ -e "$dev" ] || continue
    n=$(basename "$dev")
    bridge link set dev "$n" isolated on && found=1
  done
  [ "$found" = 1 ] && exit 0
  sleep 0.25
done
echo "lsa-hook: no interface found for $vmid" >&2
exit 0
