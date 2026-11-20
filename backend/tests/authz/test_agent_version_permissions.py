"""The two permissions publishing introduces, and why each sits where it does.

`test_the_catalogue_is_partitioned` already forces SOME classification for
both. This file pins WHICH, because the partition test would be equally happy
with `agent_version:publish` in `DELEGATABLE_PERMISSIONS` if somebody also
renamed it to end in `:view`, and that is the mistake worth a named test:
publishing is the governed act, editing a draft is not.
"""

from __future__ import annotations

from oc8.authz.catalog import describe
from oc8.authz.permissions import (
    AGENT_VERSION,
    AGENT_VERSION_PUBLISH,
    ALL_PERMISSIONS,
    DELEGATABLE_PERMISSIONS,
    DEPT_MANAGER,
    NEVER_DELEGATABLE,
    NOT_YET_DELEGATABLE,
    OPERATOR,
    ORG_ADMIN,
    VIEW,
    delegation_refusal,
    perm,
    permissions_for,
)


def test_both_permissions_exist() -> None:
    assert perm(AGENT_VERSION, VIEW) in ALL_PERMISSIONS
    assert AGENT_VERSION_PUBLISH in ALL_PERMISSIONS


def test_there_is_no_agent_version_manage() -> None:
    """The verbs are `view` and `publish`, and nothing else.

    `agent_version:manage` would be a permission with no route and no meaning:
    a version is immutable, so there is nothing to manage. Adding the resource
    to the `(VIEW, MANAGE)` product loop above would have minted it silently --
    which is why both strings are listed explicitly in the singleton set
    instead, the same way `statistics:view` already is.
    """
    assert perm(AGENT_VERSION, "manage") not in ALL_PERMISSIONS


def test_reading_versions_is_delegatable_and_publishing_is_not() -> None:
    """Spec §6: publishing is a higher bar than editing.

    An editor may change a draft (that is `agent:manage`, or a seat's
    `agent_manage` toggle) but putting it into production is the governed act --
    so the read side joins the other delegatable `:view` strings, and the
    publish side is refused to a tenant-defined role entirely.
    """
    assert perm(AGENT_VERSION, VIEW) in DELEGATABLE_PERMISSIONS
    assert AGENT_VERSION_PUBLISH in NEVER_DELEGATABLE
    assert AGENT_VERSION_PUBLISH not in NOT_YET_DELEGATABLE
    reason = delegation_refusal(AGENT_VERSION_PUBLISH)
    assert reason is not None and len(reason.strip()) > 30


def test_rollback_has_no_permission_of_its_own() -> None:
    """A rollback IS a publish (spec §2.7 and §6): it copies an old payload onto
    the row and publishes it as a new version, re-entering the same gate. A
    separate `agent_version:rollback` would be a second name for one authority,
    and the first tenant to grant one without the other would discover that the
    two are the same thing the hard way."""
    assert perm(AGENT_VERSION, "rollback") not in ALL_PERMISSIONS


def test_a_department_manager_may_publish_and_an_operator_may_not() -> None:
    """`dept_manager` already holds `agent:manage` -- it configures the
    department's agents -- so withholding publish from it would mean the person
    who edits an agent cannot make the edit take effect. `operator` puts work
    INTO the office and must not re-configure it, which is the same line
    `agent:manage` already draws."""
    assert AGENT_VERSION_PUBLISH in permissions_for(DEPT_MANAGER)
    assert AGENT_VERSION_PUBLISH not in permissions_for(OPERATOR)
    assert AGENT_VERSION_PUBLISH in permissions_for(ORG_ADMIN)


def test_every_viewing_role_can_read_the_version_history() -> None:
    """`agent_version:view` ends in `:view` and its resource is not in
    `_NOT_VIEWABLE_BY_DEFAULT`, so `_VIEW_EVERYTHING` sweeps it into
    operator/dept_manager/auditor automatically. Asserted rather than left to
    the sweep: an auditor who can read every transcript but not the
    configuration that produced it cannot audit anything."""
    for role in (ORG_ADMIN, DEPT_MANAGER, OPERATOR):
        assert perm(AGENT_VERSION, VIEW) in permissions_for(role), role


def test_both_permissions_have_bilingual_prose() -> None:
    """`catalog.py`'s own completeness test would catch a missing entry, but not
    a lazy one: the governance screen renders `permission.split(":")[1]`, so
    `agent_version:view` and `agent:view` would both read "view" without a
    hand-written label."""
    for permission in (perm(AGENT_VERSION, VIEW), AGENT_VERSION_PUBLISH):
        info = describe(permission)
        assert info.label and info.description
        assert info.label_de and info.description_de
        assert info.label != info.label_de, permission
