"""PVE API 包裝（同步；呼叫端用 asyncio.to_thread）。"""
from __future__ import annotations

import threading
import time

from .config import cfg


class PVEError(Exception):
    pass


class PVE:
    def __init__(self) -> None:
        from proxmoxer import ProxmoxAPI

        self.api = ProxmoxAPI(
            cfg.pve_host,
            port=cfg.pve_port,
            user=cfg.pve_user,
            token_name=cfg.pve_token_name,
            token_value=cfg.pve_token_value,
            verify_ssl=cfg.pve_verify_ssl,
            timeout=60,
        )
        self.node = self.api.nodes(cfg.pve_node)

    def _vm(self, kind: str, vmid: int):
        return getattr(self.node, kind)(vmid)

    def wait(self, upid: str, timeout: int = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.node.tasks(upid).status.get()
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    log = self.node.tasks(upid).log.get(limit=50)
                    raise PVEError(f"{st.get('exitstatus')}: " + " | ".join(l.get("t", "") for l in log[-5:]))
                return
            time.sleep(0.7)
        raise PVEError(f"PVE 任務逾時: {upid}")

    def _retry_locked(self, fn, tries: int = 20):
        """PVE 對同一範本同時 clone 會回 'locked'，重試即可。"""
        for i in range(tries):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if ("lock" in msg or "timeout" in msg) and i < tries - 1:
                    time.sleep(1 + i * 0.5)
                    continue
                raise

    # ---- 查詢 ----
    def resources(self) -> dict[int, dict]:
        """回傳 token 看得到的所有 VM/CT {vmid: {...status...}}"""
        out = {}
        for r in self.api.cluster.resources.get(type="vm"):
            out[int(r["vmid"])] = r
        return out

    def status(self, kind: str, vmid: int) -> str | None:
        try:
            return self._vm(kind, vmid).status.current.get()["status"]
        except Exception as e:  # noqa: BLE001
            if "does not exist" in str(e):
                return None
            # token 權限只涵蓋存在的 VM：不存在的 vmid 會回 403 而不是 does not exist
            if "403" in str(e) and vmid not in self.resources():
                return None
            raise

    def get_config(self, kind: str, vmid: int) -> dict:
        return self._vm(kind, vmid).config.get()

    # ---- 操作 ----
    def clone(self, kind: str, src: int, newid: int, name: str, description: str = "") -> None:
        args = {"newid": newid, "full": 0, "pool": cfg.pve_pool, "description": description}
        args["hostname" if kind == "lxc" else "name"] = name

        def go():
            upid = getattr(self.node, kind)(src).clone.post(**args)
            self.wait(upid)
        self._retry_locked(go)

    def full_clone(self, kind: str, src: int, newid: int, name: str, description: str = "") -> None:
        args = {"newid": newid, "full": 1, "pool": cfg.pve_pool, "storage": cfg.pve_storage,
                "description": description}
        args["hostname" if kind == "lxc" else "name"] = name

        def go():
            upid = getattr(self.node, kind)(src).clone.post(**args)
            self.wait(upid)
        self._retry_locked(go)

    def set_config(self, kind: str, vmid: int, **kw) -> None:
        self._retry_locked(lambda: self._vm(kind, vmid).config.put(**kw))

    def set_network(self, kind: str, vmid: int, ip: str, gateway: str | None = None,
                    nameserver: str | None = None) -> None:
        if kind == "lxc":
            net = f"name=eth0,bridge={cfg.bridge},ip={ip}/{cfg.net_prefix_len},firewall=0"
            if gateway:
                net += f",gw={gateway}"
            kw = {"net0": net}
            if nameserver:
                kw["nameserver"] = nameserver
            self.set_config(kind, vmid, **kw)
        else:
            ipc = f"ip={ip}/{cfg.net_prefix_len}" + (f",gw={gateway}" if gateway else "")
            kw = {"ipconfig0": ipc, "net0": f"virtio,bridge={cfg.bridge},firewall=0"}
            if nameserver:
                kw["nameserver"] = nameserver
            self.set_config(kind, vmid, **kw)

    def start(self, kind: str, vmid: int) -> None:
        if self.status(kind, vmid) == "running":
            return
        self.wait(self._retry_locked(lambda: self._vm(kind, vmid).status.start.post()))

    def stop(self, kind: str, vmid: int) -> None:
        if self.status(kind, vmid) != "running":
            return
        self.wait(self._retry_locked(lambda: self._vm(kind, vmid).status.stop.post()))

    def shutdown(self, kind: str, vmid: int, timeout: int = 60) -> None:
        if self.status(kind, vmid) != "running":
            return
        try:
            self.wait(self._vm(kind, vmid).status.shutdown.post(timeout=timeout), timeout + 30)
        except PVEError:
            self.stop(kind, vmid)

    def destroy(self, kind: str, vmid: int) -> None:
        st = self.status(kind, vmid)
        if st is None:
            return
        if st == "running":
            self.stop(kind, vmid)
        self.wait(self._retry_locked(lambda: self._vm(kind, vmid).delete(purge=1, **(
            {"destroy-unreferenced-disks": 1} if kind == "qemu" else {}))))

    def to_template(self, kind: str, vmid: int) -> None:
        r = self._vm(kind, vmid).template.post()
        if isinstance(r, str) and r.startswith("UPID"):
            self.wait(r)

    # ---- 主控台（不經過網路：LXC 是 tty、VM 是序列埠） ----
    def termproxy(self, kind: str, vmid: int) -> dict:
        """開一個主控台代理，回傳 websocket 網址、驗證標頭與登入訊息（見 app/console.py）"""
        from urllib.parse import urlencode
        r = self._vm(kind, vmid).termproxy.post()
        q = urlencode({"port": r["port"], "vncticket": r["ticket"]})
        return {
            "url": f"wss://{cfg.pve_host}:{cfg.pve_port}/api2/json/nodes/{cfg.pve_node}/{kind}/{vmid}/vncwebsocket?{q}",
            "headers": {"Authorization": f"PVEAPIToken={cfg.pve_user}!{cfg.pve_token_name}={cfg.pve_token_value}"},
            "hello": f"{r['user']}:{r['ticket']}\n".encode(),
            "verify_ssl": cfg.pve_verify_ssl,
        }


class MockPVE:
    """本機開發用：只記狀態，不真的建立容器。"""

    def __init__(self) -> None:
        self.vms: dict[int, dict] = {}
        self.lock = threading.Lock()

    def resources(self):
        return {k: {"vmid": k, "status": v["status"]} for k, v in self.vms.items()}

    def status(self, kind, vmid):
        v = self.vms.get(vmid)
        return v["status"] if v else None

    def get_config(self, kind, vmid):
        return {}

    def full_clone(self, kind, src, newid, name, description=""):
        self.clone(kind, src, newid, name, description)

    def clone(self, kind, src, newid, name, description=""):
        time.sleep(0.5)
        with self.lock:
            if newid in self.vms:
                raise PVEError(f"VM {newid} already exists")
            self.vms[newid] = {"status": "stopped"}

    full_clone = clone

    def set_config(self, kind, vmid, **kw):
        pass

    def set_network(self, *a, **kw):
        pass

    def start(self, kind, vmid):
        time.sleep(0.3)
        self.vms[vmid]["status"] = "running"

    def stop(self, kind, vmid):
        if vmid in self.vms:
            self.vms[vmid]["status"] = "stopped"

    def shutdown(self, kind, vmid, timeout=60):
        self.stop(kind, vmid)

    def destroy(self, kind, vmid):
        self.vms.pop(vmid, None)

    # mock 模式的「作答」：在 mock 終端機輸入 `mock-pass [檢查id ...]`，之後的檢查就會通過那些項目（不給 id = 全部）
    def mock_set_pass(self, vmid, ids):
        if vmid in self.vms:
            self.vms[vmid]["pass"] = set(ids) if ids else {"*"}

    def mock_results(self, vmid, check_ids):
        got = self.vms.get(vmid, {}).get("pass", set())
        return {c: ("*" in got or c in got) for c in check_ids}

    def to_template(self, kind, vmid):
        pass


_client: PVE | MockPVE | None = None


def client() -> PVE | MockPVE:
    global _client
    if _client is None:
        _client = MockPVE() if cfg.mock else PVE()
    return _client
