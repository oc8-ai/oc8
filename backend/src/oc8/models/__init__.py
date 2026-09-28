"""ORM models. Importing this package registers every table on Base.metadata."""

from oc8.copilot.models import CopilotOperation, CopilotProposal
from oc8.models.account import AccountVerificationToken
from oc8.models.api_keys import ApiKey
from oc8.models.attachments import FileAttachment
from oc8.models.capas import Capa, CapaInstallation, CapaVersion
from oc8.models.channels import ApprovalChannelBinding, ChannelPollCursor
from oc8.models.chat import ChatMessage, ChatSession
from oc8.models.collab import ContractBinding, Handoff, HandoffType
from oc8.models.components import ComponentGrant
from oc8.models.core import (
    Agent,
    Department,
    ModelConfig,
    Organization,
    Permission,
    Role,
    RolePermission,
)
from oc8.models.credentials import Credential
from oc8.models.flow import Flow, FlowRun, FlowVersion
from oc8.models.identity import (
    DashboardPreset,
    MemberDashboardLayout,
    OrgMember,
    OrgMemberDepartment,
    TotpCredential,
)
from oc8.models.knowledge import (
    DataSource,
    IngestionJob,
    KbChunk,
    KnowledgeBase,
    KnowledgeGrant,
    McpConnection,
    MemoryRecord,
    MemoryStore,
)
from oc8.models.kpi import RunStateTransition
from oc8.models.notifications import PushSubscription
from oc8.models.oauth import OAuthConnection
from oc8.models.ops import (
    ActivityEvent,
    ApprovalRequest,
    AuditChainCheckpoint,
    AuditEvent,
    Budget,
    Integration,
    Task,
    TokenUsageRecord,
)
from oc8.models.pricing import ModelCostReconciliation, ModelPrice
from oc8.models.run import (
    AgentRun,
    Clarification,
    RecordClaim,
    RunCancellation,
    RunMessage,
    ToolInvocation,
)
from oc8.models.secrets import Secret, TenantDek
from oc8.models.skills import ImportedSkillFile, Skill, SkillAssignment, SkillVersion
from oc8.models.supervision import (
    AgentCheckpoint,
    SupervisionAssignment,
    SupervisionIntervention,
    SupervisionPolicy,
    TaskAnchor,
)
from oc8.models.triggers import Trigger

__all__ = [
    "AccountVerificationToken",
    "ActivityEvent",
    "Agent",
    "AgentCheckpoint",
    "AgentRun",
    "ApiKey",
    "ApprovalChannelBinding",
    "ApprovalRequest",
    "AuditChainCheckpoint",
    "AuditEvent",
    "Budget",
    "Capa",
    "CapaInstallation",
    "CapaVersion",
    "ChannelPollCursor",
    "ChatMessage",
    "ChatSession",
    "Clarification",
    "ComponentGrant",
    "ContractBinding",
    "CopilotOperation",
    "CopilotProposal",
    "Credential",
    "DashboardPreset",
    "DataSource",
    "Department",
    "FileAttachment",
    "Flow",
    "FlowRun",
    "FlowVersion",
    "Handoff",
    "HandoffType",
    "ImportedSkillFile",
    "IngestionJob",
    "Integration",
    "KbChunk",
    "KnowledgeBase",
    "KnowledgeGrant",
    "McpConnection",
    "MemberDashboardLayout",
    "MemoryRecord",
    "MemoryStore",
    "ModelConfig",
    "ModelCostReconciliation",
    "ModelPrice",
    "OAuthConnection",
    "OrgMember",
    "OrgMemberDepartment",
    "Organization",
    "Permission",
    "PushSubscription",
    "RecordClaim",
    "Role",
    "RolePermission",
    "RunCancellation",
    "RunMessage",
    "RunStateTransition",
    "Secret",
    "Skill",
    "SkillAssignment",
    "SkillVersion",
    "SupervisionAssignment",
    "SupervisionIntervention",
    "SupervisionPolicy",
    "Task",
    "TaskAnchor",
    "TenantDek",
    "TokenUsageRecord",
    "ToolInvocation",
    "TotpCredential",
    "Trigger",
]
