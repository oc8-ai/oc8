from __future__ import annotations

from oc8.sandbox.types import ExecResult, SandboxError, SandboxHandle, SandboxSpec


def test_spec_defaults() -> None:
    spec = SandboxSpec(image="alpine:latest")
    assert spec.workdir == "/workspace"
    assert spec.command == ["sleep", "3600"]
    assert spec.env == {}


def test_spec_is_frozen() -> None:
    spec = SandboxSpec(image="alpine:latest")
    try:
        spec.image = "other"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("SandboxSpec should be frozen")


def test_exec_result_and_error() -> None:
    r = ExecResult(exit_code=0, output="ok")
    assert r.exit_code == 0 and r.output == "ok"
    assert issubclass(SandboxError, RuntimeError)
    h = SandboxHandle(container_id="abc", image="alpine:latest")
    assert h.container_id == "abc"


def test_the_workspace_fields_default_to_the_old_behaviour() -> None:
    """A spec that names none of the new fields is exactly the spec it was
    before: no scratch, no seed, open egress, no lifetime bound."""
    spec = SandboxSpec(image="alpine:latest")
    assert spec.scratch == ()
    assert spec.seed is None
    assert spec.egress == "open"
    assert spec.max_lifetime_s is None


def test_a_scratch_dir_names_a_path_and_a_size() -> None:
    from oc8.sandbox.types import ScratchDir

    scratch = ScratchDir(path="/workspace", size_mb=512)
    spec = SandboxSpec(image="alpine:latest", scratch=(scratch,), seed=b"tar", egress="restricted")
    assert spec.scratch[0].path == "/workspace"
    assert spec.scratch[0].size_mb == 512
    assert spec.egress == "restricted"


def test_the_seed_is_not_printed_with_the_spec() -> None:
    """The seed carries config files, the run token among them. A logged spec
    must not print it."""
    spec = SandboxSpec(image="alpine:latest", seed=b"secret-token")
    assert "secret-token" not in repr(spec)
