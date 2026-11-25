# 安裝與部署

> English: [en/INSTALL.md](en/INSTALL.md)

本文從一台空的 Proxmox VE 主機開始，到作答者可以登入測驗為止。

- [1. 硬體與軟體需求](#1-硬體與軟體需求)
- [2. 安裝 Proxmox VE](#2-安裝-proxmox-ve)
- [3. 在 PVE 主機上建立環境（pve-setup.sh）](#3-在-pve-主機上建立環境pve-setupsh)
- [4. 部署網站程式（deploy.sh）](#4-部署網站程式deploysh)
- [5. 第一次登入與建置範例題](#5-第一次登入與建置範例題)
- [6. 放入你的題目](#6-放入你的題目)
- [7. 測驗前的網路設定](#7-測驗前的網路設定)
- [8. 更新程式](#8-更新程式)
- [9. 備份與還原](#9-備份與還原)
- [10. 安全性說明](#10-安全性說明)

---

## 1. 硬體與軟體需求

| 項目 | 需求 |
|---|---|
| 主機 | 一台實體機，裝 **Proxmox VE 8 或 9** |
| CPU | 支援虛擬化（VT-x/AMD-V）；VM 題越多越吃 CPU |
| 記憶體 | 依人數與題型，見下表 |
| 硬碟 | SSD，數百 GB 以上；預設 `local-lvm`（LVM-thin）即可 |
| 網路 | 一個能讓作答者電腦連到的網段（vmbr0） |

**實測參考**（Intel N305 8 核、48GB、NVMe，60 人，詳見 [OPERATIONS.md](OPERATIONS.md)）：

- 每位作答者同時只開 1 題（預設）：60 人同時作答，最壞情況約 31GB，**48GB 夠用**
- 每個 LXC 環境約 50MB、每個 VM 環境約 450MB 記憶體
- 60 台 VM 同時開機時 CPU 會滿載 2~3 分鐘；平常作答、檢查時 CPU 很輕鬆
- 粗估記憶體：`4GB（系統＋網站）＋ 人數 × 0.45GB`（假設全部都在做 VM 題）

> ⚠ **實例數量上限**：預設的 `local-lvm` 大約只能容納 **1100 個實例**（LVM metadata 在安裝 PVE 時就固定、事後不能放大），
> 超過後建立環境會失敗，與硬碟剩多少空間無關。計算方式是「作答者數 × 題數」，練習題組的實例會一直累積，
> 題組結束後要記得刪除實例。詳見 [OPERATIONS.md](OPERATIONS.md#儲存空間lvm上限)。

## 2. 安裝 Proxmox VE

1. 依官方說明安裝 PVE，使用預設的 LVM-thin（`local-lvm`）
   - 預估同時存在的實例會超過約 1000 個（例如 100 人 × 12 題），安裝時改選 **ZFS**，
     或另外準備一顆硬碟做大 metadata 的儲存空間（見 [OPERATIONS.md](OPERATIONS.md#儲存空間lvm上限)）；裝好之後就不能改了
2. 安裝時設定 vmbr0 的 IP（作答者要能連到的網段）
3. 安裝完成後更新套件：`apt update && apt full-upgrade`

> 主機名稱（例如 `pve`）就是之後設定檔裡的 `pve_node`，`pve-setup.sh config` 會自動填入。

## 3. 在 PVE 主機上建立環境（pve-setup.sh）

把本 repo 放到 PVE 主機上（`apt install git` 後 clone，或從別台電腦 `scp -r`），以 root 執行：

```bash
cd /root/lsa-src/deploy
./pve-setup.sh all
```

`all` 會依序執行以下步驟。每一步都可以單獨執行、重複執行（已完成的會略過）：

| 步驟 | 做什麼 | 時間 |
|---|---|---|
| `network` | 建立測驗內網 **vmbr1**（10.10.0.0/16，VLAN-aware：多節點題的題目網路用）與防火牆規則；安裝 hookscript（作答者之間 port isolation） | 數秒 |
| `perms` | 建立 pool `lsa`、API 使用者 `lsa@pve` 與 token（存到 `/root/lsa/token.json`），權限只限 pool `lsa` | 數秒 |
| `base` | 下載 Ubuntu 24.04 LXC 映像，建立 **LXC base 範本 9000**（帳號 `user`/`user` 可 sudo、openssh、python3、評分金鑰） | 5~10 分鐘 |
| `basevm` | 下載 Ubuntu 24.04 cloud image，建立 **VM base 範本 9001**（給需要 kernel 功能的題目） | 5~15 分鐘 |
| `web` | 建立網站容器 **lsa-web（VMID 100）**，接 vmbr0 與 vmbr1（10.10.0.2） | 1~2 分鐘 |
| `config` | 產生設定檔 `/root/lsa/config.yaml`（隨機管理密碼、PVE token），放進 lsa-web | 數秒 |

可用環境變數調整：

```bash
WEB_NET0="ip=192.168.1.20/24,gw=192.168.1.1" ./pve-setup.sh web   # lsa-web 用固定 IP（預設 DHCP）
STORAGE=local-lvm ./pve-setup.sh all                              # 範本與作答者環境放的儲存空間
TZ_NAME=Asia/Taipei ./pve-setup.sh all                            # 時區（預設 Asia/Taipei），也寫進設定檔的 timezone
WEB_VMID=100 BASE_VMID=9000 BASEVM_VMID=9001                      # VMID
```

完成後，PVE 主機的 `/root/lsa/` 有這些檔案（**都是機密，不要外流**）：

| 檔案 | 用途 |
|---|---|
| `token.json` | PVE API token（只會產生一次） |
| `grader_key`, `grader_key.pub` | 評分程式登入作答者環境的 SSH 金鑰（公鑰已放進 base 範本） |
| `config.yaml` | lsa-web 的設定檔（也放在 lsa-web 的 `/etc/lsa/config.yaml`） |
| `admin_password` | 後台管理密碼 |

設定檔的欄位說明見 [`config.example.yaml`](../config.example.yaml)；完整的設定與預設值見 `app/config.py`。

### VMID 與 IP 的安排

| 範圍 | 用途 |
|---|---|
| 100 | lsa-web |
| 9000 / 9001 | LXC / VM base 範本 |
| 9100–9999 | 各題目範本（建置時用 10.10.255.0/24，可上網） |
| 10000–19999 | 作答者環境（IP 由 VMID 決定：10.10.1.2 起） |

## 4. 部署網站程式（deploy.sh）

`deploy.sh` 會把程式打包，經 PVE 主機放進 lsa-web，然後在 lsa-web 裡執行
`deploy/web/install.sh`：建立 Python venv、安裝套件（含 Ansible）、設定 systemd 服務與防火牆。

在開發機（能 `ssh root@<PVE>` 的電腦）上：

```bash
echo 'PVE=root@192.168.1.10' > deploy/deploy.env    # PVE 主機；這個檔案不會進 git
./deploy/deploy.sh
```

也可以直接在 PVE 主機上執行（需要 root 能 ssh 到自己，例如 `PVE=root@127.0.0.1`）。

部署後：

- 程式在 lsa-web 的 `/opt/lsa`，服務名稱 `lsa`（port 80）
- 資料在 `/var/lib/lsa/data`（`lsa.db`、終端機紀錄 `shell_logs/a<帳號 id>/*.cast`）
- 題庫在 `/var/lib/lsa/questions`（**部署不會覆蓋**，第一次安裝只放入範例題）
- 看紀錄：`pct exec 100 -- journalctl -u lsa -f`

## 5. 第一次登入與建置範例題

1. 查 lsa-web 的 IP：`pct exec 100 -- ip -br -4 addr show eth0`
2. 瀏覽器開 `http://<lsa-web IP>/admin`，帳號 `admin`，密碼在 PVE 主機的 `/root/lsa/admin_password`
3. 「系統設定」：改網站標題、確認編號格式
4. 「題庫」→ `example-hello` →「**建置範本＋自我測試**」，約 1~2 分鐘，應顯示
   未作答 0/10、套用解答 10/10、自我測試通過
5. 「題組」→ 新增一個練習題組，加入 example-hello，勾選「作答者可見」
6. 「帳號」→ 產生幾個測試帳號，用另一個瀏覽器（或無痕視窗）以作答者身分登入試做

## 6. 放入你的題目

題目不在本 repo 裡，另外管理。放進系統有兩種方式：

- **後台上傳**：「題庫」→ 上傳 zip（一個 zip 一題，zip 裡是題目資料夾）
- **指令同步**：`./deploy/push-questions.sh <題庫目錄> [題目id ...]`，同名題目會被取代

放進去之後在「題庫」頁按「建置範本＋自我測試」。之後改了 `setup.yml` 或 `files/`，
題庫頁會標示「需要重新建置」；只改 `description`、`check.yml`、翻譯則不用重建。

出題方式見 [QUESTIONS.md](QUESTIONS.md)。

## 7. 測驗前的網路設定

- **lsa-web 改成固定 IP**（DHCP 換了 IP 作答者就連不到）：
  `pct set 100 --net0 name=eth0,bridge=vmbr0,ip=192.168.1.20/24,gw=192.168.1.1 && pct reboot 100`
  （eth1 vmbr1 那張不要動）
- 需要 HTTPS 或網域名稱時，可在前面加反向代理（nginx、Caddy 等），**WebSocket（`/ws/`）要一併轉發**；
  服務已開啟 `--proxy-headers`
- **改掉後台密碼**：編輯 PVE 主機上的 `/root/lsa/config.yaml` 的 `admin_password`，
  再執行 `./pve-setup.sh config` 放進 lsa-web，然後 `pct exec 100 -- systemctl restart lsa`
- 只讓測驗場地的網段連得到 lsa-web，可以在 PVE 防火牆或校園網路設備上限制

## 8. 更新程式

```bash
git pull
./deploy/deploy.sh
```

- 資料庫結構有變時會在啟動時自動升級，不用手動處理；保險起見先備份（下一節）
- 服務重啟時，正在使用的終端機會斷線並自動重連；**測驗進行中請不要更新**
- **從沒有多節點題的版本升級**（2026-11 以前安裝的）：要用多節點題，先在 PVE 主機上重新執行一次
  `./pve-setup.sh network`。它會把 vmbr1 改成 VLAN-aware（原檔備份為 `/etc/network/interfaces.bak.*`）並更新 hookscript；
  現有的單機題不受影響，正在運作的環境也不用停。沒做的話，多節點題同一組的機器之間會互相連不到

## 9. 備份與還原

要備份的東西：

| 位置 | 內容 |
|---|---|
| lsa-web `/var/lib/lsa/data/lsa.db` | 帳號、題組、成績、出席、作答紀錄（SQLite） |
| lsa-web `/var/lib/lsa/data/shell_logs/` | 終端機紀錄 |
| lsa-web `/var/lib/lsa/questions/` | 題庫（若另有 repo 管理可略） |
| PVE `/root/lsa/` | token、金鑰、設定檔 |

```bash
# 在 PVE 主機上：線上備份資料庫（服務運作中也可以）
pct exec 100 -- sqlite3 /var/lib/lsa/data/lsa.db ".backup /var/lib/lsa/data/lsa-$(date +%F).db"
pct pull 100 /var/lib/lsa/data/lsa-$(date +%F).db /root/lsa-backup-$(date +%F).db
```

或直接用 PVE 的 vzdump 備份整個 lsa-web 容器（VMID 100）。
作答者環境與題目範本不需要備份（範本可從題目重新建置）。

還原：停止服務、把 `lsa.db` 放回去、改回擁有者 `lsa`、啟動服務。

## 10. 安全性說明

- 作答者環境只在 vmbr1：不能上網、不能連 PVE 主機、彼此不能互連（hookscript 設定 bridge port isolation）
- 多節點題同一組的機器之間另有題目網路（eth1…），每組一個 VLAN，組與組之間互相看不到
- lsa-web 的 vmbr1 介面只允許主動連出（作答者環境連不到網站的任何服務）
- PVE API token 只有 pool `lsa` 與指定儲存空間的權限，碰不到其他 VM
- 作答者在自己的環境裡有 sudo；評分盡量從 lsa-web 從外部驗證（見 QUESTIONS.md）
- 機密檔案：`/root/lsa/*`（PVE）、`/etc/lsa/*`（lsa-web）；不要放進 git
