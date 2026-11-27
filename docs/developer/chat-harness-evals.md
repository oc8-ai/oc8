# Chat harness evals

The `chat_harness` suite measures how well a model works inside oc8's chat
harness: long-chat recall across a checkpoint, knowledge search, grounded
figures, following a rejection reason, keeping restricted content away from
cloud models, and replay after an edit or a regenerate. It runs with the same
runner as the office suite (`backend/evals/oc8_evals`, see
[`backend/evals/README.md`](../../backend/evals/README.md)) and follows its
rule: pass or fail is decided on stored state, never by reading the
transcript.

It is never run in CI. A tiny subset runs in `pytest` with a fake model
(`backend/tests/evals/`), so the runner cannot rot.

## Run it

Against the dev stack, with the eval suite mounted:

    docker compose -f docker-compose.yml -f docker-compose.evals.yml up -d
    docker compose -f docker-compose.yml -f docker-compose.evals.yml exec \
      -e PYTHONPATH=/app/evals backend \
      python -m oc8_evals run --suite chat_harness --runtime inprocess \
        --model-config <model config id> --label baseline

- `--model-config` is required for the recall and restricted-checkpoint
  cases: each attempt runs on a chat-enabled copy of that model, and the copy
  carries the task's small context window (8192 tokens) so the chat overflows
  and is checkpointed. The copy stops being offered when the attempt ends.
- `--group recall` (or `knowledge`, `must_call_tool`, `rejection_reason`,
  `restricted`, `edit_regenerate`) runs one group; `--tasks a,b` runs named
  cases; `--k 3` repeats each case.
- `--runtime` is required by the CLI but a chat turn always runs where chats
  run (the in-process engine). The worker must be running: it answers the
  turns.
- Prerequisites: exactly one tenant (or `--tenant <slug>`), an embedding model
  for the knowledge cases (the deployment's `default_embedding_model`), and the
  Jira tool pack for the rejection cases (its mock MCP server is started from
  `/app/evals`).

The report lands in `backend/evals/reports/<label>-inprocess-<tag>.{json,md}`.
Per case it lists pass^k, steps, tool calls, tokens, cached tokens (prompt
tokens the provider served from its cache), latency, the scenario's metrics
and the failed checks; a second table sums each group.

## The groups

| Group | Cases | What is checked |
|---|---|---|
| `recall` | 5 | A fact from early in a 40-exchange chat, after the run checkpointed it (the checkpoint must cover the fact); a corrected value; two facts; replay from a seeded checkpoint; a short chat that must not be checkpointed. |
| `knowledge` | 6 | Questions against a 200-chunk corpus whose identifiers differ by a digit (ART-10427 / ART-10472, RE-2026-004711 / RE-2026-004717): exact id, near duplicate, invoice number, paraphrase, German paraphrase, an id that does not exist. The answer must carry the right figure and not a look-alike's. `hit@1` and `hit@5` report where the turn's own retrieval ranks the right chunk; they are reported, not scored. |
| `must_call_tool` | 6 | Copilot questions about budgets, usage, approvals, agents and departments: one of the status tools must have been called in the turn. One case seeds a stale figure from an earlier turn that must not be repeated. |
| `rejection_reason` | 4 | A write on the Jira mock is rejected with a reason (`autonomy = "conservative"`, so every write asks). The resumed turn must follow it: another version number, a summary prefix, update instead of create, stop. The rejected call must not be raised again. |
| `restricted` | 4 | A canary string in a restricted knowledge chunk (with and without the department clearing restricted), in a restricted earlier answer, and in a restricted answer inside a chat that gets checkpointed. On a cloud model the canary must appear neither in the answer nor in any checkpoint summary. |
| `edit_regenerate` | 5 | Edits of the first, a middle and a later turn, and regenerates: what only the old branch or the first answer said must not reach the new answer. |

Checks are deterministic: tool calls on the run, chunk ranks, checkpoint rows,
approval cards, the Jira mock's state, and regular expressions on the answer
the chat stored. No case uses a model judge.

## Add a case

1. `backend/evals/oc8_evals/suites/chat_harness/<id>.toml` with
   `kind = "chat"`, its `group`, the scenario module and `[params]`.
2. The scenario (`oc8_evals/scenarios/chat_*.py`) reads `ctx.task.params`.
   `setup` seeds the chat that `stack.create_chat_fixture` opened
   (`ctx.chat`); `task_text` is then sent as the member's next turn, unless the
   scenario defines `start(ctx, seeded)`, which starts the turn itself (an edit,
   a regenerate, a turn whose approval cards it decides) and returns the run id.
3. `params.agent = "copilot"` chats with the tenant's Copilot instead of a
   throw-away department agent.
