# Linux 實作能力檢驗系統（linux-skill-assessment）

自架的 Linux 實作練習與測驗平台。程式內部簡稱 **lsa**（安裝路徑 `/opt/lsa`、服務 `lsa`、網站容器 `lsa-web`）。

**在自己的 Proxmox VE 上架一套私有的「修好壞掉的 Linux」練習與測驗環境。**
每個人的每一題都有一台**自己的** Linux 環境（LXC 容器或 VM），題目預先「埋好錯誤」，
透過**瀏覽器裡的終端機**登入修好它，按「檢查作答」由 **Ansible** 自動評分。

概念類似 [SadServers](https://sadservers.com/)，但完全自架：題目、帳號、成績、作答紀錄都在你自己的機器上，
開源（MIT）、不需要雲端帳號。

> **English:** a self-hosted, private platform for hands-on Linux troubleshooting practice and assessments on Proxmox VE —
> see [README.en.md](README.en.md).

## 適合誰

- **學校課程**：課堂練習（含點名、課堂成績）與上機考試，幾十人同時作答
- **公司技術面試**：給應徵者一題或數題實機排錯，限時、自動評分，事後可以回看完整的終端機操作紀錄
- **團隊內部訓練**：新人 on-call 訓練、維運演練，用自己環境裡會遇到的問題出題

所有題目都是你自己寫的題目包（YAML＋Ansible），附一題範例題示範格式。

## 功能

- **一人一機**：每位作答者每題一台獨立環境（linked clone，幾秒到幾十秒開好），彼此網路隔離、不能上網
- **瀏覽器終端機**：不用裝任何軟體；可彈出成獨立視窗；可選擇禁止貼上；把網路或 sshd 改壞了會自動改用主控台，不必重置
- **自動評分**：每題多個檢查項目分別給分，盡量從外部驗證結果（作答者改不到評分程式）
- **測驗與練習分開**：「題組」可以是測驗或練習，各自有題目、配分、開放時間、計分方式
  - 測驗進行中，練習題組自動暫停
  - 練習可不限時、可看每個檢查項目是否通過
  - 練習有成績與出席紀錄，可匯出練習總表
- **單題限時、次數限制、冷卻時間**、測驗結束後「最終校驗」（以測驗結束時的環境狀態計分）
- **預發帳號**：產生帳號並列印發給作答者，第一次登入時綁定編號
- **題目包**：一題一個資料夾（YAML + Ansible playbook），後台上傳 zip、一鍵建置範本並**自我測試**
  （未作答應 0 分、套用標準解答應滿分）
- **多國語言**：介面與題目都可多語言，作答者和管理者可自行切換
- **作答紀錄與重播**：每次終端機連線都錄成 asciicast，後台可以像影片一樣重播（點輸入列表跳到該時間），評分之外也能看「怎麼解的」

## 架構

```
           作答者／管理者的瀏覽器
                  │ HTTP / WebSocket（終端機）
                  ▼
 ┌──────────────────────── Proxmox VE 主機 ─────────────────────────┐
 │ vmbr0（區域網路）                                                │
 │   │                                                              │
 │ ┌─┴──────────────────────────┐  PVE API（token、限 pool）        │
 │ │ lsa-web（LXC, VMID 100）   │ ───────────────────────────▶      │
 │ │ FastAPI 網站＋終端機轉接   │                                   │
 │ │ Ansible 評分、SQLite       │                                   │
 │ └─┬──────────────────────────┘                                   │
 │   │ SSH（評分金鑰）                                              │
 │ vmbr1（測驗內網 10.10.0.0/16：不能上網、作答者互相隔離）         │
 │   ├── 作答者 A 第 1 題（LXC）  ├── 作答者 A 第 2 題（VM）…       │
 │   └── 作答者 B 第 1 題（LXC）  …                                 │
 │                                                                  │
 │ 題目範本 9100+（從 base 9000／9001 建置，linked clone 給作答者） │
 └──────────────────────────────────────────────────────────────────┘
```

- 網站跑在 PVE 上的一個 LXC（lsa-web），用 API token 操作 PVE，權限限制在 `lsa` pool
- 作答者環境只接在內網 vmbr1，bridge port isolation 讓作答者之間不能互連，也不能連 PVE 主機或上網
- 題目範本建置時（10.10.255.0/24）才可以上網裝套件

## 文件

| 文件 | 內容 |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | 硬體需求、從零安裝（PVE 設定、範本、lsa-web）、更新、備份 |
| [docs/ADMIN.md](docs/ADMIN.md) | 管理者操作手冊：帳號、題庫、題組（測驗／練習）、測驗當天流程、成績與出席 |
| [docs/QUESTIONS.md](docs/QUESTIONS.md) | 怎麼出題：題目包格式、範例題逐步說明、多國語言 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 容量規劃與壓力測試結果、儲存空間上限、疑難排解 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 本機開發（不需要 PVE）、程式結構、翻譯 |

## 快速開始（摘要）

需要一台裝好 **Proxmox VE 8/9** 的主機（預設的 `local-lvm` 儲存空間即可）。

```bash
# 1. 在 PVE 主機上（root）：建立內網、API 權限、base 範本、lsa-web 容器與設定檔
git clone https://github.com/JalenLin/linux-skill-assessment.git /root/lsa-src
cd /root/lsa-src/deploy && ./pve-setup.sh all        # 視網路速度約 20~40 分鐘

# 2. 在開發機（或 PVE 主機本身）：部署網站程式到 lsa-web
echo 'PVE=root@<PVE IP>' > deploy/deploy.env
./deploy/deploy.sh

# 3. 瀏覽器開 http://<lsa-web IP>/admin
#    帳號 admin，密碼在 PVE 主機的 /root/lsa/admin_password
#    → 「題庫」→ example-hello →「建置範本＋自我測試」
```

詳細步驟見 [docs/INSTALL.md](docs/INSTALL.md)。

## 容量（實測）

Intel N305（8 核）、48GB RAM、NVMe，60 人同時作答、12 題（9 題 LXC、3 題 VM）：

- LXC 環境約 50MB 記憶體，VM 環境約 450MB
- 每位作答者同時只開 1 個環境（預設），60 人在 48GB 內綽綽有餘
- 60 台 VM 同時開機 CPU 會滿載 2~3 分鐘；大量同時檢查時終端機仍順暢（< 0.3 秒）
- ⚠ 預設的 LVM-thin 大約只能容納 **1100 個實例**，超過後無法建立環境（與硬碟剩餘空間無關）；題組結束後請刪除實例

詳見 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 題目

本 repo **只附一題範例題** [`questions/example-hello`](questions/example-hello)（示範所有欄位），
實際測驗題目請另外管理（例如另一個 private repo），用後台上傳 zip 或
`deploy/push-questions.sh <題庫目錄>` 放到系統裡。出題方式見 [docs/QUESTIONS.md](docs/QUESTIONS.md)。

> ⚠ 題目包含 `solution.yml`（標準解答）與 `check.yml`（評分方式），請不要放在作答者看得到的地方。

## 目錄

```
app/              網站程式（FastAPI）
  locales/        介面翻譯（en/…）
  templates/      頁面樣板
  static/         前端（xterm.js 終端機）
deploy/
  pve-setup.sh    在 PVE 主機上執行：內網、權限、base 範本、lsa-web
  pve/            PVE 主機用的防火牆與 hookscript
  web/            lsa-web 內的安裝腳本與 systemd service
  deploy.sh       部署程式到 lsa-web
  push-questions.sh 同步題庫到 lsa-web
questions/        題目格式說明 + 範例題（實際題目不放這裡）
docs/             文件
config.example.yaml  lsa-web 設定檔範例
config.dev.yaml   本機開發用（mock 模式）
```

## 授權

[MIT](LICENSE)。內含的 [xterm.js](https://github.com/xtermjs/xterm.js)（MIT）與 [asciinema-player](https://github.com/asciinema/asciinema-player)（Apache-2.0），見 `app/static/vendor/LICENSE`。
