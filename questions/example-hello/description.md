這是一個**範例題**，示範題目包怎麼寫。

這台主機有一個用 systemd 管理的小網站服務 `hello`，
應該要在 **8000 port** 顯示 `Hello Lab`，網頁內容放在 `/srv/hello/`。

但服務起不來，`curl http://localhost:8000/` 也連不上。請修好它：

1. 服務可以正常啟動，網頁顯示 `Hello Lab`
2. 開機會自動啟動
3. 重新啟動服務後仍然正常

提示：

- 問題不只一個，`systemctl status hello`、`journalctl -u hello` 會告訴你很多事情
- 服務設定檔在 `/etc/systemd/system/hello.service`，改完記得 `systemctl daemon-reload`
- 帳號 `user` 可使用 `sudo`
