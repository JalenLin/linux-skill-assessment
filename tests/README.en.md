> 中文版：[README.md](README.md)

# Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                 # unit + integration tests (mock mode, no PVE needed, about 30 seconds)
```

| Directory | Contents | Requires |
|---|---|---|
| `tests/unit/` | Config, passwords, database schema, multiple languages (including translation completeness), question package validation and zip, question set status, scoring policies, attendance | Nothing |
| `tests/integration/` | Starts a site in mock mode and runs full flows: admin pages (Chinese and English), account import and binding, practice, check cooldown, suspending in one-question-at-a-time mode, assessments locking practice and final grading, timed questions and attempt counts, attendance summary, language switching, question bank | Nothing |
| `tests/e2e/` | Connects to a real deployment: example question self-test, solving it through the terminal for full marks, files preserved after switching questions suspends the environment | An installed system |

In mock mode, typing `mock-pass [check-id ...]` in the terminal makes subsequent checks pass those items (no ids = all items);
the integration tests use this to simulate answering.

## End-to-end tests (real PVE)

```bash
LSA_E2E_URL=http://<lsa-web IP> LSA_E2E_ADMIN_PASSWORD=<admin password> \
  .venv/bin/python -m pytest tests/e2e -v
```

- Creates question sets and accounts whose names start with `e2e`, and deletes them afterwards
- If the example question's template hasn't been built yet, it builds it first (about 2 minutes)
- The whole run takes about 3~6 minutes

Running it once after deploying or updating the code is recommended.
