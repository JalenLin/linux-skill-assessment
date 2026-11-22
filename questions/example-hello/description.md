This is an **example question** that shows how to write a question package.

This host has a small web service called `hello`, managed by systemd.
It should show `Hello Lab` on **port 8000**; the page lives in `/srv/hello/`.

But the service won't start and `curl http://localhost:8000/` can't connect. Fix it so that:

1. The service starts and the page shows `Hello Lab`
2. It starts automatically at boot
3. It still works after the service is restarted

Hints:

- There is more than one problem; `systemctl status hello` and `journalctl -u hello` tell you a lot
- The unit file is `/etc/systemd/system/hello.service`; run `systemctl daemon-reload` after editing it
- The account `user` can use `sudo`
