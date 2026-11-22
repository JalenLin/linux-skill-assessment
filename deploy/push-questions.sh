#!/bin/bash
# 把本機的題目資料夾同步到 lsa-web 的題庫（/var/lib/lsa/questions）。
#   ./deploy/push-questions.sh <題庫目錄> [題目id ...]
# 例：./deploy/push-questions.sh ../my-questions            # 全部
#     ./deploy/push-questions.sh ../my-questions nginx-9090 # 只更新一題
# 同名題目會整個取代。改了 setup.yml 或 files/ 的題目，到後台「題庫」按「重新建置」；
# 只改 description / check.yml / 翻譯不用重建。也可以改用後台上傳 zip。
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f deploy/deploy.env ] && . deploy/deploy.env
: "${PVE:?請設定 PVE=root@<PVE 主機 IP>（或寫在 deploy/deploy.env）}"
WEB_VMID=${WEB_VMID:-100}
SRC=${1:?用法：$0 <題庫目錄> [題目id ...]}; shift
cd "$SRC"
if [ $# -gt 0 ]; then QS=("$@"); else QS=(); for d in */; do [ -f "$d/question.yaml" ] && QS+=("${d%/}"); done; fi
[ ${#QS[@]} -gt 0 ] || { echo "在 $SRC 找不到題目（需要 <id>/question.yaml）"; exit 1; }
for q in "${QS[@]}"; do [ -f "$q/question.yaml" ] || { echo "$q/question.yaml 不存在"; exit 1; }; done
echo "同步 ${#QS[@]} 題：${QS[*]}"
tar czf - --exclude='__pycache__' "${QS[@]}" \
  | ssh "$PVE" "cat > /tmp/lsa-q.tgz && pct push $WEB_VMID /tmp/lsa-q.tgz /tmp/lsa-q.tgz && rm /tmp/lsa-q.tgz"
ssh "$PVE" "pct exec $WEB_VMID -- bash -c 'set -e; cd /var/lib/lsa/questions; for q in ${QS[*]}; do rm -rf \"\$q\"; done; tar xzf /tmp/lsa-q.tgz; rm /tmp/lsa-q.tgz; chown -R lsa:lsa .'"
echo "完成。後台「題庫」頁會顯示哪些題目需要重新建置。"
