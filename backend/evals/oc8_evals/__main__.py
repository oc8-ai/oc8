"""`python -m oc8_evals run --suite office --k 5 --runtime isolated|inprocess`
(spec §9.1). Runs inside the backend container; see evals/README.md."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import secrets
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from oc8.config import get_settings
from oc8_evals import stack
from oc8_evals.mocks._store import Store
from oc8_evals.odoo import eval_prefix
from oc8_evals.scoring import Attempt, Check, summarise, write_report
from oc8_evals.tasks import Task, load_suite, resolve_scenario

SUITES = Path(__file__).parent / "suites"
REPORTS = Path(__file__).resolve().parents[1] / "reports"


async def _attempt(
    tenant_id: uuid.UUID,
    task: Task,
    n: int,
    *,
    runtime: str,
    run_tag: str,
    model_config_id: uuid.UUID | None,
    timeout_s: int,
) -> Attempt:
    state_root = Path(get_settings().runtime_session_root) / "evals" / run_tag
    # Multi-system tasks (e.g. odoo+jira) list odoo first; still need a mock
    # state file whenever any non-odoo system is present.
    needs_mock = any(system != "odoo" for system in task.systems)
    source_conn = (
        await stack.find_source_connection(tenant_id, "odoo")
        if "odoo" in task.systems
        else None
    )
    state_file = state_root / f"{task.id}-{n}.json" if needs_mock else None
    ctx = stack.ScenarioContext(
        tenant_id=tenant_id,
        run_tag=f"{run_tag}-{n}",
        prefix=eval_prefix(f"{run_tag}-{n}"),
        odoo=await stack.odoo_for(tenant_id, source_conn) if source_conn is not None else None,
        mock_state=Store(state_file) if state_file is not None else None,
        task=task,
    )
    scenario = resolve_scenario(task.scenario)
    started = time.monotonic()
    seeded: dict[str, Any] = {}
    fixture = None
    run_id = ""
    error: str | None = None
    expect: list[Check] = []
    forbid: list[Check] = []
    final_state = "not_started"
    run_ctx: dict[str, Any] = {}
    try:
        seeded = await scenario.setup(ctx)
        task_for_agent = Task(
            **{**task.__dict__, "task_text": task.task_text.replace("{prefix}", ctx.prefix)}
        )
        fixture = await stack.create_fixture(
            tenant_id,
            task=task_for_agent,
            runtime=runtime,
            run_tag=ctx.run_tag,
            source_conn=source_conn,
            mock_state_file=state_file,
            model_config_id=model_config_id,
        )
        rid = await stack.start_run(tenant_id, fixture, task_for_agent)
        run_id = str(rid)
        ctx.run_id = rid
        final_state, run_ctx = await stack.wait_for_terminal(tenant_id, rid, timeout_s=timeout_s)
        ctx.final_state, ctx.run_context = final_state, run_ctx
        expect = await scenario.expect(ctx, seeded)
        forbid = await scenario.forbid(ctx, seeded)
    except Exception:
        error = traceback.format_exc()
    finally:
        try:
            await scenario.teardown(ctx, seeded)
        except Exception:
            error = (error or "") + "\nteardown failed:\n" + traceback.format_exc()
        if fixture is not None:
            await stack.teardown_fixture(tenant_id, fixture)
    tokens_in, tokens_out, units = (
        await stack.usage_for(tenant_id, fixture.agent_id) if fixture is not None else (0, 0, 0)
    )
    return Attempt(
        task_id=task.id,
        attempt=n,
        run_id=run_id,
        final_state=final_state,
        steps=int(run_ctx.get("steps", 0)),
        tool_calls=len(run_ctx.get("toolCalls", [])),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        platform_units=units,
        expect=expect,
        forbid=forbid,
        asked=final_state == "waiting_for_input",
        parked=final_state == "waiting_for_approval",
        duration_s=round(time.monotonic() - started, 1),
        error=error,
        step_timings=list(run_ctx.get("stepTimings") or []),
    )


async def _run(args: argparse.Namespace) -> int:
    tasks = load_suite(SUITES / args.suite)
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t.id in wanted]
    tenant_id = await stack.find_tenant(args.tenant)
    model_config_id = uuid.UUID(args.model_config) if args.model_config else None
    run_tag = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M") + "-" + secrets.token_hex(2)
    attempts: list[Attempt] = []
    for task in tasks:
        for n in range(1, args.k + 1):
            print(f"[{task.id}] attempt {n}/{args.k} …", flush=True)
            a = await _attempt(
                tenant_id,
                task,
                n,
                runtime=args.runtime,
                run_tag=run_tag,
                model_config_id=model_config_id,
                timeout_s=args.timeout,
            )
            verdict = "PASS" if a.passed else ("ERROR" if a.error else "FAIL")
            print(
                f"[{task.id}] attempt {n}: {verdict} state={a.final_state} steps={a.steps} "
                f"tokens={a.tokens_in + a.tokens_out} ({a.duration_s}s)",
                flush=True,
            )
            attempts.append(a)
    summaries = summarise(attempts, expects_question={t.id: t.expects_question for t in tasks})
    label = args.label or "run"
    stem = f"{label}-{args.runtime}-{run_tag}"
    meta = {
        "suite": args.suite,
        "runtime": args.runtime,
        "k": args.k,
        "run_tag": run_tag,
        "label": label,
        "tenant": str(tenant_id),
        "model_config": args.model_config,
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    write_report(
        REPORTS / f"{stem}.json",
        REPORTS / f"{stem}.md",
        meta=meta,
        attempts=attempts,
        summaries=summaries,
    )
    print((REPORTS / f"{stem}.md").read_text())
    return 0 if all(a.error is None for a in attempts) else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="oc8_evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--suite", default="office")
    run.add_argument("--k", type=int, default=1)
    run.add_argument("--runtime", choices=("inprocess", "isolated"), required=True)
    run.add_argument("--tenant", default=None, help="tenant slug (required if more than one)")
    run.add_argument("--tasks", default=None, help="comma-separated task ids to restrict to")
    run.add_argument("--model-config", default=None, help="ModelConfig id for the fixture agents")
    run.add_argument("--label", default=None, help="report file label, e.g. baseline")
    run.add_argument("--timeout", type=int, default=900, help="seconds to wait per run")
    lst = sub.add_parser("list")
    lst.add_argument("--suite", default="office")
    args = parser.parse_args()
    if args.cmd == "list":
        for t in load_suite(SUITES / args.suite):
            print(
                f"{t.id:32} systems={','.join(t.systems)} "
                f"ask={t.expects_question} approval={t.expects_approval}"
            )
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
