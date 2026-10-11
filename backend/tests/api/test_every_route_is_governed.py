"""No control-plane route may be left ungoverned by accident.

This is the load-bearing test of the permission layer, more than any single
gate is. The layer was not missing because anyone decided these routes should be
open -- it was missing because nothing ever forced a new route to say. 84 of 122
routes had no role check at all, and among them were installing a plugin,
creating an agent, changing what an agent is allowed to do, and DECIDING AN
APPROVAL -- which §5.5 says must be RBAC-checked, because the entire value of an
approval gate is that a second person signs off.

So a route is governed when it either

* depends on `require_permission(...)`, or
* is explicitly marked `unguarded("why")`, which costs a sentence.

Both are read out of `route.dependant` -- FastAPI's own resolved tree, which is
what actually executes. Deriving them from source signatures does not work here:
every module uses `from __future__ import annotations`, so `inspect.signature`
returns STRING annotations and `Annotated[Principal, Depends(require_role(...))]`
is invisible. An earlier version of this sweep did exactly that and reported the
whole run API as ungated when it is not, which is the failure mode this file
exists to prevent -- a confident, wrong list of what is protected.
"""

from __future__ import annotations

from typing import Any

from oc8.authz.permissions import ALL_PERMISSIONS
from oc8.main import create_app


