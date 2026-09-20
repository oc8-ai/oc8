// Pull-up console for a "Test connection" run -- the granular spawn/
// handshake/list_tools/result steps the connection's own `health` field
// (ok/error + a single summary line) was never meant to carry. Modeled on
// agents.$id.tsx's LiveTranscriptPane (same terminal look, same
// stick-to-bottom-unless-scrolled-up behaviour) and fed by the same kind of
// live event: mcp.test.log, appended into the cache by apply-event.ts as the
// backend works through McpSession's steps (see realtime/emit.py's
// publish_mcp_test_log).
//
// Deliberately per-instance rather than one app-wide singleton: an operator
// tests one connection at a time, and each call site (custom-mcp-wizard.tsx,
// mcp-connections.tsx) renders its own drawer scoped to the connection it is
// currently testing. Testing two connections at once would show two docked
// bars -- an edge case not worth a shared coordinator for.
import { ChevronDown, ChevronUp } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { mcpTestLogKey } from "@/lib/live/apply-event";
import { useMcpTestLog } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const STEP_LABEL: Record<string, string> = {
  spawn: "spawn",
  handshake: "handshake",
  list_tools: "list_tools",
  result: "result",
};

export function McpTestLogDrawer({
  connectionId,
  busy,
}: {
  connectionId: string;
  /** True while this connection's test mutation is in flight -- opens the
   * drawer and resets its log the moment a NEW run starts. */
  busy: boolean;
}) {
  const t = useT();
  const qc = useQueryClient();
  const { data: lines } = useMcpTestLog(connectionId);
  const [open, setOpen] = useState(false);
  // Deliberately NOT initialized to `busy`: custom-mcp-wizard.tsx only learns
  // its connectionId (and so only mounts this drawer) once
  // installEnableConfigure resolves, by which point its own `busy` is
  // already true -- starting the ref at `false` means that first run's
  // false->true edge still fires below instead of being missed at mount.
  const wasBusy = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  useEffect(() => {
    if (busy && !wasBusy.current) {
      qc.setQueryData(mcpTestLogKey(connectionId), []);
      setOpen(true);
    }
    wasBusy.current = busy;
  }, [busy, connectionId, qc]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines]);

  if (lines.length === 0 && !busy) return null;

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
  }

  return (
    <div className="fixed inset-x-0 bottom-0 z-[60] flex justify-center px-4 pb-4">
      <div className="w-full max-w-2xl overflow-hidden rounded-t-lg border border-border bg-background shadow-xl">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex w-full items-center gap-1.5 px-3 py-2 text-xs font-medium text-muted-foreground transition hover:text-foreground"
        >
          {busy && (
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[color:var(--status-running)]">
              <span className="absolute inset-0 animate-ping rounded-full bg-[color:var(--status-running)] opacity-60" />
            </span>
          )}
          {t("Connection log", "Verbindungs-Log")}
          <span className="text-[10px] text-muted-foreground/70">({lines.length})</span>
          <span className="ml-auto">
            {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
          </span>
        </button>
        {open && (
          <div
            ref={scrollRef}
            onScroll={onScroll}
            className="max-h-[240px] overflow-auto border-t border-border bg-black/90 p-3 font-mono text-[11px] leading-relaxed text-emerald-300"
          >
            {lines.map((line, i) => {
              const isError = line.step === "result" && line.message.startsWith("Error:");
              return (
                <div key={i} className={cn(isError && "text-[color:var(--status-error)]")}>
                  <span className="text-emerald-500/60">
                    [{STEP_LABEL[line.step] ?? line.step}]
                  </span>{" "}
                  {line.message}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
