> 中文版：[../INSTALL.md](../INSTALL.md)

# Installation and Deployment

This guide takes you from an empty Proxmox VE host to participants being able to log in to an assessment.

- [1. Hardware and software requirements](#1-hardware-and-software-requirements)
- [2. Install Proxmox VE](#2-install-proxmox-ve)
- [3. Set up the environment on the PVE host (pve-setup.sh)](#3-set-up-the-environment-on-the-pve-host-pve-setupsh)
- [4. Deploy the web application (deploy.sh)](#4-deploy-the-web-application-deploysh)
- [5. First login and building the example question](#5-first-login-and-building-the-example-question)
- [6. Add your questions](#6-add-your-questions)
- [7. Network setup before an assessment](#7-network-setup-before-an-assessment)
- [8. Updating](#8-updating)
- [9. Backup and restore](#9-backup-and-restore)
- [10. Security notes](#10-security-notes)

---

## 1. Hardware and software requirements

| Item | Requirement |
|---|---|
| Host | One physical machine running **Proxmox VE 8 or 9** |
| CPU | Virtualization support (VT-x/AMD-V); the more VM questions, the more CPU you need |
| Memory | Depends on the number of participants and question types; see below |
| Disk | SSD, a few hundred GB or more; the default `local-lvm` (LVM-thin) is fine |
| Network | A network segment reachable from participants' computers (vmbr0) |

**Measured reference** (Intel N305 8 cores, 48GB, NVMe, 60 participants; see [OPERATIONS.md](OPERATIONS.md) for details):

- Each participant runs only 1 question at a time (the default): with 60 participants working at once, the worst case is about 31GB, so **48GB is enough**
- Each LXC environment uses about 50MB of memory, each VM environment about 450MB
- When 60 VMs boot at the same time, the CPU is saturated for 2–3 minutes; during normal work and checking, CPU load is light
- Rough memory estimate: `4GB (system + web) + participants × 0.45GB` (assuming everyone is working on a VM question)

> ⚠ **Instance count limit**: the default `local-lvm` holds only about **1100 instances** (LVM metadata is fixed when PVE
> is installed and cannot be enlarged). Beyond that, creating environments fails no matter how much disk space is free.
> Count it as "participants × questions"; instances of practice sets keep accumulating, so delete a set's instances once
> it is over. See [OPERATIONS.md](OPERATIONS.md#storage-lvm-limits).

## 2. Install Proxmox VE

1. Install PVE following the official instructions, using the default LVM-thin (`local-lvm`)
   - If you expect more than about 1000 instances to exist at once (e.g. 100 participants × 12 questions), choose **ZFS**
     during installation, or prepare a separate disk as storage with a large metadata area (see
     [OPERATIONS.md](OPERATIONS.md#storage-lvm-limits)); this cannot be changed after installation
2. During installation, set the IP of vmbr0 (on the network segment participants can reach)
3. After installation, update packages: `apt update && apt full-upgrade`

> The host name (e.g. `pve`) becomes `pve_node` in the configuration file later; `pve-setup.sh config` fills it in automatically.

## 3. Set up the environment on the PVE host (pve-setup.sh)

Put this repo on the PVE host (`apt install git` and clone it, or `scp -r` it from another computer), then run as root:

```bash
cd /root/lsa-src/deploy
./pve-setup.sh all
```

`all` runs the following steps in order. Each step can be run on its own and re-run (completed work is skipped):

| Step | What it does | Time |
|---|---|---|
| `network` | Creates the internal assessment network **vmbr1** (10.10.0.0/16, VLAN-aware for multi-node question networks) and firewall rules; installs the hookscript (port isolation between participants) | Seconds |
| `perms` | Creates the pool `lsa`, the API user `lsa@pve` and a token (saved to `/root/lsa/token.json`); permissions are limited to the pool `lsa` | Seconds |
| `base` | Downloads the Ubuntu 24.04 LXC image and creates the **LXC base template 9000** (account `user`/`user` with sudo, openssh, python3, grader key) | 5–10 minutes |
| `basevm` | Downloads the Ubuntu 24.04 cloud image and creates the **VM base template 9001** (for questions that need kernel features) | 5–15 minutes |
| `web` | Creates the web container **lsa-web (VMID 100)**, attached to vmbr0 and vmbr1 (10.10.0.2) | 1–2 minutes |
| `config` | Generates the configuration file `/root/lsa/config.yaml` (random admin password, PVE token) and puts it into lsa-web | Seconds |

You can adjust it with environment variables:

```bash
WEB_NET0="ip=192.168.1.20/24,gw=192.168.1.1" ./pve-setup.sh web   # static IP for lsa-web (default: DHCP)
STORAGE=local-lvm ./pve-setup.sh all                              # storage for templates and participant environments
TZ_NAME=Asia/Taipei ./pve-setup.sh all                            # time zone (default Asia/Taipei); also written to timezone in the config file
WEB_VMID=100 BASE_VMID=9000 BASEVM_VMID=9001                      # VMIDs
```

When it finishes, `/root/lsa/` on the PVE host contains these files (**all secret — do not leak them**):

| File | Purpose |
|---|---|
| `token.json` | PVE API token (generated only once) |
| `grader_key`, `grader_key.pub` | SSH key the grader uses to log in to participant environments (the public key is already in the base templates) |
| `config.yaml` | Configuration file for lsa-web (also at `/etc/lsa/config.yaml` in lsa-web) |
| `admin_password` | Admin console password |

See [`config.example.yaml`](../../config.example.yaml) for the configuration fields; the full set of settings and defaults is in `app/config.py`.

### VMID and IP allocation

| Range | Purpose |
|---|---|
| 100 | lsa-web |
| 9000 / 9001 | LXC / VM base templates |
| 9100–9999 | Per-question templates (built on 10.10.255.0/24, with Internet access) |
| 10000–19999 | Participant environments (IP determined by VMID: starting at 10.10.1.2) |

## 4. Deploy the web application (deploy.sh)

`deploy.sh` packages the application, puts it into lsa-web via the PVE host, and then runs
`deploy/web/install.sh` inside lsa-web: it creates a Python venv, installs packages (including Ansible), and sets up the systemd service and firewall.

On your development machine (a computer that can `ssh root@<PVE>`):

```bash
echo 'PVE=root@192.168.1.10' > deploy/deploy.env    # PVE host; this file is not tracked by git
./deploy/deploy.sh
```

You can also run it directly on the PVE host (root must be able to ssh to itself, e.g. `PVE=root@127.0.0.1`).

After deployment:

- The application is at `/opt/lsa` in lsa-web; the service is named `lsa` (port 80)
- Data is in `/var/lib/lsa/data` (`lsa.db`, terminal recordings in `shell_logs/a<account id>/*.cast`)
- The question bank is in `/var/lib/lsa/questions` (**deployments never overwrite it**; the first install only adds the example questions)
- View logs: `pct exec 100 -- journalctl -u lsa -f`

## 5. First login and building the example question

1. Find the IP of lsa-web: `pct exec 100 -- ip -br -4 addr show eth0`
2. Open `http://<lsa-web IP>/admin` in a browser; the username is `admin`, and the password is in `/root/lsa/admin_password` on the PVE host
3. "System settings": change the site title and confirm the ID format
4. "Question bank" → `example-hello` → "**Build template + self-test**". It takes about 1–2 minutes and should show
   unsolved 0/10, solved 10/10 after applying the solution, and the self-test passed
5. "Question set" → create a practice question set, add example-hello, and check "Visible to participants"
6. "Account" → generate a few test accounts, and log in as a participant in another browser (or a private window) to try it out

## 6. Add your questions

Questions are not in this repo; they are managed separately. There are two ways to put them into the system:

- **Upload in the admin console**: "Question bank" → upload a zip (one question per zip; the zip contains the question folder)
- **Sync with a command**: `./deploy/push-questions.sh <question bank directory> [question id ...]`; questions with the same name are replaced

After adding them, press "Build template + self-test" on the "Question bank" page. If you later change `setup.yml` or `files/`,
the question bank page marks the question as needing a rebuild; changes to only `description`, `check.yml` or translations do not require a rebuild.

See [QUESTIONS.md](QUESTIONS.md) for how to write questions.

## 7. Network setup before an assessment

- **Give lsa-web a static IP** (if DHCP changes its IP, participants can no longer connect):
  `pct set 100 --net0 name=eth0,bridge=vmbr0,ip=192.168.1.20/24,gw=192.168.1.1 && pct reboot 100`
  (leave eth1 on vmbr1 alone)
- If you need HTTPS or a domain name, put a reverse proxy (nginx, Caddy, etc.) in front, and **make sure WebSocket (`/ws/`) is forwarded too**;
  the service already runs with `--proxy-headers`
- **Change the admin password**: edit `admin_password` in `/root/lsa/config.yaml` on the PVE host,
  run `./pve-setup.sh config` to put it into lsa-web, then run `pct exec 100 -- systemctl restart lsa`
- To allow only the assessment venue's network segment to reach lsa-web, you can restrict access in the PVE firewall or on the campus network equipment

## 8. Updating

```bash
git pull
./deploy/deploy.sh
```

- When the database schema changes, it is upgraded automatically on startup — no manual steps needed; to be safe, back up first (next section)
- When the service restarts, open terminals disconnect and reconnect automatically; **do not update while an assessment is in progress**

## 9. Backup and restore

What to back up:

| Location | Contents |
|---|---|
| lsa-web `/var/lib/lsa/data/lsa.db` | Accounts, question sets, scores, attendance, answer records (SQLite) |
| lsa-web `/var/lib/lsa/data/shell_logs/` | Terminal logs |
| lsa-web `/var/lib/lsa/questions/` | Question bank (can be skipped if managed in a separate repo) |
| PVE `/root/lsa/` | Token, keys, configuration file |

```bash
# On the PVE host: online database backup (works while the service is running)
pct exec 100 -- sqlite3 /var/lib/lsa/data/lsa.db ".backup /var/lib/lsa/data/lsa-$(date +%F).db"
pct pull 100 /var/lib/lsa/data/lsa-$(date +%F).db /root/lsa-backup-$(date +%F).db
```

Or back up the whole lsa-web container (VMID 100) with PVE's vzdump.
Participant environments and question templates do not need to be backed up (templates can be rebuilt from the questions).

To restore: stop the service, put `lsa.db` back, change its owner back to `lsa`, and start the service.

## 10. Security notes

- Participant environments are only on vmbr1: they cannot reach the Internet, cannot reach the PVE host, and cannot reach each other (the hookscript sets bridge port isolation)
- The machines of one multi-node group also share question networks (eth1…), one VLAN per group; groups cannot see each other
- lsa-web's vmbr1 interface only allows outbound connections (participant environments cannot reach any service on the web server)
- The PVE API token only has permissions on the pool `lsa` and the designated storage; it cannot touch other VMs
- Participants have sudo in their own environments; grading verifies from the outside, from lsa-web, as much as possible (see QUESTIONS.md)
- Secret files: `/root/lsa/*` (PVE), `/etc/lsa/*` (lsa-web); do not put them in git
