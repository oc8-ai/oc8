"""The thin agent shell that runs INSIDE an isolated per-run container (§8.3).

It holds no secrets and no data — only a run-scoped token and the control-plane
internal URL, both from the environment. It drives the run by alternating
`step` (a model turn) and `tools` (batch-execute tool calls) over the internal
API, then reports the terminal result. All privileged work happens
control-plane-side.

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


def program_timeout_s(raw: str | None) -> float:
    """How long a single run_program may run. Default 120; clamp 1..600.

    Callers pass `os.environ.get("OC8_RUN_PROGRAM_TIMEOUT_S")` the same way
    `_run_shell_locally` reads the `RUN_SHELL_TIMEOUT_S` module constant at
    the subprocess call site.
    """
    if raw is None:
        return 120.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 120.0
    return max(1.0, min(600.0, value))


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


def _decode_captured(v: str | bytes | None) -> str:
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    return v if isinstance(v, str) else ""


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
        stdout = _decode_captured(exc.stdout)
        stderr = _decode_captured(exc.stderr)
        return {
            "stdout": stdout[:RUN_SHELL_OUTPUT_CHARS],
            "stderr": stderr[:RUN_SHELL_OUTPUT_CHARS],
            "exit_code": None,
            "timed_out": True,
        }


def _run_program_locally(
    code: str,
    *,
    step_no: int,
    cwd: str = "/workspace",
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Writes `code` to programs/step-{n}.py and runs it with python3.

    Same local_result shape as `_run_shell_locally`. Timeout comes from
    `OC8_RUN_PROGRAM_TIMEOUT_S` (via `program_timeout_s`) unless overridden.
    """
    programs_dir = os.path.join(cwd, "programs")
    os.makedirs(programs_dir, exist_ok=True)
    path = os.path.join(programs_dir, f"step-{step_no}.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(code)
    timeout = (
        timeout_s
        if timeout_s is not None
        else program_timeout_s(os.environ.get("OC8_RUN_PROGRAM_TIMEOUT_S"))
    )
    # Scripts live under programs/, so python3 puts that dir on sys.path[0]
    # — not cwd. Without PYTHONPATH=cwd, `import oc8_tools` fails even when
    # /workspace/oc8_tools.py exists (P12 bulk_partner_review).
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = cwd if not existing else f"{cwd}{os.pathsep}{existing}"
    try:
        proc = subprocess.run(
            ["python3", path],
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
            env=env,
        )
        return {
            "stdout": proc.stdout[:RUN_SHELL_OUTPUT_CHARS],
            "stderr": proc.stderr[:RUN_SHELL_OUTPUT_CHARS],
            "exit_code": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_captured(exc.stdout)
        stderr = _decode_captured(exc.stderr)
        return {
            "stdout": stdout[:RUN_SHELL_OUTPUT_CHARS],
            "stderr": stderr[:RUN_SHELL_OUTPUT_CHARS],
            "exit_code": None,
            "timed_out": True,
        }


def _mirror_spill(spill: dict[str, Any] | None, *, root: str) -> None:
    if not spill:
        return
    filename = str(spill.get("filename") or "").strip()
    if not filename or "/" in filename or filename in {".", ".."}:
        return
    dest_dir = os.path.join(root, "tool-results")
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, filename), "w", encoding="utf-8") as fh:
        fh.write(str(spill.get("content") or ""))


def main() -> int:
    base = os.environ["OC8_INTERNAL_URL"].rstrip("/")
    token = os.environ["OC8_AGENT_TOKEN"]
    run_id = os.environ["OC8_RUN_ID"]
    workspace = os.environ.get("OC8_WORKSPACE", "/workspace")
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
                # Code-mode SDK: write once from /step's sdk_py when the file
                # is not already on disk. Programs import oc8_tools from cwd.
                sdk_py = step.get("sdk_py") or ""
                if sdk_py:
                    sdk_path = os.path.join(workspace, "oc8_tools.py")
                    if not os.path.exists(sdk_path):
                        with open(sdk_path, "w", encoding="utf-8") as fh:
                            fh.write(sdk_py)
                # parallel_tool_calls may still appear on StepResult (spec §3.5 /
                # control-plane pre-pass). The shell always posts one /tools
                # batch; a leading read run is overlapped on the server.
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

                # Flush remotes before any local so a suspend does not leave a
                # later run_shell / run_program mutating /workspace unused.
                batch: list[dict[str, Any]] = []

                def _flush_batch() -> bool:
                    """POST the pending batch. True when the run suspended."""
                    if not batch:
                        return False
                    tr = c.post(f"{api}/tools", json={"calls": batch})
                    check_response(tr)
                    results = list(tr.json().get("results") or [])
                    for i, result in enumerate(results):
                        name = batch[i]["name"] if i < len(batch) else "?"
                        log(
                            f"step {step_no}: tool {name} -> status={result.get('status')} "
                            f"output={_preview(str(result.get('output', '')))!r}"
                        )
                        _mirror_spill(result.get("spill"), root=workspace)
                        if result.get("status") in ("waiting_for_approval", "waiting_for_input"):
                            log(f"run suspended: {result.get('status')}")
                            batch.clear()
                            return True
                    batch.clear()
                    return False

                for tc in calls:
                    args_preview = _preview(json.dumps(tc.get("arguments", {}), default=str))
                    log(f"step {step_no}: calling tool {tc['name']} args={args_preview}")
                    name = tc["name"]
                    if name in ("run_shell", "run_program"):
                        if _flush_batch():
                            return 0
                        body: dict[str, Any] = {
                            "id": tc["id"],
                            "name": name,
                            "arguments": tc.get("arguments", {}),
                        }
                        if name == "run_shell":
                            body["local_result"] = _run_shell_locally(
                                str(tc.get("arguments", {}).get("command", ""))
                            )
                        else:
                            body["local_result"] = _run_program_locally(
                                str(tc.get("arguments", {}).get("code", "")),
                                step_no=step_no,
                                cwd=workspace,
                            )
                        batch.append(body)
                        if _flush_batch():
                            return 0
                    else:
                        batch.append(
                            {
                                "id": tc["id"],
                                "name": name,
                                "arguments": tc.get("arguments", {}),
                            }
                        )
                if _flush_batch():
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
