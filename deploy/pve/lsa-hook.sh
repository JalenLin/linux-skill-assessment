#!/bin/bash
# PVE hookscript：作答者實例啟動後，把它在 vmbr1 上的管理網卡（eth0 = veth<vmid>i0／tap<vmid>i0）設為 isolated：
# isolated port 之間無法互通，只能和非 isolated 的 port（lsa-web）通訊。
# vmbr1 是 VLAN-aware：沒有 tag 的 port 預設是 trunk（接受所有 VLAN），所以 eth0 只留 VLAN 1，
# 否則作答者可以自建 eth0.<VLAN> 連進別組的題目網路。
# 多節點題的題目網路（eth1… 有 tag）不設 isolated：同一個 VLAN 內的機器要互通。
vmid="$1"; phase="$2"
[ "$phase" = "post-start" ] || exit 0
for _ in $(seq 1 40); do
  for n in veth${vmid}i0 tap${vmid}i0; do
    [ -e /sys/class/net/$n ] || continue
    bridge link set dev "$n" isolated on
    if [ "$(cat /sys/class/net/vmbr1/bridge/vlan_filtering 2>/dev/null)" = 1 ]; then
      bridge vlan del dev "$n" vid 2-4094 2>/dev/null || true
    fi
    exit 0
  done
  sleep 0.25
done
echo "lsa-hook: no interface found for $vmid" >&2
exit 0
