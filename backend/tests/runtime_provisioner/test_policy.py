from __future__ import annotations

import pytest

from oc8.config import get_settings
from oc8.runtime_provisioner.app import _default_policy
from oc8.runtime_provisioner.policy import ProvisionerPolicy
from oc8.sandbox.types import BindMount, SandboxError, SandboxSpec


def _policy() -> ProvisionerPolicy:
    return ProvisionerPolicy(
        allowed_images={"oc8-runtime:1", "nanoclaw-agent:1", "nanoclaw-provisioner:1"},
        agent_network="oc8_agents",
        session_root="/var/lib/oc8/sessions",
        platform_mounts={"/opt/nanoclaw/runner": "/app/src"},
    )


def test_policy_rejects_an_arbitrary_image() -> None:
    with pytest.raises(SandboxError, match="image"):
        _policy().validate(SandboxSpec(image="attacker:latest"))


def test_policy_rejects_host_or_default_network() -> None:
    with pytest.raises(SandboxError, match="network"):
        _policy().validate(SandboxSpec(image="oc8-runtime:1", network_disabled=False))
    with pytest.raises(SandboxError, match="network"):
        _policy().validate(
            SandboxSpec(image="oc8-runtime:1", network_disabled=False, network="host")
        )


def test_policy_rejects_a_mount_outside_allowed_roots() -> None:
    with pytest.raises(SandboxError, match="mount"):
        _policy().validate(
            SandboxSpec(
                image="oc8-runtime:1",
                mounts=[BindMount("/etc", "/workspace/etc")],
            )
        )


def test_policy_allows_both_writable_and_read_only_mounts_inside_the_session_root() -> None:
    """The session root's boundary is the per-run path, not the mount's mode.

    The headless-CLI runtimes generate a config file per run (a run-scoped
    token, the MCP endpoint, an approval policy) and mount it read-only so the
    agent's own file tools cannot rewrite how it is governed. Such a file cannot
    be registered in `platform_mounts`, which is fixed at startup.
    """
    normalized = _policy().validate(
        SandboxSpec(
            image="oc8-runtime:1",
            mounts=[
                BindMount("/var/lib/oc8/sessions/run-1/workspace", "/workspace", readonly=False),
                BindMount(
                    "/var/lib/oc8/sessions/run-1/config.toml",
                    "/etc/agent/config.toml",
                    readonly=True,
                ),
            ],
        )
    )
    assert [m.readonly for m in normalized.mounts] == [False, True]


def test_default_policy_allows_the_three_cli_runtime_images_on_the_agent_network() -> None:
    """The wiring, not the class: the images live in Settings, the policy must see them."""
    settings = get_settings()
    policy = _default_policy()
    images = [
        settings.claude_code_agent_image,
        settings.codex_agent_image,
        settings.opencode_agent_image,
    ]
    for image in images:
        assert image in policy.allowed_images
        # Networked as well: a disabled network leaves the harness unable to
        # reach oc8's /llm and /mcp gateways.
        normalized = policy.validate(
            SandboxSpec(
                image=image,
                network_disabled=False,
                network=settings.agent_runtime_network,
            )
        )
        assert normalized.image == image


def test_default_policy_still_refuses_an_unlisted_image() -> None:
    with pytest.raises(SandboxError, match="image"):
        _default_policy().validate(SandboxSpec(image="attacker:latest"))


def test_default_policy_allows_an_image_a_runtime_capa_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "oc8.runtime.images.declared_runtime_images",
        lambda paths=None: {"registry.example/custom-runtime:1"},
    )
    settings = get_settings()
    policy = _default_policy()
    assert "registry.example/custom-runtime:1" in policy.allowed_images
    normalized = policy.validate(
        SandboxSpec(
            image="registry.example/custom-runtime:1",
            network_disabled=False,
            network=settings.agent_runtime_network,
        )
    )
    assert normalized.image == "registry.example/custom-runtime:1"
    with pytest.raises(SandboxError, match="image"):
        policy.validate(SandboxSpec(image="attacker:latest"))


def test_policy_forces_dropped_capabilities_and_rejects_excessive_resources() -> None:
    normalized = _policy().validate(
        SandboxSpec(
            image="oc8-runtime:1", cap_drop=[], mem_limit="512m", pids_limit=32, cpu_limit=0.5
        )
    )
    assert normalized.cap_drop == ["ALL"]

    with pytest.raises(SandboxError, match="memory"):
        _policy().validate(SandboxSpec(image="oc8-runtime:1", mem_limit="3g"))
