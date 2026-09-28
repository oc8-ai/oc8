"""Value types for the sandbox capability."""

from __future__ import annotations

from dataclasses import dataclass, field


class SandboxError(RuntimeError):
    """A sandbox operation (provision/exec/fs/teardown) failed."""


@dataclass(frozen=True)
class BindMount:
    """One host directory made visible inside a sandbox.

    ``host_path`` is resolved by the DOCKER DAEMON, i.e. on the host -- not
    inside whatever container asked for it. A control plane that itself runs in
    a container must therefore pass a path that means the same thing on both
    sides.
    """

    host_path: str
    container_path: str
    readonly: bool = True


@dataclass(frozen=True)
class RegistryAuth:
    """Login used only to pull a missing image.

    It is not part of the container environment. ``__repr__`` hides the
    password so a logged spec cannot print it.
    """

    username: str
    password: str
    registry: str = ""

    def __repr__(self) -> str:
        return (
            f"RegistryAuth(username={self.username!r}, registry={self.registry!r}, password='***')"
        )


@dataclass(frozen=True)
class SandboxSpec:
    """Configuration for a provisioned sandbox container.

    Network is DENIED by default (``network_disabled=True``) — this blocks
    SSRF/exfiltration to arbitrary hosts as well as the cloud metadata
    service (IMDS, 169.254.169.254). Set ``network_disabled=False`` to allow
    network access (e.g. for git/pip installs); doing so carries a residual
    IMDS/SSRF risk that is expected to be further constrained by a
    locked-down network in a later hardening slice.
    """

    image: str
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "/workspace"
    command: list[str] = field(default_factory=lambda: ["sleep", "3600"])
    mem_limit: str = "1g"  # hard memory cap
    pids_limit: int = 512  # cap process count (fork-bomb guard)
    cpu_limit: float = 2.0  # CPU cores; -> nano_cpus
    network_disabled: bool = True  # deny network by default (blocks SSRF/IMDS); opt in explicitly
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])  # drop all Linux caps by default
    runtime: str | None = None
    """Container runtime, e.g. "runsc" (gVisor) for community-trust workers.
    None uses the host default (runc)."""
    read_only: bool = False
    """Mount the root filesystem read-only (community hardening)."""
    network: str | None = None
    """Attach to a specific Docker network (e.g. the compose network so the
    container can reach the control-plane internal API by service name). None +
    network_disabled=False uses the default bridge; ignored when network is
    disabled."""
    mounts: list[BindMount] = field(default_factory=list)
    """Bind mounts. Empty by default -- a sandbox is sealed unless a caller
    deliberately opens it, and every mount is validated against one root."""
    labels: dict[str, str] = field(default_factory=dict)
    """Extra container labels, merged with the driver's own.

    Where IDENTITY belongs. The name is for a human reading `docker ps` and is
    deliberately short, which makes it ambiguous: the run fragment in it is the
    first 8 chars of a uuid7, i.e. a TIMESTAMP, so runs started in the same
    moment share it. Anything that ACTS on a container -- the startup reaper
    above all -- must match on a label, never on the name."""
    name: str | None = None
    """Container name (see ``sandbox.naming.container_name``). None lets the
    engine invent one, which is what every caller did until operators had to
    inspect mounts to tell whose container was whose."""
    user: str | None = None
    """Docker's ``--user`` (``"uid:gid"``). None keeps the image's own user.

    Bind mounts carry HOST ownership, so a container that must read or write a
    directory the control plane created has to run as the uid that created it.
    Upstream harnesses pass the host's uid for exactly this reason."""
    registry_auth: RegistryAuth | None = None
    """Login for pulling `image` when it is not already local. Never copied
    into ``env``. None pulls anonymously."""


@dataclass(frozen=True)
class SandboxHandle:
    container_id: str
    image: str


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
