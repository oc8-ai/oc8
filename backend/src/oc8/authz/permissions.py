"""What an operator may do on the control-plane API (§5.2).

Until now this layer was decoration. `role` and `permission` tables were seeded
and read by nothing -- 5 role rows, 0 permission rows on the live system -- while
authorization was a string comparison against one role name on 38 of 122 routes.
The other 84 had no role check at all: **any** authenticated member of a tenant
could install a plugin, create an agent, change what an agent is allowed to do,
or decide an approval. §5.5 says in as many words that the approver must hold
`approve` on the resource; nothing checked it.

**The vocabulary is `resource:action`**, and deliberately small. §5.2 describes a
richer tuple -- `(resource_type, resource_id | *, action, constraint)` -- and
that shape is where this goes when per-object grants and tenant-defined roles
arrive. Starting there would have meant writing a constraint evaluator before
anything was enforced at all, and the gap being closed here is not "grants are
too coarse", it is "there are no grants".

**Built-in roles are defined in CODE, not in the database.** The tables still
carry them, so they can be inspected and so custom roles have somewhere to live,
but no request's authorization depends on a row existing. That is not tidiness:
an authorization layer whose grants come from seed data fails in one of two ways
when the data is missing or a tenant predates the seed -- it locks every operator
out, or it lets everyone through -- and neither belongs in the path of an
operator trying to fix a production problem.

**Nothing here widens anything.** Every route that had `require_role("org_admin")`
maps to a permission only `org_admin` holds, and the three that also admitted
`operator` map to one `operator` holds too. The routes that gain a gate can only
lose reach, never gain it.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------- actions

#: Read a resource. For secrets that means the METADATA only -- the store never
#: returns a value to anybody, operator or not (§12.3), so `secret:view` is the
#: right to know a credential exists, never to read it.
VIEW: Final = "view"
#: Create, change, or delete it.
MANAGE: Final = "manage"

# ------------------------------------------------------------------- resources

AGENT: Final = "agent"
DEPARTMENT: Final = "department"
RUN: Final = "run"
APPROVAL: Final = "approval"
KNOWLEDGE: Final = "knowledge"
PLUGIN: Final = "plugin"
SECRET: Final = "secret"
BUDGET: Final = "budget"
AUDIT: Final = "audit"
SETTINGS: Final = "settings"
SKILL: Final = "skill"
SUPERVISION: Final = "supervision"
TRIGGER: Final = "trigger"
FLOW: Final = "flow"
HANDOFF: Final = "handoff"
CONTRACT: Final = "contract"
CHANNEL: Final = "channel"
INTEGRATION: Final = "integration"
MODEL: Final = "model"
#: The configuration assistant. Its snapshot and its proposals are tenant-wide
#: by construction -- `copilot/chat.py::configuration_snapshot` hands the model
#: every agent's name and status with no department filter, because the whole
#: point is a chat that can see the office to talk about it. That is exactly
#: what `agent:manage`/`agent:view` must NOT mean (a seat holder's view is
#: department-scoped), so Copilot gets its own resource rather than borrowing
#: theirs, and it is `NEVER_DELEGATABLE` below.
COPILOT: Final = "copilot"
#: A human this tenant knows about, and the seats they hold (§1). `member:manage`
#: is the right to enrol somebody in a department, which is why it is deliberately
#: NOT in any seat vocabulary and not in `_DEPT_MANAGER`: a Head of Sales who can
#: enrol himself in Engineering is the department boundary in a different coat.
#:
#: The word collides with the token ROLE `member` -- the empty role an employee is
#: minted with -- and the collision is harmless because the two are different
#: namespaces: a resource name here, a role name in `BUILTIN_ROLE_PERMISSIONS`,
#: and `permissions_for("member")` (the role) now returns the tenant-wide
#: `copilot:use` default every human role gets, not the empty set -- the
#: collision stays harmless because a resource name and a role name are
#: different namespaces regardless of what either resolves to.
MEMBER: Final = "member"
#: A named subset of this catalogue that a tenant's IT admin authored, and the
#: assignment of it to a person. `role:view` is what makes the tenant's own role
#: list readable at all -- `GET /governance` deliberately never enumerates it, so
#: without this permission there is no route that does.
#:
#: The word collides twice over, and both collisions are harmless because all
#: three live in different namespaces: `role` is a RESOURCE here, a built-in role
#: NAME in `BUILTIN_ROLE_PERMISSIONS`, and a table holding both human and agent
#: rows in the schema. The `role.kind` column is what keeps the last pair apart.
ROLE: Final = "role"
#: A question an agent parked mid-run waiting for a person to answer. Not in the
#: resource product below on purpose -- there is no `clarification:manage`,
#: because nobody creates or edits a clarification; an agent raises one and a
#: human answers it, and those are the only two verbs that exist.
CLARIFICATION: Final = "clarification"
#: The one resource an AGENT is judged on. Everything above is the operator API;
#: this is §5.3's `role_permissions` term for a tool call, which until now was
#: simply absent -- the decision started at the department frame.
TOOL: Final = "tool"
#: The whole tenant, packed into one archive and restored from one (design
#: doc, tenant-backup-restore). Its own resource rather than folded into
#: `SETTINGS`: an export is a READ of everything the tenant holds, including
#: secret values with a passphrase, and a restore DELETES every agent, run
#: and knowledge chunk -- neither is "change a tenant-wide switch".
BACKUP: Final = "backup"
#: The live-computed usage/performance numbers this tenant's agents and
#: departments produce. Its own resource rather than folded into
#: AGENT/DEPARTMENT: the tenant-wide GET /kpis endpoint lets one request
#: span every agent and department at once, which no single existing
#: :view permission covers -- AGENT:VIEW and DEPARTMENT:VIEW are both
#: enforced via require_departmental (scoped to seats/department
#: membership), and this is deliberately NOT department-scoped in the
#: same way. No :manage counterpart exists, mirroring AUDIT's own
#: precedent (see AUDIT_VERIFY above) -- nobody configures a KPI, they
#: only read it.
#:
#: Also mirrors AUDIT in being excluded from the default `_VIEW_EVERYTHING`
#: sweep (see `_NOT_VIEWABLE_BY_DEFAULT` below), for the same shape of reason:
#: the whole point of this being its own resource is that it can span every
#: agent and department in the tenant in one request, which "no single
#: existing :view permission covers" only means something if that span isn't
#: ALSO handed to every `operator`/`dept_manager`/`auditor` automatically --
#: a `dept_manager` seated in Sales holding it by default would read every
#: OTHER department's numbers through `GET /kpis`, despite never being able
#: to see those departments any other way. `org_admin` still gets it (via
#: `ALL_PERMISSIONS`); everyone else needs it granted explicitly, same as
#: `audit:view` already works.
STATISTICS: Final = "statistics"


def perm(resource: str, action: str) -> str:
    return f"{resource}:{action}"


#: Starting a run is not "managing an agent": an operator who may put work into
#: the office must not thereby be able to re-configure the agent doing it. It was
#: already separate in practice -- the three routes that admitted `operator`
#: alongside `org_admin` are exactly these.
RUN_START: Final = perm(RUN, "start")
#: Cancel, answer a question, steer a live run. Same holder as starting one.
RUN_CONTROL: Final = perm(RUN, "control")
#: Deciding an approval is its own permission, per §5.5. Not folded into
#: `agent:manage`: the whole point of the approval gate is that a SECOND person
#: signs off, and a permission that anyone configuring the agent also holds
#: cannot express that.
APPROVAL_DECIDE: Final = perm(APPROVAL, "decide")
#: Re-verifying the hash chain writes checkpoints, so it is not a `view`. It is
#: still an AUDITOR's job rather than an administrator's -- the whole point of
#: the role is that it can check the log without being able to change what the
#: log is about.
AUDIT_VERIFY: Final = perm(AUDIT, "verify")

#: Reading, and answering, a parked question. `clarification:view` is swept into
#: `_VIEW_EVERYTHING` by ending in `:view`; `clarification:answer` is not a view
#: and is granted explicitly, to the same holders as `approval:decide` -- both are
#: a person unblocking a run, and a role that may sign off a held tool call but
#: not answer the question the agent asked instead would be a distinction nobody
#: could explain.
CLARIFICATION_VIEW: Final = perm(CLARIFICATION, VIEW)
CLARIFICATION_ANSWER: Final = perm(CLARIFICATION, "answer")

#: The one door every human in the tenant gets by default: the tenant-wide
#: Assistant chat (`GET /assistant`, `POST /chat/sessions` against its
#: agent id). Tenant-wide only -- there is no department-scoped "use the
#: Copilot in Vertrieb only" -- and delegatable, so a tenant-defined role
#: can also carry it. Granted to every built-in human role by default,
#: including the otherwise-empty `MEMBER_ROLE`: opening the Copilot to
#: everyone is the whole point of this permission existing.
COPILOT_USE: Final = perm(COPILOT, "use")

#: A readable copy of the whole tenant (transcripts, knowledge, and with a
#: passphrase every credential) and the ability to replace it wholesale from
#: an uploaded one. Neither is a `:view`/`:manage` pair -- `backup:view`
#: would suggest a reversible look, and `backup:manage` would suggest a
#: configuration change, when what these two verbs actually do is copy
#: everything out and delete everything in.
BACKUP_EXPORT: Final = perm(BACKUP, "export")
BACKUP_RESTORE: Final = perm(BACKUP, "restore")

#: "…in every department, including ones created after you signed in."
#:
#: These two are what separates a company-wide role from a departmental one, and
#: they are separate permissions rather than a property of a role name for one
#: reason: a seat may carry `approval:view` / `approval:decide` (that is the whole
#: of `SEAT_PERMISSIONS`), so if holding `approval:decide` meant "anywhere", every
#: seat would be tenant-wide and the department boundary would not exist.
#:
#: Neither ends in `:view`, so neither is swept into `_VIEW_EVERYTHING` -- which is
#: load-bearing, not incidental: a sweep that caught `approval:view_any` would
#: hand every viewing role the whole company's approvals back again.
APPROVAL_VIEW_ANY: Final = perm(APPROVAL, "view_any")
APPROVAL_DECIDE_ANY: Final = perm(APPROVAL, "decide_any")

#: The three rights a tool call is classified into (`required_right`), now also
#: expressible as a role grant. Same three words on both sides on purpose: the
#: PDP intersects them directly, and a second vocabulary would need a mapping
#: table that could disagree with itself.
TOOL_READ: Final = perm(TOOL, "read")
TOOL_WRITE: Final = perm(TOOL, "write")
TOOL_SEND: Final = perm(TOOL, "send")
_TOOL_RIGHTS: Final[dict[str, str]] = {"read": TOOL_READ, "write": TOOL_WRITE, "send": TOOL_SEND}

#: Every permission this system knows. A typo in a route's declaration is a
#: startup-time error rather than a route that silently admits nobody.
ALL_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        perm(r, a)
        for r in (
            AGENT,
            DEPARTMENT,
            KNOWLEDGE,
            PLUGIN,
            BUDGET,
            SETTINGS,
            SKILL,
            SUPERVISION,
            TRIGGER,
            FLOW,
            HANDOFF,
            CONTRACT,
            CHANNEL,
            INTEGRATION,
            MODEL,
            RUN,
            APPROVAL,
            MEMBER,
            ROLE,
            COPILOT,
        )
        for a in (VIEW, MANAGE)
    }
    | {
        RUN_START,
        RUN_CONTROL,
        APPROVAL_DECIDE,
        APPROVAL_VIEW_ANY,
        APPROVAL_DECIDE_ANY,
        CLARIFICATION_VIEW,
        CLARIFICATION_ANSWER,
        COPILOT_USE,
        AUDIT_VERIFY,
        TOOL_READ,
        TOOL_WRITE,
        TOOL_SEND,
        perm(SECRET, VIEW),
        perm(SECRET, MANAGE),
        perm(AUDIT, VIEW),
        perm(STATISTICS, VIEW),
        BACKUP_EXPORT,
        BACKUP_RESTORE,
    }
)

# ------------------------------------------ what a tenant-defined role may hold

#: THREE HAND-WRITTEN SETS, and deliberately not `DELEGATABLE = ALL_PERMISSIONS -
#: UNGRANTABLE`.
#:
#: A deny-list fails OPEN the moment somebody adds a permission, and that is not
#: hypothetical: the seat slice added `member:manage`, `approval:view_any` and
#: `approval:decide_any` to this file, and under a deny-list all three would have
#: become offerable in the role builder without anybody touching this section.
#: Any one of them ends the seat mechanism -- `member:manage` because its holder
#: enrols himself in Engineering, the two `_any` flags because they ARE the words
#: "in every department". Written out by hand, and with
#: `test_the_catalogue_is_partitioned` asserting the three are pairwise disjoint
#: and cover `ALL_PERMISSIONS` exactly, a new permission fails the build until a
#: human has classified it. That is the only arrangement that gets SAFER as the
#: catalogue grows.
#:
#: The intersection is applied at RESOLVE time, not at write time (see
#: `authz.authority`): a `role_permission` row that arrives by restore, by psql,
#: or from a future importer is inert rather than dangerous, and a permission
#: that moves out of this set later goes inert on every existing role's next
#: request with no backfill to remember.
#:
#: What is left is: look, start work, answer, decide, verify. The most a
#: tenant-defined role can ever be is a set that changes no configuration
#: anywhere in the system -- which is set arithmetic with a test, not a promise
#: in a docstring.
DELEGATABLE_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        perm(APPROVAL, VIEW),
        APPROVAL_DECIDE,
        CLARIFICATION_VIEW,
        CLARIFICATION_ANSWER,
        COPILOT_USE,
        RUN_START,
        RUN_CONTROL,
        AUDIT_VERIFY,
        perm(AUDIT, VIEW),
        perm(KNOWLEDGE, VIEW),
        perm(BUDGET, VIEW),
        perm(MEMBER, VIEW),
        perm(ROLE, VIEW),
        perm(SETTINGS, VIEW),
        perm(MODEL, VIEW),
        perm(SKILL, VIEW),
        perm(TRIGGER, VIEW),
        perm(FLOW, VIEW),
        perm(CONTRACT, VIEW),
        perm(CHANNEL, VIEW),
        perm(PLUGIN, VIEW),
        # A read of aggregate numbers, same sensitivity class as `audit:view`
        # above (also delegatable, also tenant-wide-only): a tenant-defined
        # role holding it sees run counts and durations, never a transcript,
        # a credential, or a policy decision. `GET /kpis` is gated by
        # `require_permission` (never `require_departmental`), so a role that
        # holds this holds it tenant-wide -- there is no per-department seat
        # narrowing for it, which is why it is absent from `SEAT_PERMISSIONS`
        # / `DEPARTMENT_SCOPABLE` even though it is delegatable.
        perm(STATISTICS, VIEW),
        # Graduated out of `NOT_YET_DELEGATABLE` by the department-scoped agent
        # authority slice (migration 0048): both routes now resolve a department
        # before they answer -- `GET /agents`/`GET /agents/{id}` and
        # `GET /departments`/`GET /departments/{id}`/`.../board`/`.../tools` all
        # sit behind `require_departmental` and filter through `scope.viewable`,
        # exactly the term every other entry in this set already relies on. Safe
        # specifically because it is READ: a tenant-defined role holding either
        # string tenant-wide still only ever sees what `scope.viewable` admits.
        perm(AGENT, VIEW),
        perm(DEPARTMENT, VIEW),
        perm(SUPERVISION, VIEW),
    }
)

#: Refused today, and each entry names what has to change before it stops being
#: refused. These three are reads whose ROUTE is still tenant-wide: the seat
#: slice could leave them alone because they are held only by tenant-wide
#: built-in roles, but the moment an admin can tick a box they become "500
#: people may read every transcript in the company", which is that slice's
#: confidentiality disposition inverted. They graduate when the route gains a
#: department term, not when somebody decides the label reads harmlessly.
#: `perm(AGENT, VIEW)` and `perm(DEPARTMENT, VIEW)` graduated out of this dict
#: (into `DELEGATABLE_PERMISSIONS`, above) in the department-scoped-agent-
#: authority slice, exactly as their own former entries here anticipated.
NOT_YET_DELEGATABLE: Final[dict[str, str]] = {
    perm(RUN, VIEW): (
        "GET /runs/{id} returns the transcript and every tool call, tenant-wide; "
        "graduates with the departmental read term"
    ),
    perm(HANDOFF, VIEW): (
        "GET /handoffs is tenant-wide and names both departments of every "
        "handoff; graduates with the departmental read term"
    ),
    perm(INTEGRATION, VIEW): (
        "api/v1/mcp.py returns server_url, command AND args -- a reachability map "
        "of the customer's systems plus the exact argv to clone one"
    ),
}

#: Refused permanently, with one dated exception (`model:manage`, whose exit
#: condition is written at the entry). Grouped by WHY, because the four groups
#: are four different arguments: the first two and the last are permanent, and
#: only the third -- "every remaining `:manage`" -- is expected to shrink, one
#: permission at a time, as each route learns to resolve a department.
NEVER_DELEGATABLE: Final[dict[str, str]] = {
    # ---- code execution and credential substitution
    perm(PLUGIN, MANAGE): (
        "installing a plugin loads code into the backend process itself "
        "(plugins/lifecycle.py); it is not a configuration change"
    ),
    perm(INTEGRATION, MANAGE): (
        "writes config.command/args/secret_env verbatim: arbitrary execution, and "
        "an effective read of every secret VALUE the tenant holds"
    ),
    perm(SECRET, VIEW): (
        "the list of credentials a tenant holds is a map of everywhere it can "
        "reach, worth more to an attacker than most of what it protects"
    ),
    perm(SECRET, MANAGE): (
        "rotating a credential repoints a live connection at whatever "
        "infrastructure the new value names"
    ),
    perm(MODEL, MANAGE): (
        "declares a cloud endpoint 'local' and the classification rule believes "
        "it. EXIT: when the catalogue verifies locality against the endpoint"
    ),
    perm(SETTINGS, MANAGE): (
        "flips the hire gate and the rest of the organisation-wide switches; a "
        "tenant-wide setting is by definition not a departmental one"
    ),
    perm(COPILOT, VIEW): (
        "reviewing a proposal is reviewing what the tenant-wide snapshot let the "
        "model see; a departmental grant here would leak every other department's "
        "agents to whoever holds it"
    ),
    perm(COPILOT, MANAGE): (
        "configuration_snapshot hands the model every agent tenant-wide with no "
        "department filter, and a prepared proposal can name any agent by id; "
        "delegating this is delegating agent:manage in every department at once"
    ),
    # ---- minting authority
    perm(ROLE, MANAGE): "a role that mints roles is a role that mints root",
    perm(MEMBER, MANAGE): (
        "assigns roles and seats; a Head of Sales who enrols himself in "
        "Engineering is the department boundary in a different coat"
    ),
    APPROVAL_VIEW_ANY: (
        "the unrestricted flag -- it is the words 'in every department', so a "
        "delegatable one would end the seat mechanism the day it was ticked"
    ),
    APPROVAL_DECIDE_ANY: (
        "the unrestricted flag on the deciding side; the same sentence as "
        "approval:view_any and the one that releases money"
    ),
    # ---- the whole tenant, out or in, at once
    BACKUP_EXPORT: (
        "an export is a readable copy of every transcript, every knowledge "
        "chunk and, with a passphrase, every credential the company holds"
    ),
    BACKUP_RESTORE: (
        "replaces every agent, run and knowledge chunk in the company with "
        "the contents of an uploaded file"
    ),
    # ---- every remaining `:manage`, which is decision C
    #
    # `require_permission` structurally cannot carry a resource -- it sees one
    # string -- so EVERY `:manage` permission in this catalogue is tenant-wide.
    # Handing an admin a builder whose output is tenant-wide `department:manage`
    # is exactly the flaw the seat vocabulary exists to close, and it does not
    # stop being that flaw because a caption underneath says so. Refused, not
    # badged. They graduate one at a time, as each route learns to resolve the
    # department of the thing it is about.
    **{
        perm(r, MANAGE): (
            "every :manage is tenant-wide, because the gate cannot carry a "
            "resource; graduates when this route resolves a department first"
        )
        for r in (
            AGENT,
            DEPARTMENT,
            KNOWLEDGE,
            BUDGET,
            SKILL,
            SUPERVISION,
            TRIGGER,
            FLOW,
            HANDOFF,
            CONTRACT,
            CHANNEL,
            RUN,
            APPROVAL,
        )
    },
    # ---- the agent's vocabulary, which is not a human right at all
    TOOL_READ: (
        "tool rights are the AGENT's term in the tool decision; a person never "
        "holds one, and a human-kind role carrying it would grant nothing anyway"
    ),
    TOOL_WRITE: (
        "tool rights are the AGENT's term in the tool decision; a person never "
        "holds one, and a human-kind role carrying it would grant nothing anyway"
    ),
    TOOL_SEND: (
        "tool rights are the AGENT's term in the tool decision; a person never "
        "holds one, and a human-kind role carrying it would grant nothing anyway"
    ),
}


def delegation_refusal(permission: str) -> str | None:
    """Why this permission may not be put in a tenant-defined role, or None.

    None for anything delegatable AND for anything that is not a permission at
    all -- callers validate membership of `ALL_PERMISSIONS` first, and a reason
    string invented for a typo would read as though the typo were a real right
    somebody nearly had.
    """
    if permission in NOT_YET_DELEGATABLE:
        return NOT_YET_DELEGATABLE[permission]
    return NEVER_DELEGATABLE.get(permission)


# ----------------------------------------------------------------- built-in roles

ORG_ADMIN: Final = "org_admin"
DEPT_MANAGER: Final = "dept_manager"
OPERATOR: Final = "operator"
AUDITOR: Final = "auditor"
AGENT_DEFAULT: Final = "agent_default"
#: The employee. Holds the empty set TENANT-WIDE and every bit of its authority
#: comes from seats, which is the whole of §0.A.
#:
#: It is a KEY here, mapped to `frozenset()`, rather than being left out and
#: relying on `permissions_for`'s fail-closed default. Both spellings answer the
#: same thing at every gate -- but only one of them lets `GET /governance` tell
#: `member` apart from `menber`, and that difference is a whole screen: with the
#: key absent, `callerRoleIsKnown` was False for the entire population this slice
#: creates and `/governance` -- the page the workspace's "you hold no seat" empty
#: state links to -- told every employee "this deployment does not define a role
#: called 'member', so it grants nothing at all… map your identity provider's
#: group to one of the roles below", whose remedy is the tenant-wide grant this
#: slice exists to avoid.
MEMBER_ROLE: Final = "member"

#: Three reads are NOT included in "may look at things", and each was caught by
#: a test rather than by foresight -- each is `org_admin`-only today, and the
#: rule this whole change rests on is that nothing gets wider than it already
#: is:
#:
#: * the secret store's metadata -- the list of which credentials a tenant holds
#:   is a map of where it can reach, worth more to an attacker than most of what
#:   it protects;
#: * the audit trail -- who read the log is itself an audit question, and
#:   `auditor` exists precisely so that reading it is a named, narrow grant
#:   rather than a side effect of being able to look at anything;
#: * the tenant-wide KPI span -- `GET /kpis` can name every agent and
#:   department in the tenant in one request (see `STATISTICS`'s own
#:   docstring, above), and the seat mechanism has no way to narrow a
#:   `require_permission`-gated route the way it narrows a
#:   `require_departmental` one, so a default grant here would be a default
#:   grant to see the WHOLE company's numbers, not just the caller's own
#:   departments.
_NOT_VIEWABLE_BY_DEFAULT: Final[frozenset[str]] = frozenset({SECRET, AUDIT, STATISTICS})

_VIEW_EVERYTHING: Final[frozenset[str]] = frozenset(
    p
    for p in ALL_PERMISSIONS
    if p.endswith(f":{VIEW}") and p.split(":", 1)[0] not in _NOT_VIEWABLE_BY_DEFAULT
)

#: Runs the office day to day: puts work in, steers it, signs off what an agent
#: is not allowed to do alone. Cannot re-configure the office itself -- no
#: plugins, no secrets, no budgets, no agent definitions. That split is the point
#: of having more than one role at all.
#:
#: What it does NOT gain is `approval:decide_any`. That is a real behaviour
#: change, and it is written down here rather than discovered later: an operator
#: keeps `approval:decide`, so he is still admitted at the door, but from this
#: slice on he decides only where he holds a seat. Nobody loses anything today --
#: `POST /auth/setup` mints `org_admin` and nothing else -- and the "you
#: hold no seat" empty state exists so that it reads as configuration rather than
#: as breakage.
_OPERATOR: Final[frozenset[str]] = _VIEW_EVERYTHING | {
    RUN_START,
    RUN_CONTROL,
    APPROVAL_DECIDE,
    CLARIFICATION_ANSWER,
    COPILOT_USE,
}

#: Everything an operator has, plus authority over the department's own shape:
#: its agents, their skills and triggers, its supervision policies and handoffs.
#: Still no plugins, secrets or organisation settings -- those are tenant-wide,
#: and a department manager is not the tenant.
_DEPT_MANAGER: Final[frozenset[str]] = _OPERATOR | {
    perm(AGENT, MANAGE),
    perm(DEPARTMENT, MANAGE),
    perm(SKILL, MANAGE),
    perm(TRIGGER, MANAGE),
    perm(SUPERVISION, MANAGE),
    perm(HANDOFF, MANAGE),
    perm(FLOW, MANAGE),
    perm(CONTRACT, MANAGE),
    perm(BUDGET, MANAGE),
}

#: Read-only, and the only role that reaches the audit trail. Deliberately holds
#: no `manage` and not even `run:start`: the value of an auditor is that its
#: account cannot have caused what it is auditing.
#:
#: `approval:view_any` and not `approval:decide_any`: an auditor who could only
#: see the departments he happens to hold a seat in could not audit the company,
#: and an auditor who could decide would be auditing his own decisions.
_AUDITOR: Final[frozenset[str]] = _VIEW_EVERYTHING | {
    perm(AUDIT, VIEW),
    AUDIT_VERIFY,
    APPROVAL_VIEW_ANY,
    COPILOT_USE,
}

#: An agent token is refused by the operator API as a whole
#: (`deny_agent_principals` in main.py), so this holds NO operator permission --
#: a statement rather than an oversight. What it does hold is all three TOOL
#: rights, which is what makes §5.3's role term inert for every agent that exists
#: today: an agent still reaches exactly what its department frame and its own
#: narrowing allow, and not one call more.
#:
#: The term becomes visible the moment a narrower role is assigned to an agent --
#: a role is reusable across agents, which is the one thing a per-agent narrowing
#: cannot be.
_AGENT_DEFAULT: Final[frozenset[str]] = frozenset({TOOL_READ, TOOL_WRITE, TOOL_SEND})

BUILTIN_ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    ORG_ADMIN: ALL_PERMISSIONS,
    DEPT_MANAGER: _DEPT_MANAGER,
    OPERATOR: _OPERATOR,
    AUDITOR: _AUDITOR,
    AGENT_DEFAULT: _AGENT_DEFAULT,
    # Every human role, including this one, holds copilot:use by default --
    # see COPILOT_USE's own docstring. This is the one thing that keeps
    # `member` from being the empty set: a bare member still holds nothing
    # that lets them see or change the OFFICE, but they can talk to it.
    MEMBER_ROLE: frozenset({COPILOT_USE}),
}


def permissions_for(role: str) -> frozenset[str]:
    """What this role may do. An unknown role holds nothing.

    Fail-closed on purpose, and the opposite of `required_right`'s fail-closed
    (which resolves an unknown TOOL to the more dangerous right). Here the
    unknown thing is the CALLER, and an unrecognised caller getting nothing is
    the safe direction. A role name that is a typo therefore 403s loudly instead
    of quietly admitting someone.

    An unmapped role name and a role mapped to the empty set are
    indistinguishable here on purpose: a role KNOWN to grant nothing must gate
    exactly like one nobody mapped. (`member` itself no longer grants nothing
    -- see `BUILTIN_ROLE_PERMISSIONS[MEMBER_ROLE]` -- but the principle still
    holds for any role that does.)
    """
    return BUILTIN_ROLE_PERMISSIONS.get(role, frozenset())


def role_has(role: str, permission: str) -> bool:
    return permission in permissions_for(role)


def role_kind(role: str) -> str:
    """Which population of the `role` TABLE a row with this name belongs to.

    One expression, in one place, because the alternative is the trap this
    column was added to close. `role` holds both humans and agents, and every
    writer of a row builds all five built-in roles in a `for` loop with no
    per-name discrimination -- so a loop that wrote `kind='human'` uniformly
    would produce an `agent_default` row of the wrong kind, and the moment the
    PDP starts reading the column that is every seeded agent silently granted no
    tool rights at all.

    `'human'` for everything else, including a name nobody recognises: a role
    the deployment cannot identify must not be the thing that hands an agent
    write access, which is the same fail-closed direction as `permissions_for`.
    """
    return "agent" if role == AGENT_DEFAULT else "human"


# ------------------------------------------------------------------------ seats

#: A seat is a person standing in ONE department, and its vocabulary is closed.
#:
#: The two seat roles below are the values `org_member_department.seat_role` may
#: hold; migration 0046 pins the same two words in a CHECK, so a seat naming a
#: built-in role is unrepresentable in the table rather than merely discouraged.
SEAT_VIEWER: Final = "dept_viewer"
SEAT_APPROVER: Final = "dept_approver"

#: What a seat carries -- and this dict is the whole reason the department
#: boundary is real.
#:
#: The failure it exists to prevent, verbatim from the two designs that died of
#: it: a "Head of Sales" was given the built-in `dept_manager` role, which is
#: `_OPERATOR` plus nine `:manage` grants and is TENANT-WIDE on every one of
#: them, because `require_permission` structurally cannot carry a resource. His
#: approvals list was scoped and nothing else was: he could rewrite Engineering's
#: department frame, retune Engineering's agents and read Engineering's
#: transcripts.
#:
#: So no built-in role name ever appears in a seat, and the four permissions here
#: are the only ones a seat can grant. Each is reachable ONLY through a route
#: that resolves a department before it answers. Adding a fifth is not a config
#: change: it means that route has been made department-aware first, and
#: `tests/authz/test_seat_vocabulary.py` is where that argument gets made.
SEAT_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    SEAT_VIEWER: frozenset(
        {perm(APPROVAL, VIEW), CLARIFICATION_VIEW, perm(AGENT, VIEW), perm(DEPARTMENT, VIEW)}
    ),
    SEAT_APPROVER: frozenset(
        {
            perm(APPROVAL, VIEW),
            CLARIFICATION_VIEW,
            perm(AGENT, VIEW),
            perm(DEPARTMENT, VIEW),
            APPROVAL_DECIDE,
            CLARIFICATION_ANSWER,
        }
    ),
}


#: The permissions that can be held IN ONE DEPARTMENT rather than tenant-wide --
#: the union of what the two seats carry, and nothing else.
#:
#: Derived from `SEAT_PERMISSIONS` rather than typed out again, because two
#: hand-written copies of the same four words are two things that can disagree.
#: It is the seam every `:manage` graduation plugs into: a permission joins this
#: set when its route resolves a department before it answers, and until then a
#: tenant-defined role holding it holds it everywhere.
DEPARTMENT_SCOPABLE: Final[frozenset[str]] = frozenset(
    p for granted in SEAT_PERMISSIONS.values() for p in granted
)


def seat_permissions_for(seat_role: str) -> frozenset[str]:
    """What this seat carries. An unknown seat role carries nothing.

    The same fail-closed rule as `permissions_for`, and for the same reason: the
    unknown thing is the CALLER's authority, so a seat_role that is a typo -- or
    one written by a future migration this code predates -- must refuse rather
    than admit. The CHECK constraint makes that unreachable through the API; this
    makes it harmless if it ever stops being unreachable.
    """
    return SEAT_PERMISSIONS.get(seat_role, frozenset())


#: The tool rights an agent has when no role is assigned to it. Every agent on
#: the live system is in this state, so this value is what "nothing changed" is
#: made of.
DEFAULT_AGENT_TOOL_RIGHTS: Final[frozenset[str]] = frozenset({"read", "write", "send"})


def tool_rights_for_role(role: str | None) -> frozenset[str]:
    """Which of read/write/send this role grants an agent (§5.3, first term).

    `None` means no role is assigned, which is every agent today, and resolves
    to `agent_default` -- all three rights, i.e. the frame and the narrowing keep
    deciding alone. An assigned but UNRECOGNISED role grants nothing, the same
    fail-closed rule operators get: a role name that is a typo must not be the
    thing that hands an agent write access.

    A human role assigned to an agent therefore yields no tool rights at all.
    That is deliberate rather than an omission: `operator` and `auditor` describe
    what a PERSON may do in the control plane, and silently reading them as
    permission to act on a customer's systems is exactly the kind of quiet
    widening this whole layer exists to prevent.
    """
    if role is None:
        role = AGENT_DEFAULT
    granted = permissions_for(role)
    return frozenset(right for right, p in _TOOL_RIGHTS.items() if p in granted)
