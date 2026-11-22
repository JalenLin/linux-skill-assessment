#!/bin/bash
# vmbr1 (測驗內網 10.10.0.0/16) 的防火牆規則，由 /etc/network/interfaces 的 post-up 呼叫
# - 作答者實例不能連 PVE 主機本身、不能上網
# - 只有建置範圍 10.10.255.0/24 會 NAT 上網（建題目範本時 apt install 用）
set -e
BR=vmbr1; UP=vmbr0; BUILD=10.10.255.0/24
sysctl -qw net.ipv4.ip_forward=1
sysctl -qw net.ipv6.conf.$BR.disable_ipv6=1 || true

iptables -N LSA-IN 2>/dev/null || iptables -F LSA-IN
iptables -C INPUT -i $BR -j LSA-IN 2>/dev/null || iptables -I INPUT -i $BR -j LSA-IN
iptables -A LSA-IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A LSA-IN -j DROP

iptables -N LSA-FWD 2>/dev/null || iptables -F LSA-FWD
iptables -C FORWARD -j LSA-FWD 2>/dev/null || iptables -I FORWARD -j LSA-FWD
iptables -A LSA-FWD -i $BR -o $BR -j ACCEPT                  # 橋接內（br_netfilter 載入時才會經過）
iptables -A LSA-FWD -i $BR -s $BUILD -o $UP -j ACCEPT
iptables -A LSA-FWD -i $UP -o $BR -d $BUILD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A LSA-FWD -i $BR -j DROP
iptables -A LSA-FWD -o $BR -j DROP

iptables -t nat -N LSA-NAT 2>/dev/null || iptables -t nat -F LSA-NAT
iptables -t nat -C POSTROUTING -j LSA-NAT 2>/dev/null || iptables -t nat -I POSTROUTING -j LSA-NAT
iptables -t nat -A LSA-NAT -s $BUILD -o $UP -j MASQUERADE
