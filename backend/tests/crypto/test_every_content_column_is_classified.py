"""CI gate: every Text/JSONB/ARRAY(Text) column in the schema is either an
encrypted type or explicitly, individually exempted with a stated reason
(design spec §6, §9 step 1: docs/superpowers/specs/2026-08-10-application-level-encryption-design.md).

This is the point of doing step 1 first: from the moment this test lands,
nothing new can reach the schema unclassified. EXEMPT starts out holding
every column that exists today; later rollout steps (spec §9 steps 3-6)
remove entries from it as columns migrate to an encrypted type, and the
removal itself is the record that the migration happened.
"""

from __future__ import annotations

from sqlalchemy import ARRAY, String
from sqlalchemy.dialects.postgresql import JSONB

import oc8.models  # noqa: F401 -- import registers every table on Base.metadata
from oc8.crypto.types import DeterministicText, EncryptedJSON, EncryptedText
from oc8.db.base import Base

EXEMPT: dict[str, str] = {
    "account_verification_token.purpose": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained to 'password_reset'/'email_change' (ck_account_verification_token_purpose), not content",
    "account_verification_token.token_hash": "sha256(token), hex-encoded -- already a one-way hash, not raw content, same rationale as org_member.password_hash/totp_credential.backup_codes, design spec §6c",
    "account_verification_token.new_email": "content column to encrypt as-is, not yet migrated -- the address an email-change token, once confirmed, rewrites org_member.subject to, design spec §6a",
    "activity_event.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "activity_event.message": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "activity_event.detail": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "agent.name": "displayed/ordered pervasively (agent.name also feeds container naming), design spec §6c",
    "agent.role_title": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "agent.mission": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "agent.definition": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "agent.narrowing": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "agent.narrowing_overridden_keys": "a list of tool-key names tracking operator-write provenance (agent tool login selection design) -- structural/identifier, not customer free text, same rationale as agent.narrowing",
    "agent.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "agent.pause_reason": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "agent.runtime_ref": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "agent.trust_level": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "agent.presentation": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "agent_run.state": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "agent_run.phase": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "agent_run.cursor": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "agent_run.messages": "dead column: no readers or writers anywhere in the codebase; scheduled for removal, not migration, design spec §6c",
    "agent_run.context": "carries the isolated-runtime transcript and the operator's task text; merged via raw SQL (context || :patch) so Postgres' row lock serializes the run executor and the approvals endpoint -- encryption is incompatible with an in-SQL JSON merge. Deferred to its own isolated task, see spec section 7.2 -- not scheduled in this plan, design spec §7.2",
    "agent_run.source": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "agent_run.idempotency_key": "encryptable with a defined workaround, not yet migrated (equality-queried, caller-supplied opaque key -- becomes DeterministicText), design spec §6b",
    "agent_run.coalesce_key": "encryptable with a defined workaround, not yet migrated (equality-queried, caller-supplied opaque key -- becomes DeterministicText), design spec §6b",
    "agent_run.evidence_state": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "api_key.name": "operator-chosen display name for a self-issued key, same rationale as credential.name/mcp_connection.name, not customer free text, design spec §6c",
    "api_key.token_hash": "sha256(token), hex-encoded -- already a one-way hash, not raw content, same rationale as account_verification_token.token_hash, design spec §6c",
    "api_key.token_prefix": "first characters after the oc8_ak_ prefix, kept only so the full (never-stored) token can be recognized on a list screen -- structural/identifier column, not customer free text, design spec §6c",
    "api_key.allowed_origins": "a structural allowlist of origin URLs the key may be called from, not customer free text, same rationale as agent.narrowing, design spec §6c",
    "approval_channel_binding.channel": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "approval_channel_binding.external_id": "encryptable with a defined workaround, not yet migrated (equality-looked-up on the inbound webhook path -- becomes DeterministicText), design spec §6b",
    "approval_channel_binding.code": "encryptable with a defined workaround, not yet migrated (equality-looked-up on the inbound webhook path -- becomes DeterministicText), design spec §6b",
    "approval_request.action_type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "approval_request.payload": "encryptable with a defined workaround, not yet migrated (one query filters payload['department_id'].astext; needs the scope_key column split before this can encrypt), design spec §6b",
    "approval_request.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "approval_request.reason": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "approval_request.reason_context": "a structured decision trace ({'code': Decision.reason_code, **context}: attribute/operator/threshold/actual values) written by authz/pdp.py's own Decision, not customer-authored free text -- same rationale as capa_version.manifest/capa_installation.granted_permissions, design spec §6c",
    "approval_request.title": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "approval_request.detail": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "approval_request.amount_text": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "approval_request.decision_option": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "audit_chain_checkpoint.status": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_chain_checkpoint.break_kind": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.actor_type": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.category": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.action": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.resource": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.decision": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.reason": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.responsible_type": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "audit_event.responsible_id": "audit ledger structural field; must stay diffable and independently verifiable, design spec §6c",
    "capa.name": "dispatch-matched identifier, not customer content, design spec §6c",
    "capa.type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "capa.category": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "capa.author": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "capa.origin": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "capa.trust_level": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "capa.core_compat": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "channel_poll_cursor.channel": "same free-form channel id as approval_channel_binding.channel -- structural/config/identifier column, not customer free text, design spec §6",
    "chat_session.title": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "chat_message.role": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "chat_message.content": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "chat_message.rendered_components": "durable copy of a run's render_component calls (charts/tables/record cards) -- structural/config-shaped JSON, not customer free text, same rationale as agent_run.cursor",
    "capa_installation.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "capa_installation.granted_permissions": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "capa_installation.disabled_reason": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "capa_installation.config": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "capa_version.semver": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "capa_version.manifest": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "capa_version.permissions": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "capa_version.capabilities": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "capa_version.entry_points": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "clarification.question": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "clarification.answer": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "clarification.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "component_grant.component_key": "dispatch-matched identifier (a COMPONENT_CATALOG key), not customer content, design spec §6c",
    "component_grant.grantee_type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "contract_binding.event_type": "dispatch-matched identifier, not customer content, design spec §6c",
    "contract_binding.payload_map": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "contract_binding.gate": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "copilot_operation.operation_type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "copilot_operation.configuration": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "copilot_operation.target_revision": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "copilot_proposal.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "copilot_proposal.created_by": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "copilot_proposal.applied_by": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "credential.name": "operator-chosen display name, unique per tenant (uq_credential_tenant_name) -- structural/identifier column, same rationale as mcp_connection.name, not customer free text -- see design spec §6",
    "credential.credential_type": "dispatch-matched identifier naming a credential_types/*.toml entry (core-owned or capa-contributed), not customer content, design spec §6c",
    "credential.field_values": "content column to encrypt as-is, not yet migrated -- per-credential-type non-secret config (e.g. an S3 region/endpoint); the secret material itself lives only via secret_refs below, design spec §6a",
    "credential.secret_refs": "not the secret material itself -- reference strings into the existing tenant-DEK-encrypted secret vault (oc8.secrets.service), which already carries the sensitive material; structural/identifier column, same rationale as totp_credential.secret_ref",
    "data_source.connector_type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "data_source.name": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "data_source.config": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "data_source.cursor": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "data_source.classification": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "data_source.schedule_cron": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "data_source.last_sync_at": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "data_source.last_sync_status": "structural enum-like column, CHECK-constrained to 'ok'/'failed' (ck_data_source_last_sync_status), not content",
    "data_source.last_sync_error": "content column to encrypt as-is, not yet migrated -- a connector's error message, same shape as data_source.reconcile_note, design spec §6a",
    "data_source.reconcile_state": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "data_source.reconcile_note": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "department.name": "displayed/ordered pervasively (agent.name also feeds container naming), design spec §6c",
    "department.goal": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "department.frame": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "department.frame_capa_defaults": "a snapshot of department.frame exactly as the capa template declared it, same shape/rationale as department.frame itself -- structural/config, not customer free text, design spec §6",
    "department.presentation": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "file_attachment.owner_type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained to 'chat_message'/'agent_instructions' (ck_file_attachment_owner_type), not content",
    "file_attachment.bucket_key": "internal object-store key (oc8.storage.s3), not user content -- the bytes themselves never touch this row, design spec §6c",
    "file_attachment.content_type": "MIME type string (e.g. 'application/pdf'), structural/identifier, not customer free text, design spec §6c",
    "file_attachment.filename": "content column to encrypt as-is, not yet migrated -- the user-supplied original filename, design spec §6a",
    "file_attachment.extracted_text": "content column to encrypt as-is, not yet migrated -- text extracted from an uploaded document, design spec §6a",
    "flow.name": "dispatch-matched identifier, not customer content, design spec §6c",
    "flow_run.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "flow_run.current_stages": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "flow_run.context": "named in design spec §6(a) as an encrypt-now column; not yet migrated to an encrypted type -- tracked for the future column-encryption rollout, not this scaffolding branch",
    "flow_run.trigger_event": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "flow_version.semver": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "flow_version.spec": "content to encrypt (spec 6a), not yet migrated; an adjacent artifact_hash must keep hashing the plaintext, not the ciphertext, design spec §6a",
    "handoff.payload": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "handoff.attachments": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "handoff.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "handoff.gate": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "handoff_type.name": "dispatch-matched identifier, not customer content, design spec §6c",
    "handoff_type.payload_schema": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "handoff_type.classification": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "imported_skill_file.rel_path": "a relative path under references/assets/scripts (e.g. 'references/checklist.md'), derived from the source archive's own directory structure and validated at read time against a fixed subdirectory allowlist -- structural/identifier column, not customer free text -- see design spec §6",
    "ingestion_job.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "ingestion_job.stats": "counter/catalog/config field with no free-text content, design spec §6c",
    "integration.key": "counter/catalog/config field with no free-text content, design spec §6c",
    "integration.name": "counter/catalog/config field with no free-text content, design spec §6c",
    "integration.category": "counter/catalog/config field with no free-text content, design spec §6c",
    "integration.description": "counter/catalog/config field with no free-text content, design spec §6c",
    "integration.used_by": "counter/catalog/config field with no free-text content, design spec §6c",
    "kb_chunk.content": "encryptable with a defined workaround, not yet migrated (encryptable content, but a CHECK constraint does literal content = '' equality to prove erasure -- needs the nullable-content rewrite first), design spec §6b",
    "kb_chunk.source_uri": "encryptable with a defined workaround, not yet migrated (used in GROUP BY / pagination ORDER BY and displayed as a filename -- becomes DeterministicText), design spec §6b",
    "kb_chunk.metadata": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "kb_chunk.classification": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "kb_chunk.raw_object_key": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "kb_chunk.deleted_reason": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "knowledge_base.name": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "knowledge_base.description": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "knowledge_base.embedding_model": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "knowledge_base.chunking_config": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "knowledge_base.classification": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "knowledge_base.freshness": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "knowledge_base.source_ids": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "knowledge_base.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "knowledge_grant.grantee_type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "knowledge_grant.scope": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "mcp_connection.server_url": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "mcp_connection.transport": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "mcp_connection.name": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "mcp_connection.scopes": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "mcp_connection.config": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "mcp_connection.health": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "member_dashboard_layout.widgets": "structural/config-shaped JSON describing a member's own widget grid arrangement (ids/types/positions/sizes), not customer free text, same rationale as chat_message.rendered_components",
    "member_dashboard_layout.template_id": "dispatch-matched identifier (a fixed template key), not customer content, design spec §6c",
    "dashboard_preset.widgets": "structural/config-shaped JSON describing a saved widget grid arrangement (ids/types/positions/sizes), not customer free text, same rationale as member_dashboard_layout.widgets",
    "dashboard_preset.scope": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained to 'personal'/'tenant' (ck_dashboard_preset_scope), not content",
    "dashboard_preset.name": "a member-chosen label for their own saved layout, not customer business content -- same rationale as knowledge_base.name",
    "memory_record.content": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "memory_record.metadata": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "memory_record.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "memory_store.tier": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "model_config.provider": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_config.model": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_config.locality": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_config.params": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_config.fallbacks": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_config.cost_meta": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_config.display_name": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_config.health": "counter/catalog/config field with no free-text content, design spec §6c",
    "model_price.provider": "counter/catalog/config field with no free-text content, versioned pricing table",
    "model_price.model_pattern": "counter/catalog/config field with no free-text content, versioned pricing table",
    "budget.dollar_reference_provider": "counter/catalog/config field with no free-text content, informational $ -> tokens conversion field, design spec 2026-08-19 Part B",
    "budget.dollar_reference_model": "counter/catalog/config field with no free-text content, informational $ -> tokens conversion field, design spec 2026-08-19 Part B",
    "model_cost_reconciliation.provider": "counter/catalog/config field with no free-text content, Anthropic/OpenAI provider identifier, design spec 2026-08-19 Part C",
    "oauth_connection.provider": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "oauth_connection.account_label": "encryptable with a defined workaround, not yet migrated (part of a UNIQUE(tenant_id, provider, account_label) constraint -- becomes DeterministicText), design spec §6b",
    "oauth_connection.scopes": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "oauth_connection.access_secret_ref": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "oauth_connection.refresh_secret_ref": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "oauth_connection.status": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "oauth_connection.client_source": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "oauth_connection.grant_type": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "oauth_connection.azure_tenant_id": "not individually named in spec section 6; structural/config/identifier column (an Azure AD tenant GUID), not customer free text -- see design spec §6",
    "oauth_connection.provider_metadata": "content column to encrypt as-is, not yet migrated -- generic non-secret per-connection facts a provider's own flow needs (e.g. chatgpt_account_id), same shape/rationale as credential.field_values above, design spec §6a",
    "org_member.subject": "encryptable with a defined workaround, not yet migrated (unique IdP identifier; explicitly deferred, left plaintext in v1 per spec section 6b), design spec §6b",
    "org_member.display_name": "encryptable with a defined workaround, not yet migrated (one ORDER BY display_name query -- sort in application code, then treat as (a)), design spec §6b",
    "org_member.password_hash": "already a one-way hash or part of a uniqueness constraint, not raw content, design spec §6c",
    "org_member_department.seat_role": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "organization.slug": "organization has no tenant scoping and must be readable by an unbound session, design spec §6c",
    "organization.name": "organization has no tenant scoping and must be readable by an unbound session, design spec §6c",
    "organization.tier": "organization has no tenant scoping and must be readable by an unbound session, design spec §6c",
    "organization.region": "organization has no tenant scoping and must be readable by an unbound session, design spec §6c",
    "organization.settings": "organization has no tenant scoping and must be readable by an unbound session, design spec §6c",
    "organization.onboarding_status": "organization has no tenant scoping and must be readable by an unbound session, design spec §6c",
    "permission.resource_type": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "permission.action": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "permission.constraint_expr": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "push_subscription.endpoint": "Web Push protocol delivery URL assigned by the browser's push service; structural/config/identifier column, not customer free text -- see design spec §6",
    "push_subscription.p256dh": "Web Push public key material the browser publishes so payloads can be encrypted TO it; structural/config/identifier column, not customer free text -- see design spec §6",
    "push_subscription.auth": "Web Push per-subscription auth secret used only as ECDH/HKDF input by the push encryption; structural/config/identifier column, not customer free text -- see design spec §6",
    "push_subscription.user_agent": "browser capability/identification string the device reports; structural/config/identifier column, not customer free text -- see design spec §6",
    "record_claim.entity": "encryptable with a defined workaround, not yet migrated (the unique constraint on (entity, record_ref) IS the race-resolution mechanism -- becomes DeterministicText), design spec §6b",
    "record_claim.record_ref": "encryptable with a defined workaround, not yet migrated (the unique constraint on (entity, record_ref) IS the race-resolution mechanism -- becomes DeterministicText), design spec §6b",
    "role.name": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "role.kind": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "role.description": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "role_permission.permission": "authorization/policy input read directly by the permission decision point, design spec §6c",
    "run_cancellation.cancellation_kind": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "run_message.author": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "run_message.body": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "run_state_transition.from_state": "structural enum-like column (status/state/kind/classification/...), same value domain as agent_run.state, not content, design spec §6c",
    "run_state_transition.to_state": "structural enum-like column (status/state/kind/classification/...), same value domain as agent_run.state, not content, design spec §6c",
    "secret.name": "pre-existing Secret Store envelope encryption (design spec §2); out of this design's scope",
    "secret.key_version": "pre-existing Secret Store envelope encryption (design spec §2); out of this design's scope",
    "secret.kind": "pre-existing Secret Store envelope encryption (design spec §2); out of this design's scope",
    "skill.name": "dispatch-matched identifier, not customer content, design spec §6c",
    "skill.category": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "skill.description": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "skill.author": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "skill.origin": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "skill.trust_level": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "skill_assignment.overrides": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "skill_version.semver": "not individually named in spec section 6; structural/config/identifier column, not customer free text -- see design spec §6",
    "skill_version.definition": "content to encrypt (spec 6a), not yet migrated; an adjacent artifact_hash must keep hashing the plaintext, not the ciphertext, design spec §6a",
    "task.title": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "task.payload": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "task.state": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "task.meta_label": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "tenant_dek.key_version": "pre-existing Secret Store envelope encryption (design spec §2); out of this design's scope",
    "token_usage_record.model": "counter/catalog/config field with no free-text content, design spec §6c",
    "token_usage_record.provider": "counter/catalog/config field with no free-text content, design spec §6c",
    "totp_credential.secret_ref": "not the TOTP secret itself -- a reference string into the existing tenant-DEK-encrypted secret vault (oc8.secrets.service), which already carries the sensitive material; structural/identifier column, standalone 2FA design",
    "totp_credential.backup_codes": "already a one-way hash (Argon2id, one entry per backup code) or part of a uniqueness constraint, not raw content, design spec §6c",
    "tool_invocation.tool": "already a one-way hash or part of a uniqueness constraint, not raw content, design spec §6c",
    "tool_invocation.args_hash": "already a one-way hash or part of a uniqueness constraint, not raw content, design spec §6c",
    "tool_invocation.result": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "trigger.kind": "structural enum-like column (status/state/kind/classification/...), CHECK-constrained, not content, design spec §6c",
    "trigger.task_text": "content column to encrypt as-is, not yet migrated, design spec §6a",
    "trigger.cron_expression": "dispatch-matched identifier, not customer content, design spec §6c",
    "trigger.event_source": "dispatch-matched identifier, not customer content, design spec §6c",
    "trigger.event_type": "dispatch-matched identifier, not customer content, design spec §6c",
    "trigger.webhook_token": "encryptable with a defined workaround, not yet migrated (equality-queried, server-generated bearer token that is this trigger's entire auth boundary, unlike its dispatch-identifier siblings above -- becomes DeterministicText), design spec §6b",
}

