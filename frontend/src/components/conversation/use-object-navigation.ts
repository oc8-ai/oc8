// An object chip ("oc8://run/<id>" -> "/activity?run=<id>") opens in-app:
// the router takes the whole href, search included, so the target route's
// validateSearch selects the object. Outside a router (a test, an embed)
// there is nothing to hand it to and the chip stays a plain link.
import { useMemo } from "react";
import { useRouter } from "@tanstack/react-router";

export function useObjectNavigation(): ((href: string) => void) | undefined {
  const router = useRouter({ warn: false }) as ReturnType<typeof useRouter> | null;
  return useMemo(
    () => (router ? (href: string) => void router.navigate({ href }) : undefined),
    [router],
  );
}
