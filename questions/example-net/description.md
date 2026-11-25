This is an **example multi-container question**: two networks joined by a router.

```
pc1 ──(lan 192.168.10.0/24)── r1 ──(dmz 172.16.0.0/24)── web
```

| Machine | eth1 | eth2 |
|---|---|---|
| `pc1` | 192.168.10.10 (lan) | |
| `r1` | 192.168.10.1 (lan) | 172.16.0.1 (dmz) |
| `web` | 172.16.0.10 (dmz) | |

Use the tabs above the terminal to switch machines. **Don't change eth0** — it is the management network.

On `pc1`, `curl http://172.16.0.10/` should show `Hello DMZ`, but it doesn't. Fix it so that:

1. `r1` routes between the two networks, and still does after a reboot
2. `pc1` can open the page on `web`, with the firewall on `web` still enabled — allow what is needed, don't turn it off

Checking your answer **reboots r1**, and tests the web page from another client on the LAN.

Hints: `ip route`, `sysctl`, `nft list ruleset`, `tcpdump` (on r1). The account `user` can use `sudo`.
