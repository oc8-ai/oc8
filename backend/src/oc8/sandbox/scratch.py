"""Rules every driver applies to ``SandboxSpec.scratch`` and its neighbours."""

from __future__ import annotations

import posixpath

from oc8.sandbox.types import SandboxError, SandboxSpec

#: Top-level directories a scratch dir may not cover, nor sit under. A fresh,
#: world-writable volume over any of them would hide the image's own system
#: files or the kernel's interfaces. ``lib*`` covers lib, lib32, lib64, libx32
#: and libexec.
_SYSTEM_ROOTS = frozenset({"etc", "usr", "bin", "sbin", "proc", "sys", "dev"})


def _is_system_path(path: str) -> bool:
    top = path.lstrip("/").split("/", 1)[0]
    return top in _SYSTEM_ROOTS or top.startswith("lib")


def validate_scratch(spec: SandboxSpec) -> None:
    """Refuse a spec whose scratch dirs or seed cannot be honoured safely.

    - every scratch path is absolute, normalised, not ``/``, unique, and not a
      system path (see ``_SYSTEM_ROOTS``);
    - every size is positive;
    - ``read_only`` excludes a seed and scratch dirs: both are unpacked into
      the root filesystem before start, which a read-only root refuses.
    """
    if spec.read_only and (spec.seed is not None or spec.scratch):
        raise SandboxError("read_only cannot be combined with a seed or scratch dirs")
    paths: set[str] = set()
    for entry in spec.scratch:
        path = entry.path
        if (
            not path.startswith("/")
            or posixpath.normpath(path) != path
            or path == "/"
            or path in paths
        ):
            raise SandboxError(f"scratch path is not allowed: {path!r}")
        if _is_system_path(path):
            raise SandboxError(f"scratch path covers a system directory: {path!r}")
        if entry.size_mb <= 0:
            raise SandboxError("scratch size must be positive")
        paths.add(path)
