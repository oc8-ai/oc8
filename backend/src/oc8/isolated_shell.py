"""The thin agent shell that runs INSIDE an isolated per-run container (§8.3).

It holds no secrets and no data — only a run-scoped token and the control-plane
internal URL, both from the environment. It drives the run by alternating
`step` (a model turn) and `tool` (execute a tool) over the internal API, then
reports the terminal result. All privileged work happens control-plane-side.

Deliberately depends only on the standard library + httpx, never on oc8 core, so
the container is a genuinely thin execution shell.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import httpx

#: Hard backstop only -- the control plane (internal_agent.py's /step) enforces
#: the real, per-agent step budget (agent.engine._max_steps) and ends the run
#: with status_override="done" well before this fires in normal operation.
#: Set high enough that a generous per-agent override (config.py's
#: agent_max_steps default is 200) never gets silently truncated here.
MAX_ITERS = 2000
MAX_BODY_CHARS = 1000
PREVIEW_CHARS = 200
#: How long a single run_shell command may run before this shell reports a
#: timeout instead of waiting forever -- there is no outer backstop for a
#: local subprocess the way there is for a model call (the container-level
#: `agent_max_steps * 60s` wait only bounds the WHOLE run, not one command).
RUN_SHELL_TIMEOUT_S = 120.0
#: Truncation cap for run_shell's stdout/stderr -- larger than MAX_BODY_CHARS
#: (that one is for a log line; this is real tool output the model reads).
RUN_SHELL_OUTPUT_CHARS = 4000


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """One log-line-safe rendering of a possibly long, multi-line value."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def check_response(resp: httpx.Response) -> None:
    """`raise_for_status()`, but the message carries the control plane's reason.

    When the shell dies the only trace left is the container log tail, so a bare
    "Client error '402 Payment Required'" means the cause is simply gone -- while
    the body says which budget, which frame, which tool. Deliberately duplicated
    from `modelrouter.http_errors` rather than imported: this module must depend
    on nothing but the standard library and httpx (see the module docstring).
    """
    if not resp.is_error:
        return
    try:
        body = resp.text[:MAX_BODY_CHARS]
    except Exception:
        body = ""
    message = f"{resp.status_code} from {resp.request.url}"
    if body:
        message = f"{message}: {body}"
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp)


def _run_shell_locally(command: str, *, cwd: str = "/workspace") -> dict[str, Any]:
    """Runs `command` in THIS process via subprocess -- the one tool call
    this shell executes itself instead of proxying to the backend (see the
    module docstring). `cwd` defaults to /workspace, the same directory the
    existing write_output_file/sync_run_output mechanism already watches."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            timeout=RUN_SHELL_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
        return {
            "stdout": proc.stdout[:RUN_SHELL_OUTPUT_CHARS],
            "stderr": proc.stderr[:RUN_SHELL_OUTPUT_CHARS],
            "exit_code": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:

        def _decode(v: str | bytes | None) -> str:
            if isinstance(v, bytes):
                return v.decode(errors="replace")
            return v if isinstance(v, str) else ""

        stdout = _decode(exc.stdout)
        stderr = _decode(exc.stderr)
        return {
            "stdout": stdout[:RUN_SHELL_OUTPUT_CHARS],
            "stderr": stderr[:RUN_SHELL_OUTPUT_CHARS],
            "exit_code": None,
            "timed_out": True,
        }


def main() -> int:
    base = os.environ["OC8_INTERNAL_URL"].rstrip("/")
    token = os.environ["OC8_AGENT_TOKEN"]
    run_id = os.environ["OC8_RUN_ID"]
    headers = {"Authorization": f"Bearer {token}"}
    api = f"{base}/api/v1/internal/agent/{run_id}"

    # Before this, the ONLY output this process ever produced was one line on
    # suspend and one on finish -- an operator running `docker logs` on a run
    # that was still in progress, or one that got force-killed by the
    # provisioner's timeout teardown, saw nothing at all. Every line below is
    # printed to stderr with an explicit flush: this process can be SIGKILLed
    # (isolated.py's teardown removes the container in its `finally`, whether
    # this run finished cleanly or the outer wait() timed out on a wedged
    # container), and a line still sitting in a stdio buffer at that moment is
    # gone for good.
    def log(message: str) -> None:
        print(f"[shell] run {run_id}: {message}", file=sys.stderr, flush=True)

    log("starting")
    status, output = "done", ""
    step_no = 0
    try:
        # 120s used to be too tight for a reasoning-heavy model (e.g.
        # z-ai/glm-5.3-flash via OpenRouter): a single /step completion can
        # legitimately take several minutes, and a client-side ReadTimeout
        # here crashes the whole run with no useful message -- not the
        # graceful "waiting_for_approval"/"waiting_for_input" suspend this
        # loop already handles below. The outer provisioner wait() timeout
        # (agent_max_steps * 60s, config.py) stays the real backstop.
        with httpx.Client(timeout=300.0, headers=headers) as c:
            for step_no in range(1, MAX_ITERS + 1):
                r = c.post(f"{api}/step")
                check_response(r)
                step = r.json()
                output = step.get("text") or output
                calls = step.get("tool_calls") or []
                log(
                    f"step {step_no}: model responded, tool_calls={len(calls)} "
                    f"text={_preview(step.get('text') or '')!r}"
                )
                if not calls:
                    override = step.get("status_override")
                    if override:
                        # The control plane already determined this step's
                        # terminal status itself (e.g. the model was truncated
                        # by its token budget without producing an answer,
                        # even after a retry) -- never second-guess it with
                        # the done/no-calls heuristic below.
                        log(f"step {step_no}: control plane reports {override!r}, ending run")
                        status = str(override)
                        break
                    if step.get("done"):
                        log(f"step {step_no}: model signalled it is finished, ending run as done")
                        break
                    # No tool calls but not "done" means the step budget is spent.
                    log(
                        f"step {step_no}: no tool calls and the step budget is spent, "
                        "ending run as done"
                    )
                    status = "done"
                    break
                for tc in calls:
                    args_preview = _preview(json.dumps(tc.get("arguments", {}), default=str))
                    log(f"step {step_no}: calling tool {tc['name']} args={args_preview}")
                    body: dict[str, Any] = {
                        "id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc.get("arguments", {}),
                    }
                    if tc["name"] == "run_shell":
                        command = str(tc.get("arguments", {}).get("command", ""))
                        body["local_result"] = _run_shell_locally(command)
                    tr = c.post(f"{api}/tool", json=body)
                    check_response(tr)
                    result = tr.json()
                    log(
                        f"step {step_no}: tool {tc['name']} -> status={result.get('status')} "
                        f"output={_preview(str(result.get('output', '')))!r}"
                    )
                    if result.get("status") in ("waiting_for_approval", "waiting_for_input"):
                        # The control plane suspended the run; the shell's job is done.
                        log(f"run suspended: {result.get('status')}")
                        return 0
            else:
                log(f"reached the hard backstop of {MAX_ITERS} iterations, ending run as done")
                status = "done"

            check_response(c.post(f"{api}/finish", json={"status": status, "output": output}))
    except Exception as exc:
        # The traceback that follows this (Python's default excepthook, still
        # printed to stderr) has the stack; this line has the one thing the
        # stack can't: which step of THIS run it happened on.
        log(f"FAILED at step {step_no}: {exc}")
        raise
    log(f"finished: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
