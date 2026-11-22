# 測試

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                 # 單元＋整合測試（mock 模式，不需要 PVE，約 30 秒）
```

| 目錄 | 內容 | 需要 |
|---|---|---|
| `tests/unit/` | 設定、密碼、資料庫結構、多國語言（含翻譯完整性）、題目包驗證與 zip、題組狀態、計分政策、出席 | 無 |
| `tests/integration/` | 啟動一個 mock 模式的網站，走完整流程：後台頁面（中英文）、帳號匯入與綁定、練習、檢查冷卻、一次一題的暫停、測驗鎖定練習與最終校驗、限時題與次數、出席總表、語言切換、題庫 | 無 |
| `tests/e2e/` | 連真實部署：範例題自我測試、透過終端機實際解題拿滿分、切題暫停後檔案保留 | 一套已安裝的系統 |

mock 模式下，在終端機輸入 `mock-pass [檢查id ...]` 會讓之後的檢查通過那些項目（不給 id = 全部），
整合測試用它模擬「作答」。

## 端對端測試（真實 PVE）

```bash
LSA_E2E_URL=http://<lsa-web IP> LSA_E2E_ADMIN_PASSWORD=<後台密碼> \
  .venv/bin/python -m pytest tests/e2e -v
```

- 會建立 `e2e` 開頭的題組與帳號，結束後刪除
- 範例題範本還沒建置時會先建置（約 2 分鐘）
- 全部約 3~6 分鐘

部署或更新程式後建議跑一次。
