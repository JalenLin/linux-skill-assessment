> 中文版：[../OPERATIONS.md](../OPERATIONS.md)

# Operations: Capacity Planning and Troubleshooting

- [Load test results (60 users)](#load-test-results-60-users)
- [Capacity planning](#capacity-planning)
- [Storage (LVM) limits](#storage-lvm-limits)
- [Common commands](#common-commands)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

---

## Load test results (60 users)

Test environment: Intel N305 (8 E-cores), 48GB RAM, NVMe, PVE 9, local-lvm.
60 accounts, 12 questions (9 LXC, 3 VM), simulating a full assessment.

| Scenario | Result |
|---|---|
| Pre-create 720 instances | 0 failures, about 8 seconds each (PVE performs LVM operations one at a time); 720 took about 1.5 hours |
| 60 participants open an LXC question at once | 0 failures; median time to ready 64 seconds, longest 115 seconds |
| 120 environments running at once (90 LXC + 30 VM) | 20.5GB memory |
| 60 participants open a VM question at once | 0 failures; CPU saturated for about 3.5 minutes; median 111 seconds, longest 192 seconds |
| 51 LXC + 70 VM running at once | 35–37GB normally; peak 43.8GB (91%) during simultaneous checks, 0.9GB swap used |
| 60 participants press check at once, while measuring terminal responsiveness | All completed; slowest terminal response 0.3 seconds (unaffected) |
| Final grading of 165 instances | 13 minutes (8 checks in parallel) |

**Actual memory per environment**: about 50MB for LXC, about 450MB for VM (configured with 1GB, but it does not use that much).

**Conclusions and settings:**

- Set "Max running environments per participant" to **1** (the default): 60 participants means at most 60 VMs ≈ 27GB, leaving headroom on a 48GB host.
  With 2, the worst case is 120 VMs ≈ 54GB, which exceeds 48GB
- The bottlenecks are **CPU (many VMs booting at once)** and **PVE only doing one clone at a time**, not the website itself
- The website (lsa-web, 4 cores, 4GB) handles 60 participants with light CPU and memory load

## Capacity planning

**Memory** ≈ 4GB (PVE + lsa-web) + number of running environments × memory per environment

| Participants | 1 question each at a time, all LXC | 1 question each at a time, all VM |
|---|---|---|
| 30 | 6GB | 18GB |
| 60 | 7GB | 31GB |
| 100 | 9GB | 49GB |

**CPU**: light during answering and checking; many VMs booting at once causes a backlog (about 3 minutes for 60 on an N305).
In a real assessment, participants will not all open the same question in the same minute, so the load is lighter than in the test. Before the assessment, remind participants that "the first time you open a VM question, it takes 1–2 minutes".

**Time**:

| Action | Time |
|---|---|
| Build one question template + self-test | LXC 1–2 minutes; VM 3–5 minutes |
| Pre-create instances | About 8 seconds each (N participants × M questions × 8 seconds); **do it the day before the assessment** |
| Delete instances | About 25 disks per minute |
| Final grading | About 5 seconds each (8 in parallel; fstab-type questions that reboot take longer) |

## Storage (LVM) limits

PVE's default `local-lvm` uses LVM-thin, and each instance is one LVM disk (2 for VM questions, including the cloud-init disk).
**The LVM metadata area is only about 1MB; it is fixed when PVE is installed and cannot be enlarged afterwards.**
In testing, it fills up at about **1500 disks** (about 1100 instances), after which creating environments fails:

```
VG pve metadata on /dev/... exceeds maximum metadata size
```

There is usually still plenty of disk space (linked clones are very space-efficient); it is the *number* of disks that hits the limit first.

**Countermeasures:**

1. **Pre-create only for accounts already bound to an ID** (the default)
2. The **"System-wide instance limit"** in "System settings" (default 1000) is checked before pre-creating
3. The "Question set" list shows the current total number of instances; **once a question set has ended and scores are final, delete that question set's instances** (scores are kept)
4. Check current usage:
   ```bash
   vgs -o vg_name,lv_count,vg_mda_size,vg_mda_free pve
   ```
5. If you need more: add a disk, create a new VG / thin pool with larger metadata
   (`pvcreate --metadatasize 64m`), add it as storage in PVE, point `pve_storage` in the configuration file to it,
   and rebuild all templates; or switch to ZFS (which has no such count limit)

An assessment of 60 participants × 12 questions is about 720 instances and 950 disks, within the limit; but remember to clean up the instances of practice question sets.

**Accumulation of LVM configuration backups**: every time a disk is created or deleted, LVM leaves a configuration backup in `/etc/lvm/archive`, kept for 30 days by default.
After the load test (creating + deleting about 1500 disks), more than 10,000 files totaling 3.3GB had accumulated. If the system disk is short on space, you can delete the old ones:

```bash
du -sh /etc/lvm/archive
cd /etc/lvm/archive && ls -t | tail -n +101 | xargs -r rm -f     # keep only the latest 100
```

Or lower `retain_days` (e.g. to 7) in the `backup { }` section of `/etc/lvm/lvm.conf`.

## Common commands

On the PVE host:

```bash
pct exec 100 -- journalctl -u lsa -f            # website logs
pct exec 100 -- systemctl restart lsa           # restart the website (avoid during an assessment)
pct exec 100 -- sqlite3 /var/lib/lsa/data/lsa.db   # query the database directly
pvesh get /pools/lsa                                  # all VMs/CTs in the lsa pool
vgs pve; lvs pve | wc -l                               # LVM usage
free -m; uptime                                        # memory, load
```

## Troubleshooting

**A participant says the terminal won't connect**

- Press "Reconnect" on the question page; if that still fails, have the participant press "Reset environment" (returns to the initial state; their work will be lost)
- If the participant broke the network, firewall or sshd, the terminal **switches to the console automatically** (PVE termproxy, which does not go through the network: a tty for LXC, the serial port for VMs).
  The status bar shows "Console mode (SSH unreachable)"; they log in as `user` with the default password `user`, fix things, then press "Use SSH".
  The automatic switch takes about 20 seconds (about 40 for VMs); participants who know they broke something can press "Use console" above the terminal instead of waiting. the VM serial console does not resize automatically (the terminal shows the `stty` command to run)
- If the console does not work either, the `user` account (password, shell) is probably broken or the environment cannot boot; have them reset
- The replay page marks the moment the terminal switched to the console
- Check the instance status for that question on the participant's page in the admin console; the "Instances" page lets you stop/start it manually

**A check shows "The grader cannot log in to the environment"**

- The participant broke root's SSH or the network, or the environment cannot boot (e.g. a broken fstab prevents booting)
- The system has already retried once automatically; the participant should reset the question and redo it

**Building a template fails**

- Question bank → question page: check the build log; it is usually an Ansible error in setup.yml
- Building requires Internet access: make sure the PVE host can reach the Internet and that the NAT rules from `pve-setup.sh network` are in place
  (`iptables -t nat -L LSA-NAT`)

**Opening a question takes very long or fails**

- When many participants open VM questions at once, the CPU is saturated; wait 1–3 minutes
- "Failed to create environment": check the website logs; if there is an LVM metadata error, see [Storage (LVM) limits](#storage-lvm-limits) above
- The Tasks log in the PVE interface shows clone/start errors

**The host is running out of memory**

- Make sure "Max running environments per participant" is 1
- "Stop all" on the "Instances" page frees memory immediately (contents are kept)

**Forgot the admin password**

- It is in `/root/lsa/admin_password` on the PVE host; or edit `config.yaml`, run `./pve-setup.sh config`, and restart the website

## Known limitations

- Single PVE host only; clustering is not supported
- With the default `local-lvm`, the whole system hits the LVM metadata limit at about 1100 instances; see [Storage (LVM) limits](#storage-lvm-limits)
- After a template is rebuilt, the old template is kept if participant instances still use it; when those instances are later deleted, the old template is not deleted automatically.
  Delete it manually in PVE when needed (`tmpl-<question id>`, VMID 9100 or above, the one not shown on the admin Question bank page)
- When the service restarts, checks in progress are interrupted (the participant just presses it again); do not update the application during an assessment
