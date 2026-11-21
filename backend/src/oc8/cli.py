"""Command line entry point (`oc8 ...`)."""

from __future__ import annotations

import argparse
import asyncio
import logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="oc8", description="oc8 control plane tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_p = sub.add_parser("seed", help="load seed data")
    seed_p.add_argument("--reset", action="store_true", help="delete existing rows before seeding")

    sub.add_parser("worker", help="run the durable-run background worker")
    sub.add_parser("ingestion-worker", help="run only the knowledge-ingestion worker")
    sub.add_parser("scheduler", help="run the cron trigger scheduler")

    audit_p = sub.add_parser("audit-verify", help="verify audit hash chains")
    audit_p.add_argument("--full", action="store_true", help="re-verify from genesis")
    audit_p.add_argument("--once", action="store_true", help="run one tick and exit")

    adopt_p = sub.add_parser(
        "audit-adopt-checkpoints",
        help=(
            "sign a keyed-state marker for every audit checkpoint that has none "
            "(run once after enabling OC8_AUDIT_MAC_ENABLED on an existing "
            "deployment; safe to run twice)"
        ),
    )
    adopt_p.add_argument(
        "--tenant",
        action="append",
        default=None,
        metavar="UUID",
        help="limit to this tenant (repeatable); default is every known tenant",
    )

    consolidate_p = sub.add_parser(
        "consolidate-connections",
        help=(
            "fold per-department tool setups into shared logins (the worker "
            "also runs this once at start; safe to run twice)"
        ),
    )
    consolidate_p.add_argument(
        "--tenant", default=None, metavar="UUID", help="only this tenant; default every tenant"
    )
    consolidate_p.add_argument(
        "--dry-run", action="store_true", help="report what would change, change nothing"
    )

    sub.add_parser(
        "evidence-sweep",
        help=(
            "archive finished runs' evidence and reduce what is past its "
            "retention window (one pass; the worker also runs this on its "
            "housekeeping timer)"
        ),
    )

    sub.add_parser(
        "knowledge-reconcile",
        help=(
            "tombstone documents two attested syncs agree are gone upstream, "
            "and destroy the content of tombstones past their grace window "
            "(one pass; the worker also runs this on its housekeeping timer)"
        ),
    )

    # Community is single-tenant in production, provisioned by `POST
    # /auth/setup` against the one bootstrapped organization -- there is no
    # `create`/`invite-admin`/`invite` here. `list` remains, plus the
    # `member`/`role` subcommands below, for operating on the tenant that
    # already exists.
    tenant_p = sub.add_parser("tenant", help="tenant inspection")
    tenant_sub = tenant_p.add_subparsers(dest="tenant_command", required=True)

    tenant_sub.add_parser("list", help="list tenants")

    member_p = sub.add_parser("member", help="people, and the departments they stand in")
    member_sub = member_p.add_subparsers(dest="member_command", required=True)

    m_grant = member_sub.add_parser("grant", help="seat a person in a department")
    m_grant.add_argument("--tenant", required=True, metavar="SLUG")
    m_grant.add_argument("--subject", required=True, help="the token `sub`, verbatim")
    m_grant.add_argument("--department", required=True, metavar="NAME")
    m_grant.add_argument(
        "--role",
        default="dept_approver",
        choices=("dept_viewer", "dept_approver"),
        help="dept_viewer reads the queue; dept_approver also answers it",
    )
    m_grant.add_argument("--display-name", default="", help="how the screens should name them")

    m_revoke = member_sub.add_parser(
        "revoke", help="take a seat away (effective on their next request)"
    )
    m_revoke.add_argument("--tenant", required=True, metavar="SLUG")
    m_revoke.add_argument("--subject", required=True)
    m_revoke.add_argument("--department", required=True, metavar="NAME")

    m_list = member_sub.add_parser("list", help="everybody this tenant knows, and their seats")
    m_list.add_argument("--tenant", required=True, metavar="SLUG")

    # `set-role` is the out-of-band lockout repair as well as an ordinary
    # assignment: `member:manage` is the permission that hands out roles, so an
    # administrator who ended up without it has no HTTP door back in. `--clear`
    # is that door.
    m_set_role = member_sub.add_parser(
        "set-role", help="give somebody a tenant role, or clear the assignment"
    )
    m_set_role.add_argument("--tenant", required=True, metavar="SLUG")
    m_set_role.add_argument("--subject", required=True, help="the token `sub`, verbatim")
    m_role_or_clear = m_set_role.add_mutually_exclusive_group(required=True)
    m_role_or_clear.add_argument("--role", metavar="NAME", help="the tenant role to assign")
    m_role_or_clear.add_argument(
        "--clear",
        action="store_true",
        help="remove the assignment; the person's TOKEN decides again (not 'no permissions')",
    )

    m_import = member_sub.add_parser(
        "import",
        help=(
            "drive people, roles and seats from a CSV "
            "(columns: subject, display_name, role, department, seat_role)"
        ),
    )
    m_import.add_argument("--tenant", required=True, metavar="SLUG")
    m_import.add_argument("--csv", required=True, metavar="FILE", dest="csv_path")

    role_p = sub.add_parser("role", help="the tenant's own roles")
    role_sub = role_p.add_subparsers(dest="role_command", required=True)

    r_list = role_sub.add_parser("list", help="every role, with holder counts")
    r_list.add_argument("--tenant", required=True, metavar="SLUG")

    r_show = role_sub.add_parser("show", help="one role, its grants and its holders")
    r_show.add_argument("--tenant", required=True, metavar="SLUG")
    r_show.add_argument("name")

    r_create = role_sub.add_parser("create", help="compose a role from the offerable rights")
    r_create.add_argument("--tenant", required=True, metavar="SLUG")
    r_create.add_argument("name")
    r_create.add_argument(
        "--permission",
        action="append",
        default=[],
        metavar="RESOURCE:ACTION",
        help="repeatable; see `GET /permissions/catalogue` for what may be granted",
    )
    r_create.add_argument("--description", default="")

    r_delete = role_sub.add_parser("delete", help="soft-delete a role (refused while it is held)")
    r_delete.add_argument("--tenant", required=True, metavar="SLUG")
    r_delete.add_argument("name")
    r_delete.add_argument(
        "--reassign-to",
        default=None,
        metavar="NAME",
        help="move every holder to this role instead of refusing the delete",
    )

    args = parser.parse_args()

    # The worker, the scheduler and the ingestion worker have no web server to
    # borrow a logging config from, so without this they run completely mute --
    # which is exactly how a failing agent run became undiagnosable.
    from oc8.config import get_settings
    from oc8.observability.logs import setup_logging

    setup_logging(get_settings())
    # One line at startup, so "is this process even alive and is its logging
    # wired?" is answerable from `docker compose logs` instead of by experiment.
    # These processes otherwise log only when something goes wrong, which makes
    # silence ambiguous exactly when it matters.
    logging.getLogger("oc8.cli").info("starting %s", args.command)

    if args.command == "seed":
        from oc8.seed import run_seed

        asyncio.run(run_seed(reset=args.reset))
    elif args.command == "worker":
        from oc8.knowledge.worker import get_ingestion_queue, ingest_job, recover_ingestion
        from oc8.runtime.queue import get_run_queue
        from oc8.runtime.worker import run_worker

        async def _run_both() -> None:
            # Before consuming: reap containers a killed worker left behind and
            # republish queued runs whose publish was lost; then the same sweeps
            # on the run worker's timer (oc8.runtime.housekeeping).
            from oc8.runtime.housekeeping import run_worker_housekeeping, run_worker_startup

            await run_worker_startup()

            await asyncio.gather(
                run_worker(get_run_queue(), housekeeping=run_worker_housekeeping),
                run_worker(get_ingestion_queue(), handler=ingest_job, recovery=recover_ingestion),
            )

        asyncio.run(_run_both())
    elif args.command == "ingestion-worker":
        from oc8.knowledge.worker import get_ingestion_queue, ingest_job, recover_ingestion
        from oc8.runtime.worker import run_worker

        asyncio.run(
            run_worker(get_ingestion_queue(), handler=ingest_job, recovery=recover_ingestion)
        )
    elif args.command == "scheduler":
        from oc8.events.sources import run_event_poller
        from oc8.triggers.scheduler import run_scheduler

        async def _schedule() -> None:
            # Polled event sources tick in their own task, so a slow outside
            # system never delays a schedule.
            stop = asyncio.Event()
            poller = asyncio.create_task(run_event_poller(stop))
            try:
                await run_scheduler()
            finally:
                stop.set()
                await poller

        asyncio.run(_schedule())
    elif args.command == "audit-verify":
        from oc8.audit.integrity import run_integrity_job

        asyncio.run(run_integrity_job(once=args.once, full=args.full))
    elif args.command == "audit-adopt-checkpoints":
        import uuid as _uuid

        from oc8.audit.integrity import adopt_checkpoints

        targets = [_uuid.UUID(t) for t in args.tenant] if args.tenant else None
        print(f"adopted {asyncio.run(adopt_checkpoints(targets))} checkpoint(s)")
    elif args.command == "consolidate-connections":
        import json
        import sys
        import uuid as _uuid

        from oc8.connections.consolidate import consolidate_all_tenants, consolidate_tenant
        from oc8.db.session import tenant_session

        async def _consolidate() -> int:
            if args.tenant is None:
                reports = await consolidate_all_tenants(dry_run=args.dry_run)
            else:
                tenant_id = _uuid.UUID(args.tenant)
                async with tenant_session(tenant_id) as db:
                    reports = [await consolidate_tenant(db, tenant_id, dry_run=args.dry_run)]
            for report in reports:
                print(json.dumps(report.as_dict(), sort_keys=True))
            return 0

        sys.exit(asyncio.run(_consolidate()))
    elif args.command == "evidence-sweep":
        from oc8.config import get_settings as _settings
        from oc8.evidence.sweep import sweep_evidence

        if not _settings().evidence_sweep_enabled:
            # Said out loud rather than reported as "0 runs archived", which
            # reads as "nothing to do" and sends an operator looking for a bug
            # in the sweep instead of at the flag.
            print("evidence sweep is disabled (set OC8_EVIDENCE_SWEEP_ENABLED=true)")
        report = asyncio.run(sweep_evidence())
        print(
            f"archived {len(report.archived)} run(s): "
            f"{report.raw_bytes / 1024:.1f} kB of evidence -> "
            f"{report.stored_bytes / 1024:.1f} kB stored, "
            f"{report.dropped_bytes / 1024:.1f} kB of runtime housekeeping dropped; "
            f"reduced {len(report.reduced)}; "
            f"reclaimed {report.reclaimed_bytes / 1024 / 1024:.1f} MB; "
            f"pruned {report.invocations_pruned} spent idempotency row(s); "
            f"{report.failures} failure(s)"
        )
    elif args.command == "knowledge-reconcile":
        from oc8.config import get_settings as _settings
        from oc8.knowledge.reconcile import sweep_knowledge_deletions

        if not _settings().knowledge_reconcile_enabled:
            # Same reason as the evidence sweep above, and it matters more here:
            # this job DELETES customer knowledge, so "0 documents" is exactly
            # what a working, enabled sweep with nothing to do would print.
            print("knowledge reconciliation is disabled (set OC8_KNOWLEDGE_RECONCILE_ENABLED=true)")
        k_report = asyncio.run(sweep_knowledge_deletions())
        print(
            f"swept {k_report.tenants} tenant(s): "
            f"tombstoned {k_report.documents_tombstoned} document(s) absent from "
            f"every attested listing; "
            f"reduced {k_report.documents_reduced} past their grace window and "
            f"{k_report.superseded_reduced} superseded; "
            f"{k_report.chunks} chunk(s) emptied; "
            f"{k_report.failures} failure(s)"
        )
    elif args.command == "tenant":
        import sys

        from oc8.tenants import cli as tenant_cli

        code = asyncio.run(tenant_cli.cmd_list())
        sys.exit(code)
    elif args.command == "member":
        import sys

        from oc8.tenants import cli as tenant_cli

        if args.member_command == "grant":
            code = asyncio.run(
                tenant_cli.cmd_member_grant(
                    slug=args.tenant,
                    subject=args.subject,
                    department=args.department,
                    role=args.role,
                    display_name=args.display_name,
                )
            )
        elif args.member_command == "revoke":
            code = asyncio.run(
                tenant_cli.cmd_member_revoke(
                    slug=args.tenant, subject=args.subject, department=args.department
                )
            )
        elif args.member_command == "set-role":
            code = asyncio.run(
                tenant_cli.cmd_member_set_role(
                    slug=args.tenant,
                    subject=args.subject,
                    # `None` is the CLEAR, and it is a different act from
                    # assigning a role that grants nothing: it removes the
                    # override so the person's token decides again.
                    role=None if args.clear else args.role,
                )
            )
        elif args.member_command == "import":
            code = asyncio.run(tenant_cli.cmd_member_import(slug=args.tenant, path=args.csv_path))
        else:
            code = asyncio.run(tenant_cli.cmd_member_list(slug=args.tenant))
        sys.exit(code)
    elif args.command == "role":
        import sys

        from oc8.tenants import cli as tenant_cli

        if args.role_command == "list":
            code = asyncio.run(tenant_cli.cmd_role_list(slug=args.tenant))
        elif args.role_command == "show":
            code = asyncio.run(tenant_cli.cmd_role_show(slug=args.tenant, name=args.name))
        elif args.role_command == "create":
            code = asyncio.run(
                tenant_cli.cmd_role_create(
                    slug=args.tenant,
                    name=args.name,
                    permissions=args.permission,
                    description=args.description,
                )
            )
        else:
            code = asyncio.run(
                tenant_cli.cmd_role_delete(
                    slug=args.tenant, name=args.name, reassign_to=args.reassign_to
                )
            )
        sys.exit(code)


if __name__ == "__main__":
    main()
