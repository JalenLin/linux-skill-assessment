> 中文版：[../QUESTIONS.md](../QUESTIONS.md)

# Question Authoring Guide

Each question is a folder (a **question package**): YAML describes the question, and Ansible playbooks build the environment and do the grading.
The fastest way to start is to copy the example question [`questions/example-hello`](../../questions/example-hello) and modify it.

- [Question package structure](#question-package-structure)
- [Walkthrough of the example question](#walkthrough-of-the-example-question)
- [question.yaml](#questionyaml)
- [setup.yml: building the template and planting faults](#setupyml-building-the-template-and-planting-faults)
- [check.yml: grading](#checkyml-grading)
- [solution.yml: reference solution and self-test](#solutionyml-reference-solution-and-self-test)
- [LXC or VM?](#lxc-or-vm)
- [Multi-node questions](#multi-node-questions)
- [Multiple languages](#multiple-languages)
- [Development and testing workflow](#development-and-testing-workflow)
- [Tips for writing questions](#tips-for-writing-questions)

---

## Question package structure

```
example-hello/            folder name = question id (lowercase letters, digits and -)
├── question.yaml         required: title, type, resources, check items and points
├── description.md        required: the question description participants see (Markdown, main language)
├── description.zh-TW.md  optional: the description in another language (description.<code>.md)
├── setup.yml             required: runs when building the template; installs packages and "plants faults"
├── check.yml             required: grading
├── solution.yml          optional (strongly recommended): reference solution, used by the self-test
└── files/                optional: files used by setup.yml
```

Participants only see the question description; `setup.yml`, `check.yml` and `solution.yml` are never given to participants.

## Walkthrough of the example question

`example-hello` is a small website `hello` managed by systemd (`python3 -m http.server`, port 8000),
with three planted faults:

| Fault | Where it is planted | How participants discover it |
|---|---|---|
| ExecStart uses `/usr/bin/python` (Ubuntu 24.04 only has python3) | `files/hello.service` | `systemctl status hello` |
| The web page file has mode 0600, so the service, running as `nobody`, can't read it | the copy `mode` in `setup.yml` | after fixing the first one, curl returns 404 |
| The service is not enabled at boot | `enabled: false` in `setup.yml` | `systemctl is-enabled hello` |

These map to three check items (`checks` in `question.yaml`):

| id | Points | How it is checked (`check.yml`) |
|---|---|---|
| `page` | 6 | **From the grading host**, request `http://<participant environment>:8000/` over HTTP; the content must contain `Hello Lab` |
| `boot` | 2 | Run `systemctl is-enabled hello` in the participant environment |
| `restart` | 2 | Restart the service, wait for the port to open, then request the page from the grading host again |

`solution.yml` fixes all three faults. Self-test results at build time: unanswered **0/10**, with the solution applied **10/10**.

## question.yaml

```yaml
title: The hello service won't start (example)
tags: [systemd, web, example]
type: lxc            # lxc (container, default) or vm
hostname: hello      # host name participants see
resources:
  cores: 1
  memory: 512        # MB; 1024 recommended for vm
lang: en             # optional: language of the main content, default zh-TW
checks:              # check items; points can be adjusted per question set, these are the defaults
  - id: page
    desc: "http://<host>:8000/ can be browsed from another computer and shows Hello Lab"
    points: 6
  - id: boot
    desc: The hello service is enabled to start at boot
    points: 2
  - id: restart
    desc: The page still works after restarting the hello service
    points: 2
i18n:                # optional: other languages (see "Multiple languages")
  zh-TW:
    title: Hello 服務無法啟動（範例題）
    checks:
      page: 從其他電腦瀏覽 http://<本機>:8000/ 可看到 Hello Lab
      boot: hello 服務設為開機自動啟動
      restart: 重新啟動 hello 服務後網頁仍正常
```

(The example's main language is English with a Traditional Chinese translation in `i18n.zh-TW`.)

- `tags` (optional): category tags, used only by administrators to filter on the question bank and question set pages; participants never see them. They can also be edited on the admin question bank page (which rewrites this file)
- The `desc` of each check is the check-item description participants see (when the question set setting "Participants can see whether each check item passed" is on)
- VM questions can have extra disks: add `extra_disks: [2]` under `resources` (in GB; they become `/dev/sdb`, `/dev/sdc`, … in order)
- `terminal` (optional): how the participant's terminal connects to the environment

  | Value | Meaning |
  |---|---|
  | `ssh` (default) | SSH; participants can press "Use console", and it switches to the console automatically when SSH is unreachable |
  | `console` | Starts on the PVE console (does not go through the network; log in as `user` / `user`); participants can press "Use SSH" |
  | `console-only` | Console only, no "Use SSH" (for example to simulate "only the console is available" in a server room) |
  | `ssh-only` | SSH only; no console fallback when it is unreachable (participants who break it have to reset) |

  Use `console` or `console-only` when the question itself leaves SSH unreachable (for example "fix sshd", "broken network settings", "the firewall blocks everything").
  The restriction is enforced on the server; participants cannot get around it by editing the URL.
  - **Checks still use SSH**: until the participant fixes it, a check scores 0 ("cannot connect to the environment"), which is the right result for this kind of question
  - **If SSH is unreachable in the initial state, the self-test cannot apply solution.yml automatically**: the unfixed state counts as 0 points and the admin pages show "Partially passed"; verify the solution once with a test account: fix it on the console, press check and confirm full marks (once after writing or changing the question)
  - Do not make the question change eth0's IP address: the platform assigns it from the VMID, and checks and SSH depend on it

## setup.yml: building the template and planting faults

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
        mode: "0600"           # fault 2
    - name: Service unit (contains fault 1)
      ansible.builtin.copy: {src: files/hello.service, dest: /etc/systemd/system/hello.service}
    - name: "Fault 3: not enabled at boot"
      ansible.builtin.systemd_service: {name: hello, daemon_reload: true, enabled: false}
```


- Runs as root on a **full copy** of the base template (Ubuntu 24.04, already has a `user`/`user` account with sudo, openssh, python3, curl, cron)
- **Internet access is available** during the build (apt and docker pull both work); **during an assessment the participant environment has no internet access**, so every package and image you need must be installed here
- Files in `files/` are referenced with `src: files/xxx`
- When it finishes, the container/VM shuts down automatically and is converted into a template; participant environments are linked clones of the template, so they start quickly
- After you change `setup.yml` or `files/`, the admin pages mark the question "setup changed, rebuild required"

## check.yml: grading

```yaml
- hosts: target
  gather_facts: false
  ignore_errors: true          # required: keep checking other items after one fails
  tasks:
    - name: "check:page browse the page from the grading host"
      ansible.builtin.uri:
        url: "http://{{ target_ip }}:8000/"
        return_content: true
        timeout: 5
      register: page
      failed_when: page.status != 200 or 'Hello Lab' not in page.content
      delegate_to: localhost   # runs on the grading host, out of the participant's reach

    - name: "check:boot enabled at boot"
      ansible.builtin.command: systemctl is-enabled hello
      changed_when: false
```

Rules:

- A task whose name starts with **`check:<id>`** maps to that check item in question.yaml; a description may follow
- One id can have several tasks; the item scores only if **all of them succeed**; a skipped task also counts as a failure
- The variable `target_ip` is the participant environment's IP
- This same file runs both when a participant presses "Check answer" and for the "final grading" after an assessment ends
- The default timeout is 180 seconds (`check_timeout`); for checks that reboot the whole machine, set `reboot_timeout` generously (VMs boot slower when the host is busy)
- If a participant breaks SSH, the result shows "The grader cannot log in to the environment" and scores 0 (the system automatically retries once first); participants can reset the environment themselves

**Key principles:**

- **Check results, not configuration text, whenever possible**: participants can fix things in more than one way; what matters is that the website loads
- **If something can be verified from outside, use `delegate_to: localhost`**: participants have root in their environment and can replace `curl` or `systemctl` with fakes
- "Still works after a restart": actually restart the service in the check and verify again (the example's `restart`)
- "Still works after a reboot": you can use `ansible.builtin.reboot` (VM questions; the participant's terminal will disconnect briefly)

## solution.yml: reference solution and self-test

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

"Build template + self-test" in the admin pages will:

1. Start a test instance from the template and run check.yml; it **should score 0** (a nonzero score means some check item passes before the faults are fixed)
2. Apply solution.yml and run check.yml again; it **should get full marks**

Only when both hold does it show that the self-test passed, which lets you catch mistakes in check.yml before an assessment.
(For `terminal: console` / `console-only` questions whose initial state blocks SSH, only step 1 runs; see `terminal` above.)
You can also press "Self-test only" (without rebuilding the template).

## LXC or VM?

| | LXC (`type: lxc`) | VM (`type: vm`) |
|---|---|---|
| Memory | about 50MB | about 450MB |
| First start | 10~30 seconds | about 1 minute |
| Suited for | service configuration, permissions, users, cron, web servers, databases, Docker | anything needing kernel features: mounting disks (fstab), NFS server, kernel modules, the boot process, firewalls |

**Use LXC whenever you can.** Many VM questions consume far more memory and CPU (see [OPERATIONS.md](OPERATIONS.md)).
LXC containers are unprivileged + nesting, so they can run Docker.

## Multi-node questions

A question can consist of several machines; each participant gets a whole group. Examples:

- [`questions/example-net`](../../questions/example-net): 3 LXC containers, two networks and a router (routing, firewall), about 0.3GB per group
- [`questions/example-k8s`](../../questions/example-k8s): a kubeadm cluster on 3 VMs, about 5.5GB per group

```yaml
type: vm                 # default node type; a node can set type: lxc
networks:                # question networks: each one is a separate VLAN (a switch) per group
  lab: {cidr: 192.168.50.0/24, gateway: 192.168.50.1}   # gateway is optional: a default route (need not exist)
nodes:                   # node name = hostname = Ansible host name = terminal tab name
  cp1: {nics: {lab: 192.168.50.11}, cores: 2, memory: 2560, disk: 16}
  w1:  {nics: {lab: 192.168.50.21}}
  db:  {type: lxc, nics: {lab: 192.168.50.30}, terminal: none}
```

| Node field | Meaning |
|---|---|
| `type` | `lxc` or `vm`; defaults to the question's `type` |
| `cores` / `memory` / `swap` | default to the question's `resources` |
| `disk` | grow the root disk to this many GB (optional) |
| `nics` | `{network: IP}`; they become eth1, eth2… **in the order written**. An IP of `~` attaches the network without an address (participants set it) |
| `terminal` | `ssh` / `console` / `ssh-only` / `console-only` (as for single-machine questions), or `none`: no tab, reachable only from another node over the question network |
| `role` | `grader`: a node used only for grading (see below); must be LXC |

Each entry in `networks`: `cidr` (optional; a network without cidr can only be attached, not given IPs) and `gateway`
(optional: the default route of nodes on that network; a node whose IP is the gateway, e.g. a router, gets no default
route; a node on several networks uses the first one with a gateway). Unknown or misspelled fields (`nic:`, `resources:`…)
show up as errors on the question bank page; multi-node questions don't use `hostname`.

Networking:

- **eth0 is the management network** (terminal and grading use it); don't change it or block it. Write firewall rules
  by interface (`iifname eth1 ...`): Linux also answers for eth0's IP on eth1, so rules on the destination IP alone don't block it
- Each group's question networks are its own VLANs, so **every group uses the same IPs and hostnames** and the whole
  environment (e.g. a kubeadm cluster) is built once in the templates and simply keeps working in the clones
- Participants can't reach another group by adding VLAN sub-interfaces on eth1 (PVE only lets that port carry its
  VLAN); eth0 keeps only the management VLAN as well
- At most 8 nodes and 4 question networks
- **On LXC nodes PVE manages eth1… and rewrites it at every start** (`/etc/systemd/network/eth*.network`): for questions
  where participants change IP settings, or faults planted in the IP configuration, use VM nodes (cloud-init configures
  them on the first boot only and keeps the participant's changes afterwards)
- Nodes with `terminal: none`: participants reach them from another node, so prepare a way to log in (e.g. SSH keys) in
  setup.yml; the base sshd only accepts keys
- The PVE host needs a VLAN-aware vmbr1: on older installs run `deploy/pve-setup.sh network` again on PVE (existing questions are not affected)

Ansible:

- The inventory has one host per node (named after the node), so you can use `hosts: cp1`, `hosts: all:!cp1`
- Per-host variables: `target_ip` (management IP), `node_ip` (IP on the first question network); all nodes:
  `lsa_nodes` (`{name: {mgmt_ip, ips: {network: IP}}}`)
- During the build eth0 has internet access (the default route uses eth0); the networks' `gateway` is added when the
  templates are made. Participant environments are offline, so install packages and images in setup.yml
- If one node is unreachable over SSH during a check, that node's items fail and the other nodes are graded normally

**Grader nodes** (`role: grader`): invisible to participants, no participant account, and **powered off except while a
check runs** (started when "Check answer" is pressed, shut down afterwards). check.yml uses `hosts: grader` to test the
other nodes from there (ping, curl, connection tests), so it checks the effect seen from outside and participants can't
fool it by replacing curl, sysctl and the like on their own machines. Each check takes about 5 seconds longer; a group
has one more LXC (about 50MB, one LV). Example: `example-net` tests the web page from the grader and makes sure the
firewall on web still blocks other ports (so "accept everything" doesn't pass).

Participants: the terminal has one tab per machine; switching tabs keeps the other connections open, and each recorded session is labelled with its node.

Note: memory adds up over all nodes (the example k8s question needs about 5.5GB per group), so these suit interviews and
small classes. LXC nodes can't load kernel modules (bonding, wireguard…), and STP and LACP frames don't pass PVE's bridge.

## Multiple languages

The participant interface can switch languages, and questions can optionally provide translations:

- `lang:` in `question.yaml`: the language of the main content (`title`, `description.md`, and the `desc` of `checks`); default `zh-TW`
- Add an `i18n:` block to `question.yaml` to provide the title and check-item descriptions per language code; the keys under `checks` must be `id`s that already exist in `checks:`
- Save translated descriptions as `description.<language code>.md` (keep commands, paths and code blocks as they are)
- Language codes must match the UI language codes: `zh-TW`, `en`; `zh_TW` (the Linux locale spelling) and different letter case are accepted and treated as `zh-TW`, but a bare `zh` would not be picked up
- Missing translations automatically fall back to the main content
- Changing translations does not require rebuilding the template

The example is **English with a Chinese translation**: `lang: en`, with the Chinese in `i18n: zh-TW:` (see question.yaml above)
and `description.zh-TW.md`.

For **Chinese with an English translation**, do the opposite: omit `lang`, write `title`, `desc` and `description.md` in Chinese,
and put the English in `i18n: en:` and `description.en.md`:

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

## Development and testing workflow

1. Copy `questions/example-hello` to a new folder and rename it (the name = question id)
2. Edit `question.yaml`, `description.md`, `setup.yml`, `check.yml` and `solution.yml`
3. Put it into the system: upload a zip in the admin "Question bank" page, or run `./deploy/push-questions.sh <question-bank-dir> <question-id>`
4. Run "Build template + self-test", read the build log and fix errors until the self-test passes
5. Create a practice question set containing the question and work through it with a test account (to confirm the description is clear and the hints are sufficient)

Debugging tips:

- When a build fails, the build log shows Ansible's error message and the task that failed
- To look inside the template: open the question with a test account and look around in the terminal
- You can also build from the command line on lsa-web:
  ```bash
  pct exec 100 -- bash -c 'cd /opt/lsa && sudo -u lsa LSA_CONFIG=/etc/lsa/config.yaml .venv/bin/python -m app.build <question-id>'
  ```

## Tips for writing questions

- 2~4 check items per question; give the main functionality high points and "persistence" (still works after a restart, starts at boot) low points
- Make the question description clear about the **goal** (what must be achieved), not the **method**; include a way participants can verify it themselves (e.g. `curl`)
- Planted faults should leave clues in system messages (`systemctl status`, `journalctl`, error messages); don't make it a guessing game
- Consider every reasonable way participants might fix things; grade only the result
- Question folders contain solutions, so **manage them in a separate private repo**, never anywhere participants can see
