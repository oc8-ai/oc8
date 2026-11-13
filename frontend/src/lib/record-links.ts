// The web UI's own check on record links (rulings R9 and R11 of the
// record-links plan). The server already unlinks what oc8 never issued, but
// it reads Markdown with regexes; ChatMarkdown asks about the hrefs its parser
// actually produced, so what is clickable is exactly what was checked. The
// check is bound to one saved answer: the server judges only URLs that answer
// contains, for a caller who may read it.

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface LinkVerdict {
  url: string;
  /** Points at a connected system: a record link, shown only if issued. */
  connected: boolean;
  issued: boolean;
}

// What the verify endpoint accepts at once; links beyond it stay unverified.
const MAX_VERIFY = 100;
const MAX_URL_CHARS = 2000;

function browserHost(url: string): string {
  try {
    return new URL(url).hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return "";
  }
}

/** The verdict on each of `urls` in the saved answer `messageId`, keyed by
 *  the same strings. The query key is the message and the sorted unique list;
 *  nothing is asked without a message or for an empty list. A saved answer
 *  does not change, so its verdicts are fetched once: no refetch on focus, no
 *  staleness. While a longer list loads, the shorter list's verdicts for the
 *  same message stay. */
export function useVerifiedLinks(messageId: string | undefined, urls: string[]) {
  const wanted = [...new Set(urls)]
    .filter((u) => u.length <= MAX_URL_CHARS)
    .sort()
    .slice(0, MAX_VERIFY);
  return useQuery({
    queryKey: ["record-links", "verified", messageId ?? null, wanted],
    queryFn: async () => {
      // With each URL, the host this browser will open: the server checks it
      // too, so a host that Python and the browser read differently (an IP
      // spelled `0xC0A8010A`, an IDNA corner) still counts as connected.
      const body = { messageId, links: wanted.map((url) => ({ url, host: browserHost(url) })) };
      const { links } = await api.post<{ links: LinkVerdict[] }>("/record-links/verify", body);
      return new Map(links.map((v) => [v.url, v]));
    },
    enabled: !!messageId && wanted.length > 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: 1,
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[2] === (messageId ?? null) ? previous : undefined,
  });
}
