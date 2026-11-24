# 開發

> English: [en/DEVELOPMENT.md](en/DEVELOPMENT.md)

## 本機開發（不需要 PVE）

`mock` 模式不連 PVE、不跑 Ansible：建立環境只記狀態、建置範本與自我測試直接視為通過、終端機會回顯輸入、檢查預設 0 分（在終端機輸入 `mock-pass` 可讓檢查通過）。
用來改畫面、流程、翻譯很方便。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
LSA_CONFIG=config.dev.yaml .venv/bin/uvicorn app.main:app --reload --port 8000
```

- 作答者端 http://127.0.0.1:8000/ ，後台 http://127.0.0.1:8000/admin （`admin` / `admin`，見 `config.dev.yaml`）
- 資料在 `data-dev/`（不進 git），刪掉就是全新的資料庫
- 題目放在 `questions/`（範例題以外不進 git）

要測真實的建置、終端機與評分，需要部署到 PVE（見 [INSTALL.md](INSTALL.md)），
改完用 `./deploy/deploy.sh` 部署到測試機。

## 程式結構

| 檔案 | 內容 |
|---|---|
| `app/main.py` | FastAPI 進入點：作答者端頁面與 API、WebSocket 終端機、背景工作（狀態同步、閒置暫停、題組結束停止環境） |
| `app/admin.py` | 後台所有頁面與操作 |
| `app/config.py` | 設定檔載入（`LSA_CONFIG`），所有設定與預設值 |
| `app/db.py` | SQLite 結構、自動升級（migration）、系統設定預設值 |
| `app/sets.py` | 題組：開放狀態、測驗時練習暫停、配分、出席計算 |
| `app/questions.py` | 題目包載入與驗證、zip 上傳、題目翻譯 |
| `app/build.py` | 建置題目範本、自我測試（也可從指令列執行 `python -m app.build <qid>`） |
| `app/instances.py` | 作答者環境生命週期：建立（linked clone）、啟動、暫停、重置、同時運作上限 |
| `app/pve.py` | PVE API 包裝（proxmoxer）；`MockPVE` 給 mock 模式 |
| `app/grader.py` | 檢查排隊與執行（ansible-runner）、計分政策、成績計算 |
| `app/attempts.py` | 單題限時與次數 |
| `app/shell.py` | 終端機：WebSocket ↔ SSH（asyncssh），SSH 不通時改用主控台；禁止貼上 |
| `app/console.py` | PVE 主控台（termproxy／vncwebsocket）用戶端 |
| `app/recording.py` | 終端機紀錄：asciicast v2 錄製、輸入列表、純文字檢視、隨帳號／題組刪除 |
| `app/security.py` | 密碼雜湊（scrypt）與隨機密碼；session 用 Starlette SessionMiddleware（`secret_key` 簽章） |
| `app/i18n.py` | 多國語言 |
| `app/templates/` | Jinja2 樣板（`admin/` 為後台） |
| `app/static/` | 前端 JS/CSS；`vendor/` 是 xterm.js |
| `deploy/` | 安裝與部署腳本（見 INSTALL.md） |
| `tools/` | 開發小工具 |

### 資料流

- **開題**：`POST /api/s/{set}/q/{qid}/start` → `instances.ensure_running`
  （沒有實例就從範本 linked clone；超過同時運作上限就先正常關機最久沒用的那個）→ 等 SSH 可連
- **終端機**：`/ws/shell/{set}/{qid}` → `shell.serve` 用評分金鑰以 `user` SSH 進作答者環境，雙向轉送。
  SSH 連不上（等 10／30 秒後仍不通）就改走 PVE 主控台：API token 呼叫 `termproxy`，再連 `vncwebsocket`（token 需要 VM.Console，`PVEVMAdmin` 已包含）
- **檢查**：`POST .../check` → `grader.enqueue` 排隊（同時 `grader_concurrency` 個）→ ansible-runner 跑
  `check.yml` → 依 task 名稱 `check:<id>` 統計各項通過與否 → 存入 `check_runs`
- **成績**：依題組計分政策從 `check_runs` 計算（配分改了會依新配分重算）

### 資料庫

SQLite（`data_dir/lsa.db`），結構在 `app/db.py` 的 `SCHEMA`，版本記在 `PRAGMA user_version`。

改結構時：

1. 修改 `SCHEMA`（新安裝直接建出最新版）
2. `SCHEMA_VERSION` 加 1
3. 在 `MIGRATIONS` 加上 `{新版本: "從上一版升級的 SQL"}`（既有資料庫啟動時依序執行）

主要資料表：`accounts`（`participant_code` = 編號）、`qsets`（題組，`mode` 為 `assessment` / `practice`）、
`set_questions`（題組的題目與配分）、`templates`、`instances`、`check_runs`、`attempts`（限時作答）、
`activity`（出席依據）、`settings`。

## 多國語言

- 程式與樣板裡的**原文（繁體中文）就是翻譯的 key**：Python `_("檢查作答")`、樣板 `{{ _("檢查作答") }}`、JS `t("檢查作答")`
- 帶參數：`_("請等待 {n} 秒後再檢查", n=5)`，譯文保留 `{n}`
- 翻譯檔：`app/locales/<語言>/*.json`，內容 `{"原文": "譯文"}`；同一語言可分多個檔案
- 存進資料庫的訊息（檢查結果、工作狀態）存原文，**顯示時**才用 `translate_stored` 翻譯，
  所以不同語言的使用者看到各自的語言
- 語言由 cookie `lang`（右上角切換）或系統設定的預設語言決定

**新增一種語言**（例如日文）：

```bash
mkdir app/locales/ja
echo '{"_name": "日本語"}' > app/locales/ja/_meta.json
python tools/i18n_missing.py ja --json > app/locales/ja/ui.json   # 產生待翻譯清單，填入譯文
```

沒翻到的字串會顯示原文。改完介面文字後可以用 `python tools/i18n_missing.py en` 檢查有沒有漏翻。

題目內容的翻譯見 [QUESTIONS.md](QUESTIONS.md#多國語言)。

## 測試

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # 單元＋整合（mock 模式，不需要 PVE，約 30 秒）
```

- `tests/unit/`：設定、密碼、資料庫、多國語言（含翻譯完整性）、題目包、題組狀態、計分政策、出席
- `tests/integration/`：啟動 mock 模式的網站跑完整流程（後台、帳號、練習、測驗、一次一題、限時、出席、語言）
- `tests/e2e/`：對真實部署實際作答（需設定 `LSA_E2E_URL`、`LSA_E2E_ADMIN_PASSWORD`）

mock 模式下在終端機輸入 `mock-pass [檢查id ...]`，之後的檢查就會通過那些項目，可以手動測試計分流程。
GitHub Actions（`.github/workflows/ci.yml`）每次 push 會跑單元＋整合測試與 shellcheck。
詳見 [tests/README.md](../tests/README.md)。
