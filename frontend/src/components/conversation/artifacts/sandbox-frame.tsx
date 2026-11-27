// A frame for untrusted HTML (an artifact's preview; Run Python reuses it).
//
// `sandbox="allow-scripts"` and nothing else: no `allow-same-origin`, so the
// page runs in an opaque origin -- no cookies, no storage, no oc8 API, no
// access to this page's DOM; no `allow-top-navigation`, `allow-popups`,
// `allow-forms` or `allow-modals`. The CSP in its srcdoc (see
// sandbox-srcdoc.ts) gives it no network. It reaches the parent only by
// postMessage, and only messages from this frame's own window are passed on.
//
// The sandbox does not forbid the frame navigating itself (a link,
// `location = …`); the app's `frame-src 'none'` does, before the request
// (server/frame-ancestors.ts). As a second line, a second load of the same
// document means it navigated anyway: the frame is taken away, so nothing
// more is shown from, or sent to, a page that is not the preview.

import { useEffect, useMemo, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { buildSandboxSrcdoc } from "./sandbox-srcdoc";

export function SandboxFrame({
  html,
  onMessage,
  extraCsp,
  title,
  className,
}: {
  html: string;
  /** A message the frame posted (`event.data`). */
  onMessage?: (data: unknown) => void;
  /** See `sandboxCsp`. */
  extraCsp?: string;
  title?: string;
  className?: string;
}) {
  const t = useT();
  const frame = useRef<HTMLIFrameElement>(null);
  const srcdoc = useMemo(() => buildSandboxSrcdoc(html, extraCsp), [html, extraCsp]);
  // The document the frame has loaded once; a second load of it is a
  // navigation away from it.
  const loaded = useRef<string | null>(null);
  const [navigatedFrom, setNavigatedFrom] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const navigated = navigatedFrom === srcdoc;
  const latest = useRef(onMessage);
  latest.current = onMessage;

  useEffect(() => {
    function receive(event: MessageEvent) {
      const own = frame.current?.contentWindow;
      if (!own || event.source !== own) return;
      latest.current?.(event.data);
    }
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, []);

  if (navigated) {
    return (
      <div
        role="status"
        className={cn(
          "flex flex-col items-center justify-center gap-2 p-4 text-center text-xs text-muted-foreground",
          className,
        )}
      >
        <p>
          {t(
            "The preview tried to open another page and was stopped.",
            "Die Vorschau wollte eine andere Seite öffnen und wurde angehalten.",
          )}
        </p>
        <button
          type="button"
          onClick={() => {
            loaded.current = null;
            setNavigatedFrom(null);
            setAttempt((n) => n + 1);
          }}
          className="inline-flex min-h-9 items-center gap-1 rounded-md border border-border px-2 font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <RotateCcw className="size-3.5" aria-hidden />
          {t("Show the preview again", "Vorschau neu laden")}
        </button>
      </div>
    );
  }

  return (
    <iframe
      key={attempt}
      ref={frame}
      title={title ?? t("Preview", "Vorschau")}
      sandbox="allow-scripts"
      srcDoc={srcdoc}
      referrerPolicy="no-referrer"
      onLoad={() => {
        if (loaded.current === srcdoc) setNavigatedFrom(srcdoc);
        else loaded.current = srcdoc;
      }}
      // A page draws on the browser's default canvas, as it would opened on
      // its own -- not on the chat's (dark) background.
      style={{ colorScheme: "light", backgroundColor: "Canvas" }}
      className={cn("block border-0", className)}
    />
  );
}
