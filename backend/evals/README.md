# oc8 agent-harness evals

State-diff evaluation of real agent runs (design spec §9). Not collected by the
normal `pytest`; talks to the dev stack.

## Run

    docker compose -f docker-compose.yml -f docker-compose.evals.yml up -d
    docker compose -f docker-compose.yml -f docker-compose.evals.yml exec \
      -e PYTHONPATH=/app/evals backend \
      python -m oc8_evals run --suite office --k 5 --runtime isolated --label baseline

Reports land in `backend/evals/reports/<label>-<runtime>-<tag>.{json,md}`.

Prerequisites: exactly one tenant (or pass `--tenant <slug>`), a connected MCP
connection named `odoo` in that tenant (the Odoo dev stack), and — for
`--runtime isolated` — the runtime provisioner up.

## Deterministic tests

    cd backend && PYTHONPATH=evals uv run pytest evals/tests -q

## Add a task

1. `oc8_evals/suites/office/<id>.toml` — static declaration (`id` = file name).
2. `oc8_evals/scenarios/<id>.py` — `setup / expect / forbid / teardown`, reading
   the target system (Odoo over XML-RPC, or the mock's state file). Never read
   the transcript to decide pass/fail.
3. `{prefix}` in `task_text` is replaced by the per-attempt record prefix so
   seeded records are unambiguous and teardown can delete by prefix.
