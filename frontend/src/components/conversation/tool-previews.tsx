// One preview per kind of step (spec §2.1 StepList/ToolPreview). A fixed,
// compiled registry -- like RUN_COMPONENT_REGISTRY, an unknown kind falls
// back to "generic", never to a dynamic lookup.
import type { ReactElement } from "react";
import { BookOpen, FileText, Globe, Plug, Send, Wrench } from "lucide-react";

import type { RunStepRow } from "@/lib/run-steps";
import { useT } from "@/lib/i18n";

export type ToolKind = "web" | "knowledge" | "file" | "connection" | "delegation" | "generic";

const WEB = new Set(["web_search", "fetch_url"]);
const KNOWLEDGE = new Set(["search_knowledge", "search_memory"]);
const FILE = new Set([
  "read_context",
  "read_run_file",
  "write_output_file",
  "read_reference_file",
  "read_instruction_file",
  "create_document",
  "update_document",
]);

export function toolKind(row: Pick<RunStepRow, "tool" | "connection">): ToolKind {
  if (WEB.has(row.tool)) return "web";
  if (KNOWLEDGE.has(row.tool)) return "knowledge";
  if (FILE.has(row.tool)) return "file";
  if (row.tool === "delegate_task") return "delegation";
  if (row.connection) return "connection";
  return "generic";
}

interface WebHit {
  title: string;
  url: string;
}

function webHits(row: RunStepRow): WebHit[] {
  if (row.tool === "fetch_url" && typeof row.args.url === "string") {
    return [{ title: row.args.url, url: row.args.url }];
  }
  try {
    const parsed: unknown = JSON.parse(row.result ?? "[]");
    return Array.isArray(parsed)
      ? parsed
          .filter((h): h is WebHit => typeof h?.url === "string" && typeof h?.title === "string")
          .slice(0, 3)
      : [];
  } catch {
    return [];
  }
}

function domain(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

function WebPreview({ row }: { row: RunStepRow }) {
  const hits = webHits(row);
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-2">
      <Globe className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">{row.label}</span>
      {hits.map((h) => (
        <span
          key={h.url}
          className="inline-flex min-w-0 items-center gap-1 rounded-full border border-border px-2 text-xs"
        >
          <img
            src={`https://${domain(h.url)}/favicon.ico`}
            alt=""
            className="size-3"
            loading="lazy"
          />
          <span className="truncate">{domain(h.url)}</span>
        </span>
      ))}
    </span>
  );
}

function KnowledgePreview({ row }: { row: RunStepRow }) {
  const source = /\[Knowledge: ([^\]]+)\]/.exec(row.result ?? "")?.[1];
  return (
    <span className="flex min-w-0 items-center gap-2">
      <BookOpen className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">{row.label}</span>
      {source && <span className="truncate text-muted-foreground">{source}</span>}
    </span>
  );
}

function FilePreview({ row }: { row: RunStepRow }) {
  const name = String(row.args.filename ?? row.args.title ?? row.args.ref_id ?? "");
  return (
    <span className="flex min-w-0 items-center gap-2">
      <FileText className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">{row.label}</span>
      {name && <span className="truncate font-mono text-xs text-muted-foreground">{name}</span>}
    </span>
  );
}

function DelegationPreview({ row }: { row: RunStepRow }) {
  const t = useT();
  const agentId = typeof row.args.agent_id === "string" ? row.args.agent_id : null;
  return (
    <span className="flex min-w-0 items-center gap-2">
      <Send className="size-3.5 shrink-0" aria-hidden />
      {agentId ? (
        <a
          href={`/agents/${encodeURIComponent(agentId)}`}
          className="truncate text-primary hover:underline"
        >
          {row.result?.startsWith("Handed to")
            ? row.result
            : t("Handed to another agent", "An einen anderen Agenten übergeben")}
        </a>
      ) : (
        <span className="truncate">{row.label}</span>
      )}
    </span>
  );
}

function ConnectionPreview({ row }: { row: RunStepRow }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <Plug className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">{row.label}</span>
    </span>
  );
}

function GenericPreview({ row }: { row: RunStepRow }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <Wrench className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">{row.label}</span>
    </span>
  );
}

export const TOOL_PREVIEWS: Record<ToolKind, (props: { row: RunStepRow }) => ReactElement> = {
  web: WebPreview,
  knowledge: KnowledgePreview,
  file: FilePreview,
  connection: ConnectionPreview,
  delegation: DelegationPreview,
  generic: GenericPreview,
};
