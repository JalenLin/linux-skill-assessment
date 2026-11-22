#!/bin/bash
# 在 PVE 主機上以 root 執行（可重複執行）。
#   ./pve-setup.sh network   建立 vmbr1 測驗內網 + 防火牆 + hookscript
#   ./pve-setup.sh perms     建立 pool / API 使用者與 token（token 只會印出一次）
#   ./pve-setup.sh base      建立 LXC base 範本 (VMID 9000)
#   ./pve-setup.sh basevm    建立 VM (cloud-init) base 範本 (VMID 9001)，給需要 kernel 功能的題目
#   ./pve-setup.sh web       建立 lsa-web 容器 (VMID 100)
#   ./pve-setup.sh config    產生 lsa-web 設定檔（隨機管理密碼）並放進 lsa-web
#   ./pve-setup.sh all       以上全部，依序執行
set -euo pipefail
cd "$(dirname "$0")"

BASE_VMID=${BASE_VMID:-9000}
BASEVM_VMID=${BASEVM_VMID:-9001}
CLOUDIMG_URL=${CLOUDIMG_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}
WEB_VMID=${WEB_VMID:-100}
WEB_NET0=${WEB_NET0:-"ip=dhcp"}          # 例如 WEB_NET0="ip=192.168.1.20/24,gw=192.168.1.1"
STORAGE=${STORAGE:-local-lvm}
TZ_NAME=${TZ_NAME:-Asia/Taipei}         # 時區（作答者環境、lsa-web、成績時間）
POOL=lsa
# LXC 映像：預設用 PVE 下載清單上最新的 Ubuntu 24.04 standard（清單取不到時用固定版本）
TEMPLATE=${TEMPLATE:-$( (pveam update >/dev/null 2>&1; pveam available --section system 2>/dev/null) \
  | awk '/ubuntu-24\.04-standard/ {print $2}' | sort -V | tail -1 || true)}
TEMPLATE=${TEMPLATE:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}
KEYDIR=${KEYDIR:-/root/lsa}
NODE=$(hostname)

log() { echo -e "\e[1;34m==> $*\e[0m"; }

network() {
  log "vmbr1 測驗內網"
  if ! grep -q "^auto vmbr1" /etc/network/interfaces; then
    cp /etc/network/interfaces "/etc/network/interfaces.bak.$(date +%s)"
    cat >> /etc/network/interfaces <<'EOF'

auto vmbr1
iface vmbr1 inet static
	address 10.10.0.1/16
	bridge-ports none
	bridge-stp off
	bridge-fd 0
	post-up /usr/local/sbin/lsa-net.sh
#linux-skill-assessment internal network
EOF
  fi
  install -m 755 pve/lsa-net.sh /usr/local/sbin/lsa-net.sh
  ifreload -a
  /usr/local/sbin/lsa-net.sh

  log "hookscript（bridge port isolation）"
  cur=$(pvesh get /storage/local --output-format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["content"])')
  case ",$cur," in *,snippets,*) ;; *) pvesm set local --content "${cur},snippets" ;; esac
  mkdir -p /var/lib/vz/snippets
  install -m 755 pve/lsa-hook.sh /var/lib/vz/snippets/lsa-hook.sh
}

perms() {
  log "pool / 使用者 / token"
  pveum pool list | grep -qw $POOL || pveum pool add $POOL --comment "linux-skill-assessment instances"
  pveum role list | grep -qw LsaPool || pveum role add LsaPool --privs "Pool.Audit,Pool.Allocate"
  pveum user list | grep -qw lsa@pve || pveum user add lsa@pve --comment "linux-skill-assessment API"
  mkdir -p $KEYDIR && chmod 700 $KEYDIR
  if ! pveum user token list lsa@pve 2>/dev/null | grep -qw web; then
    pveum user token add lsa@pve web --privsep 1 --comment "lsa-web" --output-format json > $KEYDIR/token.json
    chmod 600 $KEYDIR/token.json
    echo "token 已存到 $KEYDIR/token.json（只會產生一次）"
  fi
  # privsep token 的權限 = 使用者權限 ∩ token 權限，所以兩者都要給
  pveum acl modify /pool/$POOL --roles PVEVMAdmin,LsaPool --users lsa@pve
  pveum acl modify /pool/$POOL --roles PVEVMAdmin,LsaPool --tokens 'lsa@pve!web'
  pveum acl modify /storage/$STORAGE --roles PVEDatastoreUser --users lsa@pve
  pveum acl modify /storage/$STORAGE --roles PVEDatastoreUser --tokens 'lsa@pve!web'
  pveum acl modify /sdn/zones/localnetwork --roles PVESDNUser --users lsa@pve
  pveum acl modify /sdn/zones/localnetwork --roles PVESDNUser --tokens 'lsa@pve!web'
}

