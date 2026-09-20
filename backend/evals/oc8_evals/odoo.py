"""Thin XML-RPC client for state assertions and seeding against the real
Odoo dev stack. Credentials come from the same `resolve_mcp_env` the agent's
own bridge uses, so the eval reads exactly the system the agent wrote to."""

from __future__ import annotations

import xmlrpc.client
from typing import Any


class Odoo:
    def __init__(self, *, url: str, db: str, user: str, api_key: str) -> None:
        self.url, self.db, self.user, self.api_key = url.rstrip("/"), db, user, api_key
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        uid = common.authenticate(db, user, api_key, {})
        if not uid:
            raise RuntimeError(f"Odoo authentication failed for {user}@{db}")
        self.uid = int(uid)  # type: ignore[arg-type]
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Odoo:
        return cls(
            url=env["ODOO_URL"],
            db=env["ODOO_DB"],
            user=env["ODOO_USER"],
            api_key=env.get("ODOO_API_KEY") or env["ODOO_PASSWORD"],
        )

    def _call(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._models.execute_kw(
            self.db, self.uid, self.api_key, model, method, list(args), kwargs
        )

    def search_read(
        self, model: str, domain: list[Any], fields: list[str], limit: int | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields}
        if limit is not None:
            kwargs["limit"] = limit
        result = self._call(model, "search_read", domain, **kwargs)
        return list(result)

    def create(self, model: str, values: dict[str, Any]) -> int:
        return int(self._call(model, "create", values))

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        return bool(self._call(model, "write", ids, values))

    def unlink(self, model: str, ids: list[int]) -> bool:
        return bool(self._call(model, "unlink", ids)) if ids else True

    def count(self, model: str, domain: list[Any]) -> int:
        return int(self._call(model, "search_count", domain))


def eval_prefix(run_tag: str) -> str:
    return f"EVAL-{run_tag}-"


def teardown_by_prefix(odoo: Odoo, prefix: str, models: tuple[str, ...]) -> None:
    """Delete every record whose name starts with the prefix, model by model, in
    the given order (children before parents, e.g. sale.order before res.partner)."""
    for model in models:
        rows = odoo.search_read(model, [("name", "ilike", prefix + "%")], ["id"])
        odoo.unlink(model, [int(r["id"]) for r in rows])
