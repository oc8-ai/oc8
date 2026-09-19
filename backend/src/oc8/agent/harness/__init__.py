"""The agent harness kernel shared by both builtin runtimes (spec §3).

Everything the in-process engine (`oc8.agent.engine`) and the isolated
runtime's control plane (`oc8.api.v1.internal_agent`) have in common lives
here, so the two cannot drift. Nothing in this package imports the engine.
"""

from oc8.agent.harness.calls import call_sig
from oc8.agent.harness.caps import ModelCaps, resolve_caps
from oc8.agent.harness.state import HarnessState

__all__ = ["HarnessState", "ModelCaps", "call_sig", "resolve_caps"]
