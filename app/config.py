"""設定載入：預設值 <- config.yaml (LSA_CONFIG 指定路徑)。"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # --- 網站 ---
    secret_key: str = "change-me"
    admin_user: str = "admin"
    admin_password: str = "admin"
    data_dir: str = str(ROOT / "data")
    questions_dir: str = str(ROOT / "questions")
    timezone: str = "Asia/Taipei"
    mock: bool = False  # True: 不連 PVE，僅供本機看畫面

    # --- PVE API ---
    pve_host: str = ""
    pve_port: int = 8006
    pve_user: str = "lsa@pve"
    pve_token_name: str = "web"
    pve_token_value: str = ""
    pve_verify_ssl: bool = False
    pve_node: str = "pve"
    pve_pool: str = "lsa"
    pve_storage: str = "local-lvm"

    # --- VMID / 網路 ---
    base_template_vmid: int = 9000       # LXC base 範本
    base_vm_template_vmid: int = 9001    # QEMU cloud-init base 範本（VM 題用）
    template_vmid_min: int = 9100        # 題目範本
    template_vmid_max: int = 9999
    vmid_min: int = 10000                # 作答者實例
    vmid_max: int = 19999
    bridge: str = "vmbr1"
    net_prefix: str = "10.10"            # 作答者實例 10.10.{1..40}.{2..251}/16
    net_prefix_len: int = 16
    build_gateway: str = "10.10.0.1"     # 只有建置範圍 10.10.255.0/24 會被 NAT
    build_dns: str = "1.1.1.1"
    # 多節點題的題目網路：vmbr1 開 VLAN-aware，每組每個網路一個 VLAN（eth1…）；eth0 仍是管理網路
    vlan_min: int = 100                  # 作答者環境組
    vlan_max: int = 3999
    build_vlan: int = 4090               # 建置範本與自我測試用 4090～4093（一次只建一題）

    # --- SSH / 校驗 ---
    ssh_key: str = "/etc/lsa/grader_key"
    ssh_user: str = "user"
    grader_concurrency: int = 8
    check_timeout: int = 180
    clone_concurrency: int = 4           # 全部範本合計同時 clone 數

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def questions_path(self) -> Path:
        return Path(self.questions_dir)


def load() -> Config:
    path = Path(os.environ.get("LSA_CONFIG", ROOT / "config.yaml"))
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise SystemExit(f"config.yaml 有未知的設定: {sorted(unknown)}")
    cfg = Config(**raw)
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    cfg.questions_path.mkdir(parents=True, exist_ok=True)
    if cfg.timezone:   # 成績、開放時間都用這個時區（設定檔 timezone）
        os.environ["TZ"] = cfg.timezone
        import time
        time.tzset()
    return cfg


cfg = load()


def instance_ip(vmid: int) -> str:
    idx = vmid - cfg.vmid_min
    return f"{cfg.net_prefix}.{1 + idx // 250}.{2 + idx % 250}"


def build_ip(vmid: int) -> str:
    return f"{cfg.net_prefix}.255.{2 + (vmid - cfg.template_vmid_min) % 240}"
