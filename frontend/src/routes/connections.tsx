import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";
import { ConnectionsPage, type ConnectionsSearch } from "@/components/connections/connections-page";

const str = (value: unknown) => (typeof value === "string" && value ? value : undefined);

export const Route = createFileRoute("/connections")({
  validateSearch: (search: Record<string, unknown>): ConnectionsSearch => ({
    pending: str(search.pending),
    error: str(search.error),
    provider: str(search.provider),
    connect: str(search.connect),
  }),
  // The OAuth return carries a one-time id in the URL: never pass it on as
  // a Referer (the page also sets this while mounted).
  head: () => ({ meta: [{ name: "referrer", content: "no-referrer" }] }),
  component: ConnectionsRoute,
});

export function ConnectionsRoute() {
  const search = Route.useSearch();
  const navigate = useNavigate();
  const consume = useCallback(() => {
    void navigate({ to: "/connections", search: {}, replace: true });
  }, [navigate]);
  return <ConnectionsPage search={search} onConsumeSearch={consume} />;
}
