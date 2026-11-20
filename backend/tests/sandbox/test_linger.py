"""oc8.sandbox.linger: the commands every driver shares."""

from __future__ import annotations

import subprocess

from oc8.sandbox.linger import (
    EXIT_FILE,
    START_FILE,
    gated_command,
    lingering_command,
)


def test_the_marker_files_live_in_one_driver_provided_dir() -> None:
    assert START_FILE == "/.oc8/start"
    assert EXIT_FILE == "/.oc8/exit"


def test_an_empty_command_is_never_wrapped() -> None:
    """The image's own CMD then runs; a seeded spec must name its command."""
    assert lingering_command([]) == []
    assert gated_command([]) == []


def test_the_gated_command_waits_for_the_start_file_then_lingers(tmp_path: object) -> None:
    """Run the real script under /bin/sh, with the marker paths pointed at a
    temp dir: nothing runs before the start file appears; then the command
    runs and its exit code is recorded."""
    import pathlib
    import time

    base = pathlib.Path(str(tmp_path))
    cmd = gated_command(["sh", "-c", "echo ran; exit 4"])
    assert cmd[:2] == ["sh", "-c"]
    script = cmd[2].replace("/.oc8", str(base))
    script = script.replace("exec sleep 2147483647", "exit 0")
    proc = subprocess.Popen(["sh", "-c", script, *cmd[3:]], stdout=subprocess.PIPE, text=True)
    time.sleep(0.5)
    assert proc.poll() is None, "ran before the start file existed"
    assert not (base / "exit").exists()
    (base / "start").touch()
    out, _ = proc.communicate(timeout=10)
    assert "ran" in out
    assert (base / "exit").read_text().strip() == "4"
