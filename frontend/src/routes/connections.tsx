import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";
import {
  CONNECTIONS_TABS,
  ConnectionsPage,
  type ConnectionsSearch,
  type ConnectionsTab,
} from "@/components/connections/connections-page";

const str = (value: unknown) => (typeof value === "string" && value ? value : undefined);
// "mine" is the default tab and never written to the URL.
const tabOf = (value: unknown): ConnectionsTab | undefined =>
  CONNECTIONS_TABS.find((tab) => tab !== "mine" && tab === value);

export const Route = createFileRoute("/connections")({
  validateSearch: (search: Record<string, unknown>): ConnectionsSearch => ({
    pending: str(search.pending),
    error: str(search.error),
    provider: str(search.provider),
    connect: str(search.connect),
    tab: tabOf(search.tab),
  }),
  // The OAuth return carries a one-time id in the URL: never pass it on as
  // a Referer (the page also sets this while mounted).
  head: () => ({ meta: [{ name: "referrer", content: "no-referrer" }] }),
  component: ConnectionsRoute,
});

const tabSearch = (tab: ConnectionsTab | undefined) => (tab && tab !== "mine" ? { tab } : {});

export function ConnectionsRoute() {
  const search = Route.useSearch();
  const navigate = useNavigate();
  const tab = search.tab;
  // Cleans the one-time return values out of the URL, keeping the open tab.
  const consume = useCallback(() => {
    void navigate({ to: "/connections", search: tabSearch(tab), replace: true });
  }, [navigate, tab]);
  const changeTab = useCallback(
    (next: ConnectionsTab) => {
      void navigate({ to: "/connections", search: tabSearch(next), replace: true });
    },
    [navigate],
  );
  return <ConnectionsPage search={search} onConsumeSearch={consume} onTabChange={changeTab} />;
}
