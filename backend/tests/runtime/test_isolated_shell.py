"""The container shell reports WHY the control plane refused it.

When the shell dies, all anyone has is the container log tail (isolated.py logs
the last 800 chars on a non-zero exit). `raise_for_status()` puts only the status
line there -- "Client error '402 Payment Required'" -- while the reason the
control plane gave ("tenant budget exhausted") is thrown away, in the one place
where nobody can re-run the call to find out.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from oc8.isolated_shell import _preview, _run_shell_locally, check_response, main

STEP = "http://oc8:8000/api/v1/internal/agent/1234/step"


def _response(status_code: int, payload: dict[str, str]) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("POST", STEP))


def test_a_refusal_carries_the_control_planes_reason() -> None:
    with pytest.raises(httpx.HTTPStatusError) as exc:
        check_response(_response(402, {"detail": "tenant budget exhausted"}))

    assert "tenant budget exhausted" in str(exc.value)
    assert "402" in str(exc.value)


def test_a_successful_call_passes_through() -> None:
    check_response(_response(200, {"text": "done"}))  # must not raise


def test_preview_passes_short_text_through_unchanged() -> None:
    assert _preview("short") == "short"


def test_preview_truncates_and_flattens_long_multiline_text() -> None:
    text = "line one\nline two\n" + "x" * 300
    result = _preview(text, limit=20)
    assert len(result) == 20
    assert result.endswith("…")
    assert "\n" not in result


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    **env: str,
) -> int:
    monkeypatch.setenv("OC8_INTERNAL_URL", "http://internal")
    monkeypatch.setenv("OC8_AGENT_TOKEN", "test-token")
    monkeypatch.setenv("OC8_RUN_ID", "run-1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    real_client = httpx.Client

    def fake_client(*, timeout: float, headers: dict[str, str]) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), headers=headers)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return main()


def test_main_logs_every_step_and_tool_call_before_finishing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of this module's logging: a run that calls one tool and
    then finishes must leave a readable trail on stderr, not just the two
    boundary lines it used to print."""
    calls = {"step": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/step"):
            calls["step"] += 1
            if calls["step"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "done": False,
                        "text": "I will look up the ticket now",
                        "tool_calls": [{"id": "call_1", "name": "get_ticket", "arguments": {}}],
                    },
                )
            return httpx.Response(200, json={"done": True, "text": "All done", "tool_calls": []})
        if request.url.path.endswith("/tool"):
            return httpx.Response(200, json={"status": "ok", "output": "ticket #30"})
        if request.url.path.endswith("/finish"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.url.path}")

    exit_code = _run_main(monkeypatch, handler)

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "run run-1: starting" in err
    assert "step 1: calling tool get_ticket" in err
    assert "step 1: tool get_ticket -> status=ok output='ticket #30'" in err
    assert "step 2: model signalled it is finished" in err
    assert "run run-1: finished: done" in err


def test_main_stops_and_reports_suspension_without_finishing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/step"):
            return httpx.Response(
                200,
                json={
                    "done": False,
                    "text": "",
                    "tool_calls": [{"id": "call_1", "name": "send_email", "arguments": {}}],
                },
            )
        if request.url.path.endswith("/tool"):
            return httpx.Response(200, json={"status": "waiting_for_approval", "output": ""})
        raise AssertionError(f"unexpected request: {request.url.path}")

    exit_code = _run_main(monkeypatch, handler)

    assert exit_code == 0
    assert "run suspended: waiting_for_approval" in capsys.readouterr().err


def test_main_honours_a_status_override_from_the_control_plane(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """/step can decide the terminal status itself (e.g. the model was
    truncated by its token budget without producing an answer, even after a
    retry) -- the shell must report that verbatim, never fall back to its
    own done/no-calls heuristic, which would read this as a plain "done"."""
    finish_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/step"):
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "text": "",
                    "tool_calls": [],
                    "status_override": "failed",
                },
            )
        if request.url.path.endswith("/finish"):
            import json

            finish_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.url.path}")

    exit_code = _run_main(monkeypatch, handler)

    assert exit_code == 0
    assert finish_bodies == [{"status": "failed", "output": ""}]
    err = capsys.readouterr().err
    assert "control plane reports 'failed'" in err
    assert "run run-1: finished: failed" in err


def test_main_logs_which_step_failed_before_reraising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/step"):
            return httpx.Response(402, json={"detail": "tenant budget exhausted"})
        raise AssertionError(f"unexpected request: {request.url.path}")

    with pytest.raises(httpx.HTTPStatusError):
        _run_main(monkeypatch, handler)

    assert "FAILED at step 1: 402" in capsys.readouterr().err


def test_run_shell_locally_runs_a_real_command_and_captures_output(tmp_path: Path) -> None:
    result = _run_shell_locally("echo hello", cwd=str(tmp_path))
    assert result["stdout"].strip() == "hello"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False


def test_run_shell_locally_captures_a_nonzero_exit_code(tmp_path: Path) -> None:
    result = _run_shell_locally("exit 3", cwd=str(tmp_path))
    assert result["exit_code"] == 3
    assert result["timed_out"] is False


def test_run_shell_locally_truncates_long_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.isolated_shell.RUN_SHELL_OUTPUT_CHARS", 10)
    result = _run_shell_locally("python3 -c \"print('x' * 100)\"", cwd=str(tmp_path))
    assert len(result["stdout"]) == 10


def test_run_shell_locally_reports_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.isolated_shell.RUN_SHELL_TIMEOUT_S", 0.1)
    result = _run_shell_locally("sleep 2", cwd=str(tmp_path))
    assert result["timed_out"] is True
    assert result["exit_code"] is None


def test_main_executes_run_shell_locally_and_posts_the_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """run_shell is the one tool call this file executes itself -- the /tool
    POST must carry the already-computed result, not wait for the backend to
    run anything."""
    tool_bodies: list[dict[str, object]] = []

    # Monkeypatch _run_shell_locally to use tmp_path as the default cwd
    original = _run_shell_locally
    monkeypatch.setattr(
        "oc8.isolated_shell._run_shell_locally",
        lambda command, *, cwd=str(tmp_path): original(command, cwd=cwd),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/step"):
            if not tool_bodies:
                return httpx.Response(
                    200,
                    json={
                        "done": False,
                        "text": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "name": "run_shell",
                                "arguments": {"command": "echo hi"},
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"done": True, "text": "done", "tool_calls": []})
        if request.url.path.endswith("/tool"):
            tool_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "output": "recorded"})
        if request.url.path.endswith("/finish"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.url.path}")

    exit_code = _run_main(monkeypatch, handler)

    assert exit_code == 0
    assert len(tool_bodies) == 1
    local_result = tool_bodies[0]["local_result"]
    assert local_result["exit_code"] == 0
    assert "hi" in local_result["stdout"]
