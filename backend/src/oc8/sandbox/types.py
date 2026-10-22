"""Value types for the sandbox capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Egress = Literal["open", "restricted"]


class SandboxError(RuntimeError):
    """A sandbox operation (provision/exec/fs/teardown) failed."""


class ArchiveTooLarge(SandboxError):
    """An archive passed its size cap. Raised while it is read, before the
    rest of it is buffered."""


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
class ScratchDir:
    """A writable directory the driver provides inside one sandbox.

    It starts EMPTY (the image's own files at that path are not copied in, as
    with a Kubernetes ``emptyDir``), is writable by any user in the container
    (Docker: mode 1777, root-owned, sticky like /tmp; an ``emptyDir`` is 0777)
    and goes away with the sandbox. Its ancestors the image lacks are created
    root-owned, mode 0755: seed into the scratch dir, not next to it.

    ``size_mb`` is the most it may hold. "Full" has no guaranteed shape: a
    driver may fail writes with ENOSPC, may evict the whole sandbox
    (Kubernetes, on ``ephemeral-storage``), or may not enforce the size at
    all (Docker without quota support). A runtime must not rely on any one.
    """

    path: str
    size_mb: int


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
    """The NUMERIC ``"uid"`` or ``"uid:gid"`` the sandbox runs as, never a name
    (an orchestrator's ``runAsUser`` takes numbers, and the image's passwd db
    need not know the uid). None keeps the image's own user. Every exec and
    every read (``get_archive``) runs as this user.

    Bind mounts carry HOST ownership, so a container that must read or write a
    directory the control plane created has to run as the uid that created it.
    Upstream harnesses pass the host's uid for exactly this reason."""
    registry_auth: RegistryAuth | None = None
    """Login for pulling `image` when it is not already local. Never copied
    into ``env``. None pulls anonymously."""
    scratch: tuple[ScratchDir, ...] = ()
    """Writable directories the driver provides, each with a size limit. The
    way to give a sandbox a workspace without a host directory."""
    seed: bytes | None = field(default=None, repr=False)
    """A tar archive unpacked at ``/`` BEFORE the command starts, so the
    command's first instruction already sees every file in it. Kept out of
    ``repr``: it carries config files, run tokens among them.

    A portable seed targets only the spec's scratch dirs (see
    ``SandboxDriver.put_archive`` on identity and ownership), never
    ``/.oc8``, and comes with an explicit ``command``: the start guarantee
    holds for the wrapped command, not for an image's own CMD."""
    egress: Egress = "open"
    """Which outbound network profile the sandbox asks for. ``"restricted"``
    lets a driver that can enforce an allowlist do so; it must still reach
    ``settings.internal_base_url`` (the control plane's gateways), or no
    runtime works. A driver that cannot enforce one (Docker) ignores it and
    applies the network settings above unchanged. ``network`` is a Docker
    network name; drivers without Docker networks ignore it."""
    max_lifetime_s: int | None = None
    """Upper bound on the sandbox's life, in seconds, counted from creation.
    Past it the driver (or its reaper) may remove the sandbox whatever its
    run says: a backstop for a worker that died, sized by the runtime to
    cover the whole leg plus slack, not a deadline a leg should reach. None
    means no bound beyond the caller's own teardown."""


@dataclass(frozen=True)
class SandboxHandle:
    container_id: str
    image: str


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
