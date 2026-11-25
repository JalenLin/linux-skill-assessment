"""端對端測試：多節點範例題實際作答
- example-net：3 台 LXC（路由＋防火牆），檢查時才開機的 grader 節點
- example-k8s：3 台 VM 的 kubeadm 叢集（一組約需 5.5GB 記憶體）

環境變數同 test_real_pve.py。範本還沒建好時略過，設 LSA_E2E_BUILD_GROUP=1 才會先建置（各約 5~10 分鐘）。
"""
from __future__ import annotations

import os
import time

import pytest

from helpers import Admin, Participant

URL = os.environ.get("LSA_E2E_URL")
PW = os.environ.get("LSA_E2E_ADMIN_PASSWORD")
Q = "example-k8s"
QN = "example-net"
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not (URL and PW), reason="LSA_E2E_URL / LSA_E2E_ADMIN_PASSWORD 未設定")]
KUBECTL_READY = "until kubectl get --raw /readyz >/dev/null 2>&1; do sleep 3; done"


@pytest.fixture(scope="module")
def group():
    a = Admin(URL.rstrip("/"), PW)
    for qid in (QN, Q):
        j = a.get(f"/admin/questions/{qid}/status.json").json()
        ready = j["template"] and j["template"]["status"] == "ready" and j["selftest"] and j["selftest"]["ok"]
        if not ready:
            if not os.environ.get("LSA_E2E_BUILD_GROUP"):
                pytest.skip(f"{qid} 範本尚未建置（設 LSA_E2E_BUILD_GROUP=1 先建置）")
            j = a.build(qid, timeout=3600)
            assert j["template"]["status"] == "ready" and j["selftest"]["ok"], j
    tag = f"e2eg{int(time.time()) % 100000}"
    sid = a.create_set(tag, "practice")
    for qid in (QN, Q):
        a.add_question(sid, qid)
    u, pw = next(iter(a.generate(tag, 1).items()))
    p = Participant(a.base, u, pw)
    p.code = tag.upper()
    assert p.bind(p.code).status_code == 303
    yield {"admin": a, "p": p, "sid": sid}
    a.post(f"/admin/sets/{sid}/destroy_instances", data={"confirm": "DELETE"})
    for _ in range(120):
        if p.code not in a.get("/admin/instances").text:
            break
        time.sleep(2)
    a.post(f"/admin/sets/{sid}/delete", data={"confirm": tag})
    a.post(f"/admin/accounts/{a.account_id(u)}/delete")


def test_router_and_firewall_with_grader(group):
    """example-net：修好 r1 的轉送與 web 的防火牆；檢查由（只在檢查時開機的）grader 從 LAN 測"""
    p, sid = group["p"], group["sid"]
    assert p.api(sid, QN, "start")["ok"]
    assert p.check(sid, QN)["score"] == 0
    r1 = p.terminal(sid, QN, node="r1")
    try:
        r1.read(until="$ ", timeout=60)
        r1.run("echo net.ipv4.ip_forward=1 | sudo tee /etc/sysctl.d/60-router.conf && sudo sysctl --system >/dev/null")
    finally:
        r1.close()
    web = p.terminal(sid, QN, node="web")
    try:
        web.read(until="$ ", timeout=60)
        web.run("sudo sed -i 's#ip saddr 172.16.0.0/24 tcp dport 80#ip saddr { 172.16.0.0/24, 192.168.10.0/24 } tcp dport 80#'"
                " /etc/nftables.conf && sudo systemctl restart nftables")
    finally:
        web.close()
    pc1 = p.terminal(sid, QN, node="pc1")
    try:
        pc1.read(until="$ ", timeout=60)
        assert "Hello DMZ" in pc1.run("curl -s -m5 http://172.16.0.10/")
        assert "250" not in pc1.run("ip neigh")                  # grader 平常關機，LAN 上看不到它
    finally:
        pc1.close()
    r = p.check(sid, QN)
    assert r["status"] == "done" and r["score"] == 10, r


def test_fix_broken_worker(group):
    p, sid = group["p"], group["sid"]
    j = p.api(sid, Q, "start")
    assert j["ok"], j
    assert p.check(sid, Q)["score"] == 0

    cp1 = p.terminal(sid, Q, node="cp1")
    try:
        cp1.read(until="$ ", timeout=60)
        cp1.run(KUBECTL_READY, timeout=300)
        assert "NotReady" in cp1.run("kubectl get node w2", timeout=30)
        assert " 0% packet loss" in cp1.run("ping -c1 -W2 192.168.50.22", timeout=10)   # 題目網路互通

        w2 = p.terminal(sid, Q, node="w2")                 # 另一個分頁，cp1 的連線不受影響
        try:
            w2.read(until="$ ", timeout=60)
            assert "w2" in w2.run("hostname")
            w2.run("sudo systemctl enable --now kubelet", timeout=60)
        finally:
            w2.close()
        cp1.run("kubectl wait --for=condition=Ready node/w2 --timeout=300s", timeout=320)
        cp1.run("kubectl scale deployment web --replicas=3 && kubectl rollout status deployment/web --timeout=300s",
                timeout=320)
    finally:
        cp1.close()
    r = p.check(sid, Q)
    assert r["status"] == "done" and r["score"] == 10, r
