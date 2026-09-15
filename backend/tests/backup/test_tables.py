"""Table-set derivation, denylist, and FK dependency order (design doc §2.1,
§5.2.1)."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from oc8.backup.tables import EXCLUDED_TABLES, dependency_order, exported_tables, table_by_name
from oc8.db.base import Base
from tests.conftest import AppSessionFactory


def test_excluded_tables_all_exist() -> None:
    """A renamed table breaks the build rather than silently re-entering the export."""
    known = set(Base.metadata.tables.keys())
    assert EXCLUDED_TABLES <= known


def test_exported_tables_excludes_the_denylist() -> None:
    exported = exported_tables()
    assert exported.isdisjoint(EXCLUDED_TABLES)
    assert "agent_run" in exported and "run_message" in exported and "task" in exported
    # 62 TenantMixin tables minus the 7-name denylist (channel_poll_cursor,
    # added by migration 0079, is legitimate company operational state,
    # portable like data_source.cursor; account_verification_token, added by
    # migration 0078, joins the denylist -- see EXCLUDED_TABLES docstring;
    # run_state_transition, added by migration 0080, stays INCLUDED -- it is a
    # run's own state history and the only source the live KPI aggregation
    # computes task/execution durations from, so restoring agent_run without it
    # would silently report wrong durations for every restored run rather than
    # no durations at all; imported_skill_file, added by migration 0085, stays
    # INCLUDED -- it is a directly-imported skill's own bundled reference
    # files, portable company data exactly like the skill_version it is
    # pinned to, not instance-bound like secret/tenant_dek; file_attachment,
    # added by migration 0086, stays INCLUDED -- a chat/instruction
    # attachment's row is portable company data exactly like
    # kb_chunk.raw_object_key, not instance-bound;
    # member_dashboard_layout, added by migration 0088, stays INCLUDED too --
    # unlike push_subscription, its rows carry no instance-bound cryptographic
    # material (just widget positions/sizes/config), so a restore into another
    # instance is fully meaningful; the model docstring's "same ownership
    # pattern as push_subscription" is about access control, not portability;
    # dashboard_preset, added by migration 0089, stays INCLUDED for the same
    # reason as member_dashboard_layout -- a saved arrangement is portable
    # company data, not instance-bound; api_key, added by migration 0093,
    # stays INCLUDED too -- unlike push_subscription/secret/tenant_dek, its
    # token_hash carries no instance-bound key material, just sha256 of a
    # random secret the member already holds, so it still validates
    # correctly after a restore into another instance.
    assert len(exported) == 57


def test_table_by_name_returns_a_real_table() -> None:
    table = table_by_name("agent")
    assert "tenant_id" in table.columns


async def test_dependency_order_places_role_before_role_permission_and_org_member(
    app_session: AppSessionFactory,
) -> None:
    tables = exported_tables()
    async with app_session(uuid.uuid4()) as s:
        order = await dependency_order(s, tables)
    assert order.index("role") < order.index("role_permission")
    assert order.index("role") < order.index("org_member")
    # every table exactly once
    assert sorted(order) == sorted(tables)


async def test_dependency_order_deletion_is_the_reverse(
    app_session: AppSessionFactory,
) -> None:
    tables = exported_tables()
    async with app_session(uuid.uuid4()) as s:
        order = await dependency_order(s, tables)
    reverse = list(reversed(order))
    assert reverse.index("role_permission") < reverse.index("role")


async def test_every_table_the_app_may_not_delete_is_excluded_from_the_export(
    app_session: AppSessionFactory,
) -> None:
    """A table the runtime role may not DELETE cannot take part in a restore.

    Restore replaces a company by deleting its rows and inserting the
    archive's. Against an undeletable table the best it could do is merge,
    leaving a company whose history is the union of two -- so
    immutable-by-grant must imply excluded.

    Asks POSTGRES, not the migrations. The first version of this test parsed
    migration 0001's `_IMMUTABLE_TABLES` tuple and was weaker than it claimed:
    migration 0028 had already revoked DELETE on `audit_chain_checkpoint`
    outside that tuple, so the "third immutable table" it promised to catch
    already existed and went unseen. A second version grepped the migrations
    and could not resolve the f-string variables they interpolate. The live
    grant is the only form that no future migration style can outflank.
    """
    async with app_session(uuid.uuid4()) as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT table_name FROM information_schema.table_privileges "
                        "WHERE grantee = 'oc8_app' AND privilege_type = 'DELETE' "
                        "AND table_schema = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )

    deletable = set(rows)
    assert deletable, "oc8_app may DELETE nothing at all -- the query has rotted"
    exported_but_undeletable = exported_tables() - deletable
    assert not exported_but_undeletable, (
        "tables the app may not DELETE, yet the restore would try to: "
        f"{sorted(exported_but_undeletable)}"
    )