grader_key() {
  mkdir -p $KEYDIR && chmod 700 $KEYDIR
  [ -f $KEYDIR/grader_key ] || ssh-keygen -q -t ed25519 -N '' -C lsa-grader -f $KEYDIR/grader_key
}

base() {
  grader_key
  log "LXC 範本映像 $TEMPLATE"
  [ -f /var/lib/vz/template/cache/$TEMPLATE ] || pveam download local $TEMPLATE
  if pct status $BASE_VMID >/dev/null 2>&1; then
    echo "VMID $BASE_VMID 已存在，略過（要重建請先 pct destroy $BASE_VMID）"; return
  fi
  log "建立 base 容器 $BASE_VMID"
  pct create $BASE_VMID local:vztmpl/$TEMPLATE \
    --hostname lsa-base --unprivileged 1 --features nesting=1,keyctl=1 \
    --cores 1 --memory 512 --swap 256 --rootfs $STORAGE:8 \
    --net0 name=eth0,bridge=vmbr1,ip=10.10.255.250/16,gw=10.10.0.1,firewall=0 \
    --nameserver 1.1.1.1 --timezone $TZ_NAME --ostype ubuntu \
    --hookscript local:snippets/lsa-hook.sh --pool $POOL --tags lsa-base
  pct start $BASE_VMID
  for _ in $(seq 30); do pct exec $BASE_VMID -- getent hosts archive.ubuntu.com >/dev/null 2>&1 && break; sleep 1; done
  pct push $BASE_VMID $KEYDIR/grader_key.pub /tmp/grader_key.pub
  pct exec $BASE_VMID -- bash -euxc '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get -y full-upgrade
    apt-get install -y --no-install-recommends openssh-server python3 python3-apt sudo vim nano curl wget \
      less man-db iproute2 iputils-ping net-tools dnsutils cron rsyslog bash-completion lsof psmisc procps \
      tree file locales ca-certificates gnupg
    locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8
    # 測驗時沒有網路：關掉自動更新避免卡住
    systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
    apt-get purge -y unattended-upgrades || true
    # 環境內的帳號 user / user，可 sudo
    id user >/dev/null 2>&1 || useradd -m -s /bin/bash user
    echo user:user | chpasswd
    usermod -aG sudo user
    echo "user ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-lsa-user && chmod 440 /etc/sudoers.d/90-lsa-user
    for h in /root /home/user; do
      install -d -m 700 $h/.ssh
      cat /tmp/grader_key.pub > $h/.ssh/authorized_keys
      chmod 600 $h/.ssh/authorized_keys
    done
    chown -R user:user /home/user/.ssh
    rm /tmp/grader_key.pub
    passwd -l root
    sed -i "s/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/" /etc/ssh/sshd_config
    systemctl enable ssh
    apt-get clean
    rm -rf /var/lib/apt/lists/*
  '
  pct shutdown $BASE_VMID
  pct set $BASE_VMID --net0 name=eth0,bridge=vmbr1,ip=10.10.255.250/16,firewall=0
  pct template $BASE_VMID
  log "base 範本完成"
}

basevm() {
  grader_key
  if qm status $BASEVM_VMID >/dev/null 2>&1; then
    echo "VMID $BASEVM_VMID 已存在，略過（要重建請先 qm destroy $BASEVM_VMID）"; return
  fi
  img=/var/lib/vz/template/cache/$(basename $CLOUDIMG_URL)
  [ -f $img ] || { log "下載 cloud image"; wget -q -O $img.part $CLOUDIMG_URL && mv $img.part $img; }
  ip=10.10.255.251
  log "建立 base VM $BASEVM_VMID"
  qm create $BASEVM_VMID --name lsa-base-vm --memory 1024 --cores 1 --cpu host --ostype l26 \
    --net0 virtio,bridge=vmbr1,firewall=0 --scsihw virtio-scsi-single \
    --scsi0 $STORAGE:0,import-from=$img,discard=on --ide2 $STORAGE:cloudinit --boot order=scsi0 \
    --serial0 socket --vga serial0 \
    --ipconfig0 ip=$ip/16,gw=10.10.0.1 --nameserver 1.1.1.1 \
    --ciuser user --cipassword user --sshkeys $KEYDIR/grader_key.pub --ciupgrade 0 \
    --hookscript local:snippets/lsa-hook.sh --pool $POOL --tags lsa-base
  qm disk resize $BASEVM_VMID scsi0 8G
  qm start $BASEVM_VMID
  SSH="ssh -i $KEYDIR/grader_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 user@$ip"
  for _ in $(seq 90); do $SSH true 2>/dev/null && break; sleep 2; done
  $SSH 'cloud-init status --wait >/dev/null 2>&1; sudo bash -euxc "
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends python3 python3-apt sudo vim nano curl wget less man-db \
      iproute2 iputils-ping net-tools dnsutils cron rsyslog bash-completion lsof psmisc tree file locales
    locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8
    timedatectl set-timezone '"$TZ_NAME"'
    systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
    apt-get purge -y unattended-upgrades snapd || true
    echo \"user ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/90-lsa-user && chmod 440 /etc/sudoers.d/90-lsa-user
    install -d -m 700 /root/.ssh && cp /home/user/.ssh/authorized_keys /root/.ssh/authorized_keys
    sed -i \"s/^#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/\" /etc/ssh/sshd_config
    rm -f /etc/ssh/sshd_config.d/*cloudimg*
    printf \"disable_root: false\\nssh_deletekeys: true\\n\" > /etc/cloud/cloud.cfg.d/99-lsa.cfg
    apt-get clean
    cloud-init clean --logs
    truncate -s 0 /etc/machine-id
  "'
  qm shutdown $BASEVM_VMID --timeout 120
  qm set $BASEVM_VMID --ipconfig0 ip=$ip/16
  qm template $BASEVM_VMID
  log "base VM 範本完成"
}

web() {
  grader_key
  [ -f /var/lib/vz/template/cache/$TEMPLATE ] || pveam download local $TEMPLATE
  if ! pct status $WEB_VMID >/dev/null 2>&1; then
    log "建立 lsa-web 容器 $WEB_VMID"
    pct create $WEB_VMID local:vztmpl/$TEMPLATE \
      --hostname lsa-web --unprivileged 1 --features nesting=1 \
      --cores 4 --memory 4096 --swap 1024 --rootfs $STORAGE:20 \
      --net0 name=eth0,bridge=vmbr0,$WEB_NET0 \
      --net1 name=eth1,bridge=vmbr1,ip=10.10.0.2/16 \
      --timezone $TZ_NAME --ostype ubuntu --onboot 1 --tags lsa-web
    pct start $WEB_VMID
    sleep 5
  fi
  pct exec $WEB_VMID -- mkdir -p /etc/lsa
  pct push $WEB_VMID $KEYDIR/grader_key /etc/lsa/grader_key --perms 600
  log "lsa-web IP："
  pct exec $WEB_VMID -- ip -br -4 addr
}

config() {
  [ -f $KEYDIR/token.json ] || { echo "找不到 $KEYDIR/token.json，請先執行 perms"; exit 1; }
  if [ ! -f $KEYDIR/config.yaml ]; then
    log "產生 $KEYDIR/config.yaml"
    local token pw sk host
    token=$(python3 -c "import json; print(json.load(open('$KEYDIR/token.json'))['value'])")
    pw=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12)))")
    sk=$(python3 -c "import secrets; print(secrets.token_urlsafe(36))")
    host=$(ip -4 -o addr show vmbr0 | awk '{print $4}' | cut -d/ -f1 | head -1)
    umask 077
    echo "$pw" > $KEYDIR/admin_password
    cat > $KEYDIR/config.yaml <<EOF
secret_key: "$sk"
admin_user: admin
admin_password: "$pw"
data_dir: /var/lib/lsa/data
questions_dir: /var/lib/lsa/questions
pve_host: $host
pve_node: $NODE
pve_user: lsa@pve
pve_token_name: web
pve_token_value: "$token"
pve_storage: $STORAGE
ssh_key: /etc/lsa/grader_key
timezone: $TZ_NAME
EOF
  else
    echo "$KEYDIR/config.yaml 已存在，沿用（要重新產生請先刪除）"
  fi
  pct exec $WEB_VMID -- mkdir -p /etc/lsa
  pct push $WEB_VMID $KEYDIR/config.yaml /etc/lsa/config.yaml --perms 600
  log "設定檔已放到 lsa-web:/etc/lsa/config.yaml；後台帳號 admin，密碼在 $KEYDIR/admin_password"
}

case "${1:-}" in
  network) network ;;
  perms) perms ;;
  base) base ;;
  basevm) basevm ;;
  web) web ;;
  config) config ;;
  all) network; perms; base; basevm; web; config ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
