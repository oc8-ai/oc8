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
            # Before consuming anything: a worker that was killed mid-run never
            # ran its teardown, so its containers are still alive with a live
            # run-scoped token. Only containers of FINISHED runs are removed, so
            # this is safe with several workers on one host. Goes through the
            # sandbox driver -- not oc8.sandbox.reaper directly -- because under
            # OC8_SANDBOX_DRIVER=provisioner (the Community default) the worker
            # has no docker.sock of its own; only runtime-provisioner does, and
            # ProvisionerSandboxDriver.reap_orphans() is what reaches it over
            # HTTP instead of raising DockerException every time.
            from oc8.sandbox import get_sandbox_driver

            await get_sandbox_driver().reap_orphans()

            # The sweeps below run on a timer rather than only at startup: a run
            # abandoned in hour two of a worker's life would otherwise stay open
            # until the next deploy. Only the run queue gets them -- an ingestion
            # job has no containers, no agent to free and no evidence on disk.
            from oc8.evidence.sweep import sweep_evidence
            from oc8.knowledge.reconcile import sweep_knowledge_deletions
            from oc8.runtime.reconcile import close_abandoned_runs
            from oc8.runtime.workspace import sync_active_run_outputs

            async def _housekeeping() -> None:
                # BOTH sweeps on the timer, not one at startup and one on a
                # clock. A container whose run finished after the worker booted
                # used to survive until the next restart: two of them were found
                # alive four hours later, each holding a (by then expired)
                # run-scoped token and a gigabyte of memory on a laptop.
                await close_abandoned_runs()
                await get_sandbox_driver().reap_orphans()
                # Third sweep, same timer: the evidence a finished run left on
                # disk. Inert unless OC8_EVIDENCE_SWEEP_ENABLED is on. It lands
                # here rather than on its own schedule because it must run
                # AFTER the two above -- a run this tick just closed is not
                # archived until the next one, which is exactly the grace the
                # archive window already asks for.
                await sweep_evidence()
                # Fourth sweep, same timer: knowledge a sync observed as gone
                # upstream. Inert unless OC8_KNOWLEDGE_RECONCILE_ENABLED is on.
                # It needs no schedule of its own because it performs no network
                # IO and calls no connector -- it only acts on facts a sync
                # already wrote, so running it more often than syncs happen
                # costs two indexed queries per tenant and changes nothing.
                await sweep_knowledge_deletions()
                # Fifth sweep, same timer: pick up files a still-RUNNING run has
                # already written under /workspace/output/, so they show up in
                # the Files UI before the run finishes, not only after.
                await sync_active_run_outputs()

            await asyncio.gather(
                run_worker(get_run_queue(), housekeeping=_housekeeping),
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
        from oc8.triggers.scheduler import run_scheduler

        asyncio.run(run_scheduler())
    elif args.command == "audit-verify":
        from oc8.audit.integrity import run_integrity_job

        asyncio.run(run_integrity_job(once=args.once, full=args.full))
    elif args.command == "audit-adopt-checkpoints":
        import uuid as _uuid

        from oc8.audit.integrity import adopt_checkpoints

        targets = [_uuid.UUID(t) for t in args.tenant] if args.tenant else None
        print(f"adopted {asyncio.run(adopt_checkpoints(targets))} checkpoint(s)")
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
