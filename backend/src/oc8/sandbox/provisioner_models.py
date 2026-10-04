"""Typed JSON transport models for the private runtime provisioner API."""

from __future__ import annotations

import base64

from pydantic import BaseModel, ConfigDict, Field

from oc8.sandbox.types import BindMount, RegistryAuth, SandboxHandle, SandboxSpec


class _TransportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BindMountDTO(_TransportModel):
    host_path: str
    container_path: str
    readonly: bool

    @classmethod
    def from_domain(cls, mount: BindMount) -> BindMountDTO:
        return cls(
            host_path=mount.host_path,
            container_path=mount.container_path,
            readonly=mount.readonly,
        )


class RegistryAuthDTO(_TransportModel):
    username: str
    password: str
    registry: str = ""

    def to_domain(self) -> RegistryAuth:
        return RegistryAuth(username=self.username, password=self.password, registry=self.registry)

    @classmethod
    def from_domain(cls, auth: RegistryAuth) -> RegistryAuthDTO:
        return cls(username=auth.username, password=auth.password, registry=auth.registry)


class SandboxSpecDTO(_TransportModel):
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/workspace"
    command: list[str] = Field(default_factory=lambda: ["sleep", "3600"])
    mem_limit: str = "1g"
    pids_limit: int = 512
    cpu_limit: float = 2.0
    network_disabled: bool = True
    cap_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    runtime: str | None = None
    read_only: bool = False
    network: str | None = None
    mounts: list[BindMountDTO] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    name: str | None = None
    user: str | None = None
    registry_auth: RegistryAuthDTO | None = None

    def to_domain(self) -> SandboxSpec:
        return SandboxSpec(
            image=self.image,
            env=self.env,
            workdir=self.workdir,
            command=self.command,
            mem_limit=self.mem_limit,
            pids_limit=self.pids_limit,
            cpu_limit=self.cpu_limit,
            network_disabled=self.network_disabled,
            cap_drop=self.cap_drop,
            runtime=self.runtime,
            read_only=self.read_only,
            network=self.network,
            mounts=[
                BindMount(mount.host_path, mount.container_path, mount.readonly)
                for mount in self.mounts
            ],
            labels=self.labels,
            name=self.name,
            user=self.user,
            registry_auth=None if self.registry_auth is None else self.registry_auth.to_domain(),
        )

    @classmethod
    def from_domain(cls, spec: SandboxSpec) -> SandboxSpecDTO:
        return cls(
            image=spec.image,
            env=spec.env,
            workdir=spec.workdir,
            command=spec.command,
            mem_limit=spec.mem_limit,
            pids_limit=spec.pids_limit,
            cpu_limit=spec.cpu_limit,
            network_disabled=spec.network_disabled,
            cap_drop=spec.cap_drop,
            runtime=spec.runtime,
            read_only=spec.read_only,
            network=spec.network,
            mounts=[BindMountDTO.from_domain(mount) for mount in spec.mounts],
            labels=spec.labels,
            name=spec.name,
            user=spec.user,
            registry_auth=None
            if spec.registry_auth is None
            else RegistryAuthDTO.from_domain(spec.registry_auth),
        )


class SandboxHandleDTO(_TransportModel):
    container_id: str
    image: str

    def to_domain(self) -> SandboxHandle:
        return SandboxHandle(container_id=self.container_id, image=self.image)


class ExecRequestDTO(_TransportModel):
    command: list[str]
    workdir: str | None = None
    timeout: float | None = None


class ExecResponseDTO(_TransportModel):
    exit_code: int
    output: str


class FileWriteRequestDTO(_TransportModel):
    path: str
    content_base64: str

    @classmethod
    def from_content(cls, path: str, content: bytes) -> FileWriteRequestDTO:
        return cls(path=path, content_base64=base64.b64encode(content).decode("ascii"))


class FileReadRequestDTO(_TransportModel):
    path: str


class FileReadResponseDTO(_TransportModel):
    content_base64: str

    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)


class WaitRequestDTO(_TransportModel):
    timeout_s: float


class WaitResponseDTO(_TransportModel):
    exit_code: int


class LogsResponseDTO(_TransportModel):
    output: str


class ReapResponseDTO(_TransportModel):
    count: int = Field(ge=0)
