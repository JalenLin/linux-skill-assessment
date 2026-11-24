# 出題指南

> English: [en/QUESTIONS.md](en/QUESTIONS.md)

一題是一個資料夾（**題目包**），用 YAML 描述題目、用 Ansible playbook 建置環境與評分。
最快的方式是複製範例題 [`questions/example-hello`](../questions/example-hello) 再修改。

- [題目包的結構](#題目包的結構)
- [範例題逐步說明](#範例題逐步說明)
- [question.yaml](#questionyaml)
- [setup.yml：建置範本、埋錯](#setupyml建置範本埋錯)
- [check.yml：評分](#checkyml評分)
- [solution.yml：標準解答與自我測試](#solutionyml標準解答與自我測試)
- [LXC 還是 VM？](#lxc-還是-vm)
- [多國語言](#多國語言)
- [開發與測試流程](#開發與測試流程)
- [出題建議](#出題建議)

---

## 題目包的結構

```
example-hello/            資料夾名稱 = 題目 id（小寫英數與 -）
├── question.yaml         必要：標題、類型、資源、檢查項目與配分
├── description.md        必要：作答者看到的題目敘述（Markdown，主要語言）
├── description.zh-TW.md  選用：其他語言的題目敘述（description.<語言代碼>.md）
├── setup.yml             必要：建置範本時執行，安裝套件並「埋錯」
├── check.yml             必要：評分
├── solution.yml          選用（強烈建議）：標準解答，自我測試用
└── files/                選用：setup.yml 用到的檔案
```

作答者只看得到題目敘述；`setup.yml`、`check.yml`、`solution.yml` 都不會給作答者。

## 範例題逐步說明

`example-hello` 是一個用 systemd 管理的小網站 `hello`（`python3 -m http.server`，port 8000），
被埋了三個錯：

| 錯誤 | 埋在哪裡 | 作答者怎麼發現 |
|---|---|---|
| ExecStart 寫 `/usr/bin/python`（Ubuntu 24.04 只有 python3） | `files/hello.service` | `systemctl status hello` |
| 網頁檔權限 0600，服務以 `nobody` 執行讀不到 | `setup.yml` 的 copy `mode` | 修好第一個後 curl 得到 404 |
| 沒有設定開機啟動 | `setup.yml` 的 `enabled: false` | `systemctl is-enabled hello` |

對應三個檢查項目（`question.yaml` 的 `checks`）：

| id | 配分 | 怎麼檢查（`check.yml`） |
|---|---|---|
| `page` | 6 | **從評分主機**用 HTTP 連 `http://<作答者環境>:8000/`，內容要有 `Hello Lab` |
| `boot` | 2 | 在作答者環境執行 `systemctl is-enabled hello` |
| `restart` | 2 | 重新啟動服務，等 port 開好，再從評分主機連一次 |

`solution.yml` 修正這三個錯。建置時的自我測試結果：未作答 **0/10**、套用解答 **10/10**。

## question.yaml

```yaml
title: The hello service won't start (example)
tags: [systemd, web, example]
type: lxc            # lxc（容器，預設）或 vm
hostname: hello      # 作答者看到的主機名稱
resources:
  cores: 1
  memory: 512        # MB；vm 建議 1024
lang: en             # 選用：主要內容的語言，預設 zh-TW
checks:              # 檢查項目；配分在各題組可以另外調整，這裡是預設值
  - id: page
    desc: "http://<host>:8000/ can be browsed from another computer and shows Hello Lab"
    points: 6
  - id: boot
    desc: The hello service is enabled to start at boot
    points: 2
  - id: restart
    desc: The page still works after restarting the hello service
    points: 2
i18n:                # 選用：其他語言（見「多國語言」）
  zh-TW:
    title: Hello 服務無法啟動（範例題）
    checks:
      page: 從其他電腦瀏覽 http://<本機>:8000/ 可看到 Hello Lab
      boot: hello 服務設為開機自動啟動
      restart: 重新啟動 hello 服務後網頁仍正常
```

範例題以英文為主要語言、附繁體中文翻譯；你的題目用中文為主的話，`lang` 不寫（預設 `zh-TW`），
`title`、`desc` 寫中文，翻譯放在 `i18n: en:`。

- `tags`（選用）：分類標籤，只給管理者在題庫頁、題組頁篩選用，作答者看不到；也可以在後台題庫頁編輯（會改寫這個檔案）
- `checks` 的 `desc` 是作答者看到的檢查項目說明（題組設定「作答者可看到每個檢查項目是否通過」時）
- VM 題可以加額外磁碟：`resources` 底下加 `extra_disks: [2]`（單位 GB，依序成為 `/dev/sdb`、`/dev/sdc`…）
- `terminal`（選用）：作答者的終端機怎麼連到環境

  | 值 | 意思 |
  |---|---|
  | `ssh`（預設） | 用 SSH；可以按「改用主控台」，SSH 連不上時也會自動改用主控台 |
  | `console` | 一開始就用 PVE 主控台（不經過網路，以 `user`／`user` 登入）；可以按「改用 SSH」 |
  | `console-only` | 只能用主控台，沒有「改用 SSH」（例如模擬「只能接 console」的機房情境） |
  | `ssh-only` | 只能用 SSH；連不上也不改用主控台（作答者改壞了就只能重置） |

  題目本身就會讓 SSH 不通時（例如「修好 sshd」「網路設定錯誤」「防火牆擋住所有連線」）用 `console` 或 `console-only`。
  限制是在伺服器端強制的，作答者改網址也換不了。
  - **檢查仍然走 SSH**：作答者沒修好之前檢查會是 0 分（「無法連線到環境」），這也是這類題目該有的結果
  - **初始狀態 SSH 不通時，自我測試沒辦法自動套用 solution.yml**：未修正狀態直接以 0 分計，後台顯示「部分通過」；解答請用測試帳號在主控台修好、按檢查確認滿分（出題或改題後做一次即可）
  - 不要讓題目改掉 eth0 的 IP：平台依 VMID 分配 IP，檢查和 SSH 都靠它

## setup.yml：建置範本、埋錯

```yaml
- hosts: target
  gather_facts: false
  tasks:
    - name: Web directory
      ansible.builtin.file: {path: /srv/hello, state: directory, mode: "0755"}
    - name: Web page
      ansible.builtin.copy:
        dest: /srv/hello/index.html
        content: "Hello Lab\n"
        mode: "0600"           # 錯誤 2
    - name: Service unit (contains fault 1)
      ansible.builtin.copy: {src: files/hello.service, dest: /etc/systemd/system/hello.service}
    - name: "Fault 3: not enabled at boot"
      ansible.builtin.systemd_service: {name: hello, daemon_reload: true, enabled: false}
```

- 從 base 範本（Ubuntu 24.04，已有 `user`/`user` 可 sudo、openssh、python3、curl、cron）**完整複製**一份後以 root 執行
- 建置時**可以上網**（apt、docker pull 都可以）；**測驗時作答者環境不能上網**，用到的套件、image 都要在這裡先裝好
- `files/` 裡的檔案用 `src: files/xxx` 引用
- 結束後自動關機並轉成範本；作答者的環境從範本 linked clone，開得很快
- 修改 `setup.yml` 或 `files/` 後，後台會標示「需要重新建置」

## check.yml：評分

```yaml
- hosts: target
  gather_facts: false
  ignore_errors: true          # 必要：某項失敗後繼續檢查其他項
  tasks:
    - name: "check:page browse the page from the grading host"
      ansible.builtin.uri:
        url: "http://{{ target_ip }}:8000/"
        return_content: true
        timeout: 5
      register: page
      failed_when: page.status != 200 or 'Hello Lab' not in page.content
      delegate_to: localhost   # 在評分主機執行，作答者改不到

    - name: "check:boot enabled at boot"
      ansible.builtin.command: systemctl is-enabled hello
      changed_when: false
```

規則：

- task 名稱以 **`check:<id>`** 開頭就對應到 question.yaml 的檢查項目，後面可以接說明
- 同一個 id 可以有多個 task，**全部成功**才得分；被 skip 也算失敗
- 變數 `target_ip` 是作答者環境的 IP
- 作答者按「檢查作答」和測驗結束後「最終校驗」都是跑這份
- 逾時預設 180 秒（`check_timeout`）；整台重開機的檢查請把 `reboot_timeout` 設寬一點（主機忙時 VM 開機會變慢）
- 作答者如果把 SSH 弄壞，會顯示「評分程式無法登入環境」並以 0 分計（系統會先自動重試一次），作答者可自行重置

**重要原則：**

- **盡量檢查結果，不要檢查設定文字**：作答者的修法不只一種，網站打得開才是重點
- **能從外部驗證就用 `delegate_to: localhost`**：作答者在環境裡有 root，可以把 `curl`、`systemctl` 換成假的
- 「重新啟動後仍正常」：在 check 裡實際重啟服務再驗證一次（範例題的 `restart`）
- 「開機後仍正常」：可以用 `ansible.builtin.reboot`（VM 題；會讓作答者的終端機斷線一下）

## solution.yml：標準解答與自我測試

```yaml
- hosts: target
  gather_facts: false
  tasks:
    - ansible.builtin.replace:
        path: /etc/systemd/system/hello.service
        regexp: '/usr/bin/python '
        replace: '/usr/bin/python3 '
    - ansible.builtin.file: {path: /srv/hello/index.html, mode: "0644"}
    - ansible.builtin.systemd_service: {name: hello, daemon_reload: true, enabled: true, state: restarted}
```

後台「建置範本＋自我測試」會：

1. 從範本開一個測試實例跑 check.yml，**應為 0 分**（不是 0 分代表有檢查項目在未修正時就會通過）
2. 套用 solution.yml 再跑 check.yml，**應為滿分**

兩個都符合才顯示「自我測試通過」，可以在測驗前抓出 check.yml 寫錯的地方。
（`terminal: console`／`console-only` 且初始狀態 SSH 不通的題目只做第 1 步，見上面 `terminal` 的說明。）
也可以只按「只跑自我測試」（不重建範本）。

## LXC 還是 VM？

| | LXC（`type: lxc`） | VM（`type: vm`） |
|---|---|---|
| 記憶體 | 約 50MB | 約 450MB |
| 第一次開好 | 10~30 秒 | 約 1 分鐘 |
| 適合 | 服務設定、權限、使用者、cron、網頁伺服器、資料庫、Docker | 需要 kernel 功能：掛載磁碟（fstab）、NFS server、核心模組、開機流程、防火牆 |

**能用 LXC 就用 LXC。** VM 題多時記憶體與 CPU 消耗會大很多（見 [OPERATIONS.md](OPERATIONS.md)）。
LXC 容器是 unprivileged + nesting，可以跑 Docker。

## 多國語言

作答者介面可切換語言，題目可以另外提供翻譯（選用）：

- `question.yaml` 的 `lang:`：主要內容（`title`、`description.md`、`checks` 的 `desc`）的語言，預設 `zh-TW`
- `question.yaml` 加 `i18n:` 區塊，依語言代碼提供標題與各檢查項目的說明；`checks` 底下的 key 必須是 `checks:` 裡已有的 `id`
- 題目敘述另存成 `description.<語言代碼>.md`（指令、路徑、code block 保持原樣）
- 語言代碼要和介面語言代碼一致：`zh-TW`、`en`；`zh_TW`（Linux locale 寫法）、大小寫不同也可以，會自動視為 `zh-TW`，但只寫 `zh` 不會被採用
- 缺少的翻譯會自動改用主要內容顯示
- 修改翻譯不需要重新建置範本

範例題是**英文為主、附中文**：`lang: en`，中文放在 `i18n: zh-TW:`（見上方 question.yaml）與 `description.zh-TW.md`。

**中文為主、附英文**則反過來：`lang` 不寫，`title`／`desc`／`description.md` 寫中文，英文放在 `i18n: en:` 與 `description.en.md`：

```yaml
title: Hello 服務無法啟動
checks:
  - id: page
    desc: 從其他電腦瀏覽 http://<本機>:8000/ 可看到 Hello Lab
    points: 6
i18n:
  en:
    title: The hello service won't start
    checks:
      page: "http://<host>:8000/ can be browsed from another computer and shows Hello Lab"
```

## 開發與測試流程

1. 複製 `questions/example-hello` 成新資料夾，改名（＝題目 id）
2. 改 `question.yaml`、`description.md`、`setup.yml`、`check.yml`、`solution.yml`
3. 放進系統：後台「題庫」上傳 zip，或 `./deploy/push-questions.sh <題庫目錄> <題目id>`
4. 「建置範本＋自我測試」，看建置紀錄修正錯誤，直到自我測試通過
5. 建一個練習題組放這題，用測試帳號實際做一次（確認題目敘述清楚、提示足夠）

除錯技巧：

- 建置失敗時，建置紀錄會顯示 Ansible 的錯誤訊息與出錯的 task
- 想進去看範本長怎樣：用測試帳號開題，在終端機裡看
- 在 lsa-web 上也可以用指令建置：
  ```bash
  pct exec 100 -- bash -c 'cd /opt/lsa && sudo -u lsa LSA_CONFIG=/etc/lsa/config.yaml .venv/bin/python -m app.build <題目id>'
  ```

## 出題建議

- 一題 2~4 個檢查項目，主要功能配分高、「持久化」（重開後仍正常、開機自動啟動）配分低
- 題目敘述講清楚**目標**（要達成什麼），不要講**做法**；附上作答者可以自己驗證的方法（例如 `curl`）
- 埋的錯要能從系統訊息找到線索（`systemctl status`、`journalctl`、錯誤訊息），不要考猜謎
- 考慮作答者的各種合理修法，評分只看結果
- 題目資料夾含解答，**另外用 private repo 管理**，不要放在作答者看得到的地方