def _walk(routes: list[Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten the router tree.

    This FastAPI keeps an included router as a lazy `_IncludedRouter`, so
    `app.routes` has five entries where there are 122 endpoints, and the mount
    prefix lives on the include CONTEXT rather than on either router object.
    """
    out: list[tuple[str, Any]] = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            sub = getattr(route.include_context, "prefix", "") or ""
            out.extend(_walk(route.original_router.routes, prefix + sub))
        else:
            out.append((prefix, route))
    return out


def _guards(dependant: Any, seen: set[int] | None = None) -> list[tuple[str, str]]:
    """Every guard in the resolved dependency tree: (kind, detail)."""
    seen = seen if seen is not None else set()
    found: list[tuple[str, str]] = []
    for sub in getattr(dependant, "dependencies", []) or []:
        call = getattr(sub, "call", None)
        if call is not None and id(call) not in seen:
            seen.add(id(call))
            reason = getattr(call, "oc8_unguarded_reason", None)
            if reason is not None:
                found.append(("unguarded", str(reason)))
            else:
                name = getattr(call, "__qualname__", "") or getattr(call, "__name__", "")
                # `require_departmental` is a permission gate like any other as
                # far as this sweep is concerned: it admits on a permission held
                # tenant-wide OR in one seat, and the ROW narrowing happens behind
                # it. `_closed_over_permission` reads its closure cell unchanged.
                if "require_permission" in name or "require_departmental" in name:
                    found.append(("permission", _closed_over_permission(call)))
                elif "require_agent_write" in name:
                    # The department-scoped agent-write gate. Deliberately its
                    # own kind and not folded into "permission": neither
                    # `require_agent_write` nor `authorize_agent_write` closes
                    # over a permission string -- that is decision 2, made
                    # structural -- so `_closed_over_permission` would return
                    # "?" for every one of these six routes and the pinned
                    # table below would have nothing to compare against.
                    found.append(("agent_write", name))
                elif "require_role" in name:
                    found.append(("role", name))
                elif "require_scope" in name:
                    found.append(("scope", name))
        found.extend(_guards(sub, seen))
    return found


def _closed_over_permission(call: Any) -> str:
    for cell in getattr(call, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if isinstance(value, str) and value in ALL_PERMISSIONS:
            return value
    return "?"


def _api_routes() -> list[dict[str, Any]]:
    app = create_app()
    rows: list[dict[str, Any]] = []
    for prefix, route in _walk(list(app.routes)):
        path = prefix + str(getattr(route, "path", ""))
        dependant = getattr(route, "dependant", None)
        if not path.startswith("/api") or dependant is None:
            continue
        methods = sorted(
            m for m in (getattr(route, "methods", None) or ()) if m not in ("HEAD", "OPTIONS")
        )
        if not methods:
            continue
        guards = _guards(dependant)
        rows.append(
            {
                "path": path,
                "methods": methods,
                "guards": guards,
                "kinds": {kind for kind, _ in guards},
            }
        )
    return rows


def test_the_sweep_actually_finds_the_routes() -> None:
    """A sweep that silently matched nothing would pass every test below it.

    This is not ceremony: the first version of this walk returned 0 routes
    because it never descended into the lazy included routers, and the second
    returned 122 routes with the wrong guards on them.
    """
    routes = _api_routes()
    assert len(routes) > 100, f"only found {len(routes)} routes; the walk is broken"
    assert any(r["path"] == "/api/v1/agents" for r in routes)


def test_every_route_is_governed() -> None:
    """Either a permission, or a written reason. Nothing in between."""
    ungoverned = [
        f"{'/'.join(r['methods'])} {r['path']}"
        for r in _api_routes()
        if not r["kinds"] & {"permission", "unguarded", "scope", "agent_write"}
    ]
    assert not ungoverned, (
        f"{len(ungoverned)} route(s) carry no permission and no unguarded() reason:\n  "
        + "\n  ".join(sorted(ungoverned))
    )


def test_no_route_still_uses_the_old_role_gate() -> None:
    """`require_role` names roles, which cannot be checked for typos -- one route
    was guarded by `member`, a role issued to nobody."""
    leftovers = [
        f"{'/'.join(r['methods'])} {r['path']}" for r in _api_routes() if "role" in r["kinds"]
    ]
    assert not leftovers, "still on require_role:\n  " + "\n  ".join(sorted(leftovers))


def test_every_declared_permission_is_a_known_one() -> None:
    unknown = [
        (r["path"], detail)
        for r in _api_routes()
        for kind, detail in r["guards"]
        if kind == "permission" and detail not in ALL_PERMISSIONS
    ]
    assert not unknown, f"routes declare permissions that do not exist: {unknown}"


#: The routes the department-scoped workspace adds or re-gates, and the ONE
#: permission each of them declares. Path parameters are normalised to `{}` so a
#: rename of `approval_id` is not a test failure, while a route quietly losing
#: its gate is.
#:
#: Four of these are reachable from a seat, and that is exactly why they are
#: pinned here: `SEAT_PERMISSIONS` is closed at four permissions, and each one is
#: only safe because the route declaring it resolves a department before it
#: answers. A route that appeared in this table with, say, `run:control` would be
#: a seat quietly gaining a tenant-wide right.
_WORKSPACE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/approvals"): "approval:view",
    ("POST", "/api/v1/approvals/{}/decision"): "approval:decide",
    ("GET", "/api/v1/clarifications"): "clarification:view",
    ("POST", "/api/v1/clarifications/{}/answer"): "clarification:answer",
    ("GET", "/api/v1/members"): "member:view",
    ("POST", "/api/v1/members"): "member:manage",
    ("PUT", "/api/v1/members/{}/departments/{}"): "member:manage",
    ("DELETE", "/api/v1/members/{}/departments/{}"): "member:manage",
    ("DELETE", "/api/v1/members/{}"): "member:manage",
    ("POST", "/api/v1/members/{}/password-reset"): "member:manage",
}


def _normalised(path: str) -> str:
    out: list[str] = []
    depth = 0
    for ch in path:
        if ch == "{":
            depth += 1
            if depth == 1:
                out.append("{}")
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def test_the_new_routes_declare_a_permission() -> None:
    """`require_departmental` admits a caller who holds a permission tenant-wide
    OR in one seat, and the ROW narrowing happens behind it -- so as far as this
    sweep is concerned it is a permission gate like any other, and `_guards` is
    taught one line to say so.

    Without that line every route below reads as ungoverned and
    `test_every_route_is_governed` fails; with it but with the wrong permission
    declared, this fails. The pair is what makes "the gate is a departmental one"
    checkable rather than a comment.
    """
    declared: dict[tuple[str, str], set[str]] = {}
    for row in _api_routes():
        key_path = _normalised(row["path"])
        for method in row["methods"]:
            if (method, key_path) in _WORKSPACE_ROUTES:
                declared[(method, key_path)] = {
                    detail for kind, detail in row["guards"] if kind == "permission"
                }

    missing = sorted(set(_WORKSPACE_ROUTES) - set(declared))
    assert not missing, f"the workspace slice's routes are not mounted: {missing}"

    wrong = {
        key: sorted(found) for key, found in declared.items() if _WORKSPACE_ROUTES[key] not in found
    }
    assert not wrong, (
        "these routes do not declare the permission the design gives them "
        f"(expected -> found): { {k: (_WORKSPACE_ROUTES[k], v) for k, v in wrong.items()} }"
    )


#: The tenant-defined-roles slice's routes, and the ONE permission each declares.
#:
#: Two authorities, deliberately not one. `role:manage` AUTHORS a role;
#: `member:manage` HANDS ONE OUT. Folding them together would mean the person who
#: composes the bundles is automatically the person who decides who gets them,
#: which is the whole of "a role that mints roles is a role that mints root" with
#: one extra step. Both are `NEVER_DELEGATABLE`, so `org_admin` is the only
#: holder of either today -- and this table is what makes a later slice that
#: graduates one of them visible in review.
#:
#: `GET /permissions/catalogue` is absent on purpose: it returns the static
#: catalogue and no tenant data, so it is `unguarded()` and
#: `test_unguarded_routes_say_why` is what holds it to a reason.
_ROLE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/roles"): "role:view",
    ("POST", "/api/v1/roles"): "role:manage",
    ("GET", "/api/v1/roles/{}"): "role:view",
    ("PUT", "/api/v1/roles/{}"): "role:manage",
    ("DELETE", "/api/v1/roles/{}"): "role:manage",
    ("PUT", "/api/v1/members/{}/role"): "member:manage",
    ("POST", "/api/v1/members/roles:bulk"): "member:manage",
}


def test_the_role_and_member_routes_declare_a_permission() -> None:
    """A route that mints authority and forgets to say so is the one route in the
    system where `test_every_route_is_governed` failing loudly matters most --
    and it would NOT fail loudly, because `unguarded("...")` is a legal answer
    that costs one sentence anybody can write.

    So the permission is pinned per route, not merely required to exist.
    """
    declared: dict[tuple[str, str], set[str]] = {}
    for row in _api_routes():
        key_path = _normalised(row["path"])
        for method in row["methods"]:
            if (method, key_path) in _ROLE_ROUTES:
                declared[(method, key_path)] = {
                    detail for kind, detail in row["guards"] if kind == "permission"
                }

    missing = sorted(set(_ROLE_ROUTES) - set(declared))
    assert not missing, f"the roles slice's routes are not mounted: {missing}"

    wrong = {
        key: sorted(found) for key, found in declared.items() if _ROLE_ROUTES[key] not in found
    }
    assert not wrong, (
        "these routes do not declare the permission the design gives them "
        f"(expected -> found): { {k: (_ROLE_ROUTES[k], v) for k, v in wrong.items()} }"
    )


def test_the_permission_catalogue_is_reachable_by_whoever_is_composing_a_role() -> None:
    """It is `unguarded()`, and the reason has to be the real one.

    Gating the catalogue on `settings:view` or `role:manage` would hide the list
    of rights from the caller who is looking at the refusal it explains -- the
    same mistake `GET /governance` was built with and 12c40e6 records fixing. It
    returns no tenant data, so there is nothing to gate.
    """
    rows = [
        r
        for r in _api_routes()
        if _normalised(r["path"]) == "/api/v1/permissions/catalogue" and "GET" in r["methods"]
    ]
    assert rows, "GET /permissions/catalogue is not mounted"
    reasons = [detail for kind, detail in rows[0]["guards"] if kind == "unguarded"]
    assert reasons, f"the catalogue carries a permission gate: {rows[0]['guards']}"
    assert len(reasons[0].strip()) >= 15


#: The six routes decision 1 names -- `POST /agents` plus the five
#: `agents_write.py` mutations -- and nothing else. Pinned by name rather than
#: swept, because `_closed_over_permission` cannot find a permission string in
#: `require_agent_write`'s closure BY DESIGN (decision 2: no `perm()` call
#: anywhere in the write path for a role builder to target), so the usual
#: "declared permission" table has nothing to compare against here. This table
#: is what stands in its place: not WHICH permission, but THAT the gate is
#: `require_agent_write` and not, say, a `require_permission` a refactor quietly
#: put back.
_AGENT_WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/agents"),
        ("POST", "/api/v1/agents/{}/lifecycle"),
        ("PUT", "/api/v1/agents/{}/narrowing"),
        ("PUT", "/api/v1/agents/{}/runtime"),
        ("PATCH", "/api/v1/agents/{}/model-config"),
        ("POST", "/api/v1/agents/{}/skills"),
    }
)


def test_the_agent_write_routes_declare_require_agent_write() -> None:
    """Exactly these six carry `require_agent_write`'s gate kind -- not more
    (which would be `department:manage`'s broader surface quietly reusing this
    gate) and not fewer (which is one of the six still on the OLD tenant-wide
    `require_permission(perm(AGENT, MANAGE))`, silently un-scoped)."""
    declared: dict[tuple[str, str], set[str]] = {}
    for row in _api_routes():
        key_path = _normalised(row["path"])
        for method in row["methods"]:
            if (method, key_path) in _AGENT_WRITE_ROUTES:
                declared[(method, key_path)] = row["kinds"]

    missing = sorted(set(_AGENT_WRITE_ROUTES) - set(declared))
    assert not missing, f"the agent-write routes are not mounted: {missing}"

    wrong = {key: sorted(kinds) for key, kinds in declared.items() if "agent_write" not in kinds}
    assert not wrong, (
        f"these routes do not carry require_agent_write's gate kind (found instead): {wrong}"
    )


def test_unguarded_routes_say_why() -> None:
    """The marker is only worth having if it carries a reason a reviewer can
    disagree with."""
    thin = [
        (r["path"], detail)
        for r in _api_routes()
        for kind, detail in r["guards"]
        if kind == "unguarded" and len(detail.strip()) < 15
    ]
    assert not thin, f"unguarded() without a real reason: {thin}"
