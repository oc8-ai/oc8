import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Connection } from "@/lib/connections";
import { useT } from "@/lib/i18n";
import { RowMenu } from "./connection-row";
import { isBroken, shortDate } from "./texts";

const MEMBERS_PAGE_SIZE = 25;

type StatusFilter = "" | "active" | "needs_reauth";

const SELECT =
  "h-9 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** Every member's own connections, for an administrator: searchable,
 *  filterable and paged on the client so a large organisation stays usable. */
export function MemberConnections({
  rows,
  labelOf,
  lang,
  onRevoke,
}: {
  rows: readonly Connection[];
  labelOf: (c: Connection) => string;
  lang: string;
  onRevoke: (c: Connection) => void;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [app, setApp] = useState("");
  const [status, setStatus] = useState<StatusFilter>("");
  const [page, setPage] = useState(0);

  const apps = useMemo(
    () => [...new Set(rows.map(labelOf).filter(Boolean))].sort((a, b) => a.localeCompare(b)),
    [rows, labelOf],
  );
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter((c) => {
      if (app && labelOf(c) !== app) return false;
      if (status === "active" && isBroken(c)) return false;
      if (status === "needs_reauth" && !isBroken(c)) return false;
      if (!needle) return true;
      return (
        (c.ownerName ?? "").toLowerCase().includes(needle) ||
        c.accountLabel.toLowerCase().includes(needle)
      );
    });
  }, [rows, labelOf, query, app, status]);

  if (rows.length === 0) {
    return (
      <p className="py-4 text-sm text-muted-foreground">
        {t(
          "No member has connected an account yet.",
          "Noch kein Mitglied hat ein Konto verbunden.",
        )}
      </p>
    );
  }

  const pages = Math.max(1, Math.ceil(filtered.length / MEMBERS_PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const start = current * MEMBERS_PAGE_SIZE;
  const visible = filtered.slice(start, start + MEMBERS_PAGE_SIZE);
  const end = start + visible.length;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[200px] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            aria-label={t("Search person or account", "Person oder Konto suchen")}
            placeholder={t("Search person or account …", "Person oder Konto suchen …")}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(0);
            }}
            className="pl-8"
          />
        </div>
        <select
          aria-label={t("App", "App")}
          className={SELECT}
          value={app}
          onChange={(e) => {
            setApp(e.target.value);
            setPage(0);
          }}
        >
          <option value="">{t("All apps", "Alle Apps")}</option>
          {apps.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <select
          aria-label={t("Status", "Status")}
          className={SELECT}
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as StatusFilter);
            setPage(0);
          }}
        >
          <option value="">{t("Any status", "Jeder Status")}</option>
          <option value="active">{t("Active", "Aktiv")}</option>
          <option value="needs_reauth">{t("Needs reconnecting", "Erneut verbinden nötig")}</option>
        </select>
      </div>

      {filtered.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground">
          {t("No connection matches.", "Keine Verbindung passt zur Suche.")}
        </p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th scope="col" className="py-2 pr-3 font-medium">
                    {t("Person", "Person")}
                  </th>
                  <th scope="col" className="py-2 pr-3 font-medium">
                    {t("App", "App")}
                  </th>
                  <th scope="col" className="py-2 pr-3 font-medium">
                    {t("Account", "Konto")}
                  </th>
                  <th scope="col" className="py-2 pr-3 font-medium">
                    {t("Since", "Seit")}
                  </th>
                  <th scope="col" className="py-2">
                    <span className="sr-only">{t("Actions", "Aktionen")}</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {visible.map((c) => {
                  const who = c.ownerName ?? t("This member", "Dieses Mitglied");
                  return (
                    <tr key={c.id}>
                      <td className="py-2 pr-3 font-medium">{who}</td>
                      <td className="py-2 pr-3">{labelOf(c)}</td>
                      <td className="py-2 pr-3">
                        <span className="break-all">{c.accountLabel}</span>
                        {isBroken(c) && (
                          <span className="ml-2 text-xs text-amber-600 dark:text-amber-400">
                            {t("Needs reconnecting", "Erneut verbinden nötig")}
                          </span>
                        )}
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap text-muted-foreground">
                        {shortDate(c.createdAt, lang)}
                      </td>
                      <td className="py-1 text-right">
                        <RowMenu
                          label={t(
                            `Actions for ${who} (${c.accountLabel})`,
                            `Aktionen für ${who} (${c.accountLabel})`,
                          )}
                          items={[
                            {
                              label: t("Revoke", "Widerrufen"),
                              destructive: true,
                              onSelect: () => onRevoke(c),
                            },
                          ]}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
            <span aria-live="polite">
              {t(
                `${start + 1}–${end} of ${filtered.length}`,
                `${start + 1}–${end} von ${filtered.length}`,
              )}
            </span>
            {pages > 1 && (
              <>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8"
                  aria-label={t("Previous page", "Vorherige Seite")}
                  disabled={current === 0}
                  onClick={() => setPage(current - 1)}
                >
                  <ChevronLeft />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8"
                  aria-label={t("Next page", "Nächste Seite")}
                  disabled={current >= pages - 1}
                  onClick={() => setPage(current + 1)}
                >
                  <ChevronRight />
                </Button>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
