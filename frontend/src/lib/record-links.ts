// The web UI's own check on record links (ruling R9 of the record-links
// plan). The server already unlinks what oc8 never issued, but it reads
// Markdown with regexes; ChatMarkdown asks about the hrefs its parser actually
// produced, so what is clickable is exactly what was checked.

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

const keys = {
  hosts: ["record-links", "hosts"] as const,
  verified: (urls: string[]) => ["record-links", "verified", urls] as const,
};

/** Hosts of the tenant's connected systems: a link there is a record link and
 *  is only shown once verified. Changes when a connection does, so rarely. */
export function useConnectedHosts() {
  return useQuery({
    queryKey: keys.hosts,
    queryFn: async () => new Set((await api.get<{ hosts: string[] }>("/record-links/hosts")).hosts),
    staleTime: 5 * 60_000,
  });
}

// What the verify endpoint accepts at once; links beyond it stay unverified.
const MAX_VERIFY = 100;
const MAX_URL_CHARS = 2000;

/** Which of `urls` oc8 issued, as the same strings. Keyed by the sorted
 *  unique list; nothing is asked for an empty one. A longer list keeps the
 *  previous answer meanwhile -- every URL in it was verified, so it is safe. */
export function useVerifiedLinks(urls: string[]) {
  const wanted = [...new Set(urls)]
    .filter((u) => u.length <= MAX_URL_CHARS)
    .sort()
    .slice(0, MAX_VERIFY);
  return useQuery({
    queryKey: keys.verified(wanted),
    queryFn: async () =>
      new Set(
        (await api.post<{ issued: string[] }>("/record-links/verify", { urls: wanted })).issued,
      ),
    enabled: wanted.length > 0,
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
}
