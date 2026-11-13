"""`_safe_env` is the one choke point every MCP subprocess launch goes
through (McpSession.__init__, agent/mcp_client.py) -- these tests cover the
oc8-branded User-Agent it now injects (see that module's own comment: a WAF
in front of a customer's system, e.g. Cloudflare, needs a real name to
allowlist instead of Python's bare `Python-urllib/<pyver>` default) without
spawning a real subprocess, since the dict it builds is pure, synchronous
logic."""

from __future__ import annotations

import os

from oc8.agent.mcp_client import _SITECUSTOMIZE_DIR, _safe_env
from oc8.net.outbound import USER_AGENT


def test_sets_a_real_oc8_user_agent() -> None:
    env = _safe_env(None)
    assert env["OC8_USER_AGENT"] == USER_AGENT


def test_prepends_the_sitecustomize_dir_to_pythonpath() -> None:
    env = _safe_env(None)
    assert env["PYTHONPATH"] == _SITECUSTOMIZE_DIR


def test_a_plugins_own_pythonpath_survives_alongside_it() -> None:
    # microsoft365/google_workspace both ship their own tool_pack.toml
    # env.PYTHONPATH entry (their bridge package's own directory) -- the
    # sitecustomize dir must not clobber it, both need to stay importable.
    env = _safe_env({"PYTHONPATH": "/app/capas/microsoft365"})
    assert env["PYTHONPATH"] == os.pathsep.join([_SITECUSTOMIZE_DIR, "/app/capas/microsoft365"])


def test_sitecustomize_file_actually_exists_on_disk() -> None:
    # The whole mechanism is a no-op if this file is ever renamed/moved --
    # Python's `site` module only imports a module literally named
    # `sitecustomize`.
    from pathlib import Path

    assert (Path(_SITECUSTOMIZE_DIR) / "sitecustomize.py").is_file()
