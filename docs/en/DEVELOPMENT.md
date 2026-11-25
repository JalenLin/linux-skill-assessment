> 中文版：[../DEVELOPMENT.md](../DEVELOPMENT.md)

# Development

## Local development (no PVE needed)

`mock` mode doesn't connect to PVE or run Ansible: creating an environment only records state, template builds and self-tests are treated as passed immediately, the terminal echoes your input, and checks score 0 by default (type `mock-pass` in the terminal to make checks pass).
It is handy for working on pages, flows and translations.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
LSA_CONFIG=config.dev.yaml .venv/bin/uvicorn app.main:app --reload --port 8000
```

- Participant side: http://127.0.0.1:8000/ , admin pages: http://127.0.0.1:8000/admin (`admin` / `admin`, see `config.dev.yaml`)
- Data lives in `data-dev/` (not in git); delete it for a fresh database
- Questions go in `questions/` (nothing except the example question is in git)

To test real builds, terminals and grading you need to deploy to PVE (see [INSTALL.md](INSTALL.md));
after making changes, deploy to the test machine with `./deploy/deploy.sh`.

## Code structure

| File | Contents |
|---|---|
| `app/main.py` | FastAPI entry point: participant pages and API, WebSocket terminal, background jobs (state sync, idle suspend, stopping environments when a question set ends) |
| `app/admin.py` | All admin pages and actions |
| `app/config.py` | Config file loading (`LSA_CONFIG`), all settings and defaults |
| `app/db.py` | SQLite schema, automatic upgrades (migrations), System settings defaults |
| `app/sets.py` | Question sets: open status, pausing practice during an assessment, points, attendance calculation |
| `app/questions.py` | Question package loading and validation, zip upload, question translations |
| `app/build.py` | Building question templates and self-tests (can also run from the command line: `python -m app.build <qid>`); multi-node questions get one template per node |
| `app/instances.py` | Participant environment lifecycle: create (linked clone), start, suspend, reset, concurrent running limit; multi-node groups as a unit, VLAN allocation, grader on/off |
| `app/pve.py` | PVE API wrapper (proxmoxer); `MockPVE` for mock mode |
| `app/grader.py` | Check queueing and execution (ansible-runner), scoring policies, score calculation; multi-host inventory for multi-node questions |
| `app/attempts.py` | Per-question time limits and attempt counts |
| `app/shell.py` | Terminal: WebSocket ↔ SSH (asyncssh), console fallback when SSH is down; paste blocking |
| `app/console.py` | PVE console (termproxy / vncwebsocket) client |
| `app/recording.py` | Terminal recordings: asciicast v2 capture, input list, plain-text views, deletion with account/set |
| `app/security.py` | Password hashing (scrypt) and random passwords; sessions use Starlette SessionMiddleware (signed with `secret_key`) |
| `app/i18n.py` | Multiple languages |
| `app/templates/` | Jinja2 templates (`admin/` is the admin pages) |
| `app/static/` | Frontend JS/CSS; `vendor/` is xterm.js |
| `deploy/` | Install and deploy scripts (see INSTALL.md) |
| `tools/` | Small development tools |

### Data flow

- **Opening a question**: `POST /api/s/{set}/q/{qid}/start` → `instances.ensure_running`
  (if there is no instance, linked-clone one from the template; if the concurrent running limit is exceeded, first cleanly shut down the least recently used one) → wait until SSH is reachable
- **Terminal**: `/ws/shell/{set}/{qid}` → `shell.serve` SSHes into the participant environment as `user` with the grading key and relays in both directions.
  If SSH is still unreachable after a 10 / 30 second wait, it falls back to the PVE console: the API token calls `termproxy`, then connects to `vncwebsocket` (the token needs VM.Console, which `PVEVMAdmin` includes)
- **Check**: `POST .../check` → `grader.enqueue` queues it (`grader_concurrency` at a time) → ansible-runner runs
  `check.yml` → pass/fail per item is tallied from task names `check:<id>` → stored in `check_runs`
- **Multi-node questions**: every operation goes through `instances.nodes_of(inst)` (a single-machine question returns one
  node with an empty name). Creating a group allocates one VMID per node and one VLAN per question network
  (`vlan_min`–`vlan_max`, stored in `instances.vlans`); eth1… get their tags via `pve.set_nics`. The terminal websocket
  carries `node=<name>`, and `main._shell_target` uses that node's `terminal` to decide whether and how to connect.
  A check calls `instances.start_graders` first and `stop_graders` afterwards; `grader.inventory_for` builds the
  inventory (host names = node names). Builds and self-tests use the fixed VLANs `build_vlan`–`build_vlan+3`
  (one question is built at a time)
- **Scores**: computed from `check_runs` according to the question set's scoring policy (if points change, scores are recalculated with the new points)

### Database

SQLite (`data_dir/lsa.db`); the schema is `SCHEMA` in `app/db.py`, and the version is recorded in `PRAGMA user_version`.

When changing the schema:

1. Update `SCHEMA` (new installs create the latest version directly)
2. Increment `SCHEMA_VERSION` by 1
3. Add `{new_version: "SQL to upgrade from the previous version"}` to `MIGRATIONS` (existing databases run these in order at startup)

Main tables: `accounts` (`participant_code` = participant ID), `qsets` (question sets; `mode` is `assessment` / `practice`),
`set_questions` (a question set's questions and points), `templates`, `instances`, `check_runs`, `attempts` (timed attempts),
`activity` (basis for attendance), `settings`. Multi-node questions also use `instance_nodes` (every machine of a group;
`instances.vmid/kind/ip` is the first one) and `template_nodes` (one template per node; `templates.vmid` is the first one).

## Multiple languages

- The **source text (Traditional Chinese) in the code and templates is the translation key**: Python `_("檢查作答")`, templates `{{ _("檢查作答") }}`, JS `t("檢查作答")`
- With parameters: `_("請等待 {n} 秒後再檢查", n=5)`; the translation keeps `{n}`
- Translation files: `app/locales/<language>/*.json`, containing `{"source text": "translation"}`; one language can be split across several files
- Messages stored in the database (check results, job status) are stored as source text and translated with `translate_stored` only **when displayed**,
  so users of different languages each see their own language
- The language is determined by the `lang` cookie (switcher at the top right) or the default language in System settings

**Adding a language** (e.g. Japanese):

```bash
mkdir app/locales/ja
echo '{"_name": "日本語"}' > app/locales/ja/_meta.json
python tools/i18n_missing.py ja --json > app/locales/ja/ui.json   # generate the list of strings to translate, then fill in translations
```

Untranslated strings are shown in the source text. After changing UI text, run `python tools/i18n_missing.py en` to check for missing translations.

For translating question content, see [QUESTIONS.md](QUESTIONS.md#multiple-languages).

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # unit + integration (mock mode, no PVE needed, about 30 seconds)
```

- `tests/unit/`: config, passwords, database, multiple languages (including translation completeness), question packages, question set status, scoring policies, attendance
- `tests/integration/`: starts the site in mock mode and runs full flows (admin, accounts, practice, assessments, one question at a time, time limits, attendance, languages)
- `tests/e2e/`: actually answers questions against a real deployment (requires `LSA_E2E_URL` and `LSA_E2E_ADMIN_PASSWORD`)

In mock mode, type `mock-pass [check-id ...]` in the terminal and subsequent checks will pass those items, which lets you test the scoring flow by hand.
GitHub Actions (`.github/workflows/ci.yml`) runs the unit + integration tests and shellcheck on every push.
See [tests/README.en.md](../../tests/README.en.md) for details.