_ENCRYPTED_TYPES = (EncryptedText, EncryptedJSON, DeterministicText)


def _is_free_text_shaped(column_type: object) -> bool:
    if isinstance(column_type, _ENCRYPTED_TYPES):
        return False
    if isinstance(column_type, JSONB):
        return True
    if isinstance(column_type, String):
        return True
    if isinstance(column_type, ARRAY) and isinstance(column_type.item_type, String):
        return True
    return False


def test_every_content_column_is_classified() -> None:
    unclassified: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, _ENCRYPTED_TYPES):
                continue
            key = f"{table.name}.{column.name}"
            if _is_free_text_shaped(column.type) and key not in EXEMPT:
                unclassified.append(key)
    assert not unclassified, (
        "the following columns are Text/JSONB/ARRAY(Text) but neither an encrypted "
        "type nor listed in EXEMPT -- classify them (design spec §6) before merging: "
        f"{sorted(unclassified)}"
    )


def test_exempt_has_no_stale_entries() -> None:
    live_keys = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    stale = sorted(set(EXEMPT) - live_keys)
    assert not stale, f"EXEMPT names columns that no longer exist -- remove them: {stale}"


def test_encrypted_type_instances_are_not_shared() -> None:
    seen: dict[int, str] = {}
    duplicates: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if not isinstance(column.type, _ENCRYPTED_TYPES):
                continue
            key = f"{table.name}.{column.name}"
            instance_id = id(column.type)
            if instance_id in seen:
                duplicates.append(f"{key} shares its type instance with {seen[instance_id]}")
            else:
                seen[instance_id] = key
    assert not duplicates, (
        f"each encrypted column must declare its own type instance so its AAD "
        f"label is unique: {duplicates}"
    )


def test_encrypted_type_label_matches_its_column() -> None:
    mismatched: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if not isinstance(column.type, _ENCRYPTED_TYPES):
                continue
            key = f"{table.name}.{column.name}"
            if column.type.label != key:
                mismatched.append(f"{key} has label {column.type.label!r}")
    assert not mismatched, (
        f"an encrypted column's stamped label must equal its table.column name "
        f"(AAD binding depends on this): {mismatched}"
    )
