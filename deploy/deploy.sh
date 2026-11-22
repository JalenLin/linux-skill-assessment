#!/bin/bash
# 從開發機把程式部署到 lsa-web（經 PVE 主機 pct push），並執行 deploy/web/install.sh。
#   PVE=root@<PVE IP> WEB_VMID=100 ./deploy/deploy.sh
# 也可以把 PVE=... WEB_VMID=... 寫在 deploy/deploy.env（不會進 git）。
# 題庫不會被覆蓋：/var/lib/lsa/questions 只在第一次安裝時放入範例題；
# 更新題目用後台上傳 zip，或 deploy/push-questions.sh。
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f deploy/deploy.env ] && . deploy/deploy.env
: "${PVE:?請設定 PVE=root@<PVE 主機 IP>（或寫在 deploy/deploy.env）}"
WEB_VMID=${WEB_VMID:-100}
tar czf - --exclude='__pycache__' app deploy requirements.txt questions/README.md questions/example-hello \
  | ssh "$PVE" "cat > /tmp/lsa.tgz && pct push $WEB_VMID /tmp/lsa.tgz /tmp/lsa.tgz && rm /tmp/lsa.tgz"
ssh "$PVE" "pct exec $WEB_VMID -- bash -c 'mkdir -p /opt/lsa && cd /opt/lsa && rm -rf app deploy questions && tar xzf /tmp/lsa.tgz -C /opt/lsa && rm /tmp/lsa.tgz && /opt/lsa/deploy/web/install.sh'"
