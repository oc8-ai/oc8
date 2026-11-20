import { createFileRoute, redirect } from "@tanstack/react-router";

// Absorbed into /connections (connections design): kept as a redirect so a
// bookmark or an old link still lands somewhere real.
export const Route = createFileRoute("/credentials")({
  beforeLoad: () => {
    throw redirect({ to: "/connections" });
  },
});
