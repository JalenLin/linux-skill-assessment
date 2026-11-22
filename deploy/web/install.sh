#!/bin/bash
# 在 lsa-web 容器內以 root 執行；程式碼已放在 /opt/lsa
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
PKGS="python3-venv python3-pip openssh-client nftables ca-certificates sqlite3"
if ! dpkg -s $PKGS >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends $PKGS
fi
id lsa >/dev/null 2>&1 || useradd -r -m -d /var/lib/lsa -s /usr/sbin/nologin lsa
cd /opt/lsa
[ -x .venv/bin/pip ] || { rm -rf .venv; python3 -m venv .venv; }
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p /var/lib/lsa/data /var/lib/lsa/questions
# 題庫放在資料目錄（上傳的題目才不會被程式更新覆蓋）；只複製還沒有的題目（首次安裝＝範例題）
shopt -s nullglob
for d in questions/*/; do
  n=$(basename "$d"); [ -e /var/lib/lsa/questions/$n ] || cp -r "$d" /var/lib/lsa/questions/
done
chown -R lsa:lsa /var/lib/lsa
chown root:lsa /etc/lsa /etc/lsa/grader_key /etc/lsa/config.yaml
chmod 750 /etc/lsa; chmod 640 /etc/lsa/grader_key /etc/lsa/config.yaml

# 內網 eth1 只允許 lsa-web 主動連出（作答者實例不能連 lsa-web 的任何服務）
cat > /etc/nftables.conf <<'NFT'
#!/usr/sbin/nft -f
flush ruleset
table inet lsa {
  chain input {
    type filter hook input priority 0; policy accept;
    iifname "eth1" ct state established,related accept
    iifname "eth1" drop
  }
}
NFT
systemctl enable --now nftables
nft -f /etc/nftables.conf

install -m 644 deploy/web/lsa.service /etc/systemd/system/lsa.service
systemctl daemon-reload
systemctl enable lsa
systemctl restart lsa
sleep 2
systemctl --no-pager status lsa | head -5
