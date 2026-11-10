// The words of the "waiting for an answer" line (ThinkingPresence): what the
// run is doing right now, said the way a person would. Pure, so the mapping
// is tested without a DOM. Tool calls reach the client once they ran, so the
// newest one is what the model is working with now.
import { START_TEXT, isStartPhase } from "@/lib/run-start-phase";
import { resolveToolLabel, type ToolLabelCatalogueDTO, type Translate } from "@/lib/tool-labels";
import type { Lang } from "@/lib/i18n";

export interface PresenceRun {
  state: string;
  phase?: string | null;
  toolCalls: Array<Record<string, unknown>>;
}

export interface PresenceLabel {
  /** Changes exactly when the words change: the crossfade and the
   *  screen-reader announcement both key on it. */
  key: string;
  text: string;
}

export interface PresenceContext {
  t: Translate;
  lang: Lang;
  catalogue?: ToolLabelCatalogueDTO;
  /** An agent's display name, when the client already knows it. */
  agentName?: (agentId: string) => string | null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

function host(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname || null;
  } catch {
    return null;
  }
}

/** The newest tool call of a run, or null before the first one. */
export function latestCall(run: PresenceRun): Record<string, unknown> | null {
  return run.toolCalls.length ? run.toolCalls[run.toolCalls.length - 1] : null;
}

export function presenceLabel(run: PresenceRun | null, ctx: PresenceContext): PresenceLabel {
  const { t } = ctx;
  const thinking = { key: "thinking", text: t("Thinking …", "Denkt nach …") };
  if (!run) return thinking;
  if (isStartPhase(run.phase)) return { key: "start", text: t(START_TEXT.en, START_TEXT.de) };

  const call = latestCall(run);
  if (!call) return thinking;
  const tool = str(call.tool) ?? str(call.name) ?? "";
  if (!tool) return thinking;
  const raw = call.arguments;
  const args =
    raw !== null && typeof raw === "object" && !Array.isArray(raw)
      ? (raw as Record<string, unknown>)
      : {};
  // Position plus tool: two calls of the same tool in a row are still news.
  const key = `${run.toolCalls.length}-${tool}`;

  switch (tool) {
    case "fetch_url": {
      const where = host(str(args.url));
      return {
        key,
        text: where
          ? t(`Reading ${where} …`, `Liest ${where} …`)
          : t("Reading a web page …", "Liest eine Webseite …"),
      };
    }
    case "web_search":
      return { key, text: t("Searching the web …", "Sucht im Web …") };
    case "search_knowledge":
      return { key, text: t("Searching the knowledge base …", "Sucht im Wissen …") };
    case "search_memory":
      return { key, text: t("Looking through its notes …", "Sieht seine Notizen durch …") };
    case "list_pending_approvals":
    case "decide_approval":
      return { key, text: t("Checking your approvals …", "Prüft Ihre Freigaben …") };
    case "delegate_task": {
      const id = str(args.agent_id);
      const name = id ? (ctx.agentName?.(id) ?? null) : null;
      return {
        key,
        text: name
          ? t(`Asking ${name} …`, `Fragt ${name} …`)
          : t("Asking a colleague …", "Fragt einen Kollegen …"),
      };
    }
  }

  // Everything else: the tool's own present-tense label when one exists (a
  // control tool, or a plugin that declares one), else the tool by name.
  const label = resolveToolLabel(
    { tool, arguments: args, connection: str(call.connection), running: true },
    ctx.catalogue,
    t,
    ctx.lang,
  );
  if (label && label !== tool) return { key, text: `${label} …` };
  const named = tool.replace(/[_-]+/g, " ").trim();
  return { key, text: t(`Working on ${named} …`, `Arbeitet an ${named} …`) };
}
