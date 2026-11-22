# Linux Skill Assessment System (linux-skill-assessment)

A self-hosted Linux hands-on practice & assessment platform. Internally abbreviated **lsa**
(installed to `/opt/lsa`, service `lsa`, web container `lsa-web`).

**Run your own private "fix the broken Linux box" practice and assessment environment on Proxmox VE.**
Every person gets their **own** Linux environment (LXC container or VM) for every question.
Each question has problems planted in it; people log in through a **terminal in the browser**, fix it,
and press "Check answer" to be graded automatically by **Ansible**.

Similar in spirit to [SadServers](https://sadservers.com/), but entirely self-hosted: questions, accounts,
scores and session recordings stay on your own hardware. Open source (MIT), no cloud account needed.

> 中文版：[README.md](README.md). The user interface supports English and Traditional Chinese (more languages
> can be added), and questions can be translated too.

## Who it is for

- **University courses** — in-class practice (with attendance and practice scores) and lab exams, dozens of students at once
- **Technical interviews** — give candidates one or more hands-on troubleshooting tasks, time-limited and auto-graded,
  then review their full terminal session afterwards
- **Internal team training** — on-call onboarding and ops drills built from problems you actually see

You write your own question packages (YAML + Ansible). One example question is included.

## Features

- **One environment per person per question** — linked clones that are ready in seconds, network-isolated, no internet
- **Browser terminal** (xterm.js) — nothing to install; can pop out into its own window; optional paste blocking
- **Automatic grading** — several checks per question with partial credit, verified from outside the environment where possible
- **Question sets** for assessments and practice — each with its own questions, points, time window and scoring policy;
  practice sets pause automatically while an assessment is running
- **Per-question time limits, attempt limits, check cooldown**, and a post-assessment "final check" that grades the state at the end
- **Attendance and semester summary** for practice sessions
- **Pre-issued accounts** — generate and print accounts; users bind their ID on first login (or import a CSV with IDs)
- **Question packages** with one-click template build and **self-test**: unsolved must score 0, the reference solution must score full marks
- **Session recording** — every keystroke and all terminal output is kept for review
- **Multilingual** UI and questions; users switch language themselves

## Architecture

```
            browsers (participants / admins)
                   │ HTTP + WebSocket (terminal)
                   ▼
 ┌────────────────────────── Proxmox VE host ───────────────────────────┐
 │ vmbr0 (LAN)                                                          │
 │   │                                                                  │
 │ ┌─┴──────────────────────────────┐  PVE API (token, pool-scoped)     │
 │ │ lsa-web (LXC, VMID 100)       │ ──────────────────────────────▶   │
 │ │ FastAPI web app, terminal proxy│                                   │
 │ │ Ansible grading, SQLite        │                                   │
 │ └─┬──────────────────────────────┘                                   │
 │   │ SSH (grader key)                                                 │
 │ vmbr1 (isolated network 10.10.0.0/16: no internet, ports isolated)   │
 │   ├── user A / question 1 (LXC)   ├── user A / question 2 (VM) …     │
 │   └── user B / question 1 (LXC)   …                                  │
 │ question templates 9100+ (built from base templates 9000 / 9001)     │
 └──────────────────────────────────────────────────────────────────────┘
```

## Documentation

| Document | Contents |
|---|---|
| [docs/en/INSTALL.md](docs/en/INSTALL.md) | Hardware requirements, installing from scratch (PVE setup, templates, lsa-web), updates, backups |
| [docs/en/ADMIN.md](docs/en/ADMIN.md) | Administrator guide: accounts, question bank, question sets (assessment / practice), assessment-day workflow, scores and attendance |
| [docs/en/QUESTIONS.md](docs/en/QUESTIONS.md) | Writing questions: package format, step-by-step walkthrough of the example, translations |
| [docs/en/OPERATIONS.md](docs/en/OPERATIONS.md) | Capacity planning and load-test results, storage limits, troubleshooting |
| [docs/en/DEVELOPMENT.md](docs/en/DEVELOPMENT.md) | Local development (no PVE needed), code layout, translations |

## Quick start

Requires one machine running **Proxmox VE 8 or 9** (the default `local-lvm` storage is fine).

```bash
# 1. On the PVE host (as root): isolated network, API permissions, base templates, lsa-web container, config
git clone https://github.com/JalenLin/linux-skill-assessment.git /root/lsa-src
cd /root/lsa-src/deploy && TZ_NAME=Europe/Berlin ./pve-setup.sh all   # 20–40 min depending on network

# 2. On your workstation (or the PVE host): deploy the web app into lsa-web
echo 'PVE=root@<PVE IP>' > deploy/deploy.env
./deploy/deploy.sh

# 3. Open http://<lsa-web IP>/admin — user "admin", password in /root/lsa/admin_password on the PVE host
#    Switch the UI to English (top right), then Question bank → example-hello → "Build template + self-test"
```

Set the default UI language to English under System settings. Full steps: [docs/en/INSTALL.md](docs/en/INSTALL.md).

## Sizing (measured)

Intel N305 (8 cores), 48 GB RAM, NVMe, 60 concurrent users, 12 questions (9 LXC, 3 VM):

- LXC environment ≈ 50 MB RAM, VM environment ≈ 450 MB RAM
- With one running environment per user (the default), 60 users fit comfortably in 48 GB
- 60 VMs booting at once saturate the CPU for 2–3 minutes; terminals stay responsive (< 0.3 s) during mass grading
- Default LVM-thin metadata limits a host to roughly 1,100 instances — delete a set's instances when it's over

Details: [docs/en/OPERATIONS.md](docs/en/OPERATIONS.md).

## Writing questions

A question is a folder: `question.yaml` (title, checks, points), `description.md`, `setup.yml` (Ansible: install and break things),
`check.yml` (Ansible tasks named `check:<id>`), optional `solution.yml` and translations (`description.en.md`, an `i18n:` block).
Start by copying [`questions/example-hello`](questions/example-hello); see [docs/en/QUESTIONS.md](docs/en/QUESTIONS.md).

Keep your real questions (which contain solutions) in a separate, private repository and load them with
the admin upload or `deploy/push-questions.sh`.

## License

[MIT](LICENSE). Bundles [xterm.js](https://github.com/xtermjs/xterm.js) (MIT, see `app/static/vendor/LICENSE`).
