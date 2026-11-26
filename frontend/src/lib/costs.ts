import { agents, departments, type Agent } from "./mock-data";
import { useSyncExternalStore } from "react";
import type { UsageRange } from "./hooks";

// USD price per 1,000,000 tokens. Prices are mock, roughly aligned with
// publicly known list prices — editable in the UI to demonstrate the flow.
export interface ModelPricing {
  id: string;           // matches Agent.llm (display name) for lookup
  provider: Agent["provider"];
  inputPer1M: number;   // USD per 1M input tokens
  outputPer1M: number;  // USD per 1M output tokens
  currency: "USD";
}

export const defaultPricing: ModelPricing[] = [
  { id: "Claude 3.5 Sonnet", provider: "Claude", inputPer1M: 3.0,  outputPer1M: 15.0, currency: "USD" },
  { id: "Claude 3.5 Haiku",  provider: "Claude", inputPer1M: 0.8,  outputPer1M: 4.0,  currency: "USD" },
  { id: "GPT-4o",            provider: "GPT",    inputPer1M: 2.5,  outputPer1M: 10.0, currency: "USD" },
  { id: "GPT-4o mini",       provider: "GPT",    inputPer1M: 0.15, outputPer1M: 0.60, currency: "USD" },
  { id: "Mistral Large",     provider: "Mistral",inputPer1M: 2.0,  outputPer1M: 6.0,  currency: "USD" },
  { id: "Llama 3.1 (local)", provider: "Ollama", inputPer1M: 0.0,  outputPer1M: 0.0,  currency: "USD" },
];

// ============= Cost limits =============

export type LimitScope = "agent" | "department" | "model";
export type LimitPeriod = "daily" | "monthly";
export type LimitAction = "notify" | "throttle" | "block";

export interface CostLimit {
  id: string;
  scope: LimitScope;
  targetId: string;        // agent.id | department.id | model.id (llm name)
  amountUSD: number;       // stored in USD
  period: LimitPeriod;
  action: LimitAction;
  enabled: boolean;
}

export const defaultLimits: CostLimit[] = [
  { id: "l1", scope: "agent",      targetId: "vera",         amountUSD: 50,   period: "daily",   action: "notify",  enabled: true },
  { id: "l2", scope: "agent",      targetId: "data",         amountUSD: 200,  period: "monthly", action: "throttle", enabled: true },
  { id: "l3", scope: "department", targetId: "vertrieb",     amountUSD: 800,  period: "monthly", action: "notify",  enabled: true },
  { id: "l4", scope: "department", targetId: "entwicklung",  amountUSD: 1500, period: "monthly", action: "block",   enabled: true },
  { id: "l5", scope: "model",      targetId: "GPT-4o",       amountUSD: 500,  period: "monthly", action: "notify",  enabled: true },
  { id: "l6", scope: "model",      targetId: "Claude 3.5 Sonnet", amountUSD: 1000, period: "monthly", action: "throttle", enabled: false },
];

// Deterministic pseudo-random so numbers look real but stay stable.
function seeded(seed: string) {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return () => {
    h ^= h << 13; h ^= h >>> 17; h ^= h << 5;
    return ((h >>> 0) % 1_000_000) / 1_000_000;
  };
}

export interface AgentUsage {
  agentId: string;
  inputTokens: number;   // last 30 days
  outputTokens: number;
  requests: number;
}

export const agentUsage: AgentUsage[] = agents.map((a) => {
  const rnd = seeded(a.id);
  // Scale by tasksToday * ~30 days with jitter
  const days = 30;
  const baseIn = Math.round(a.tasksToday * days * (2200 + rnd() * 3800));
  const baseOut = Math.round(a.tasksToday * days * (700 + rnd() * 1600));
  const requests = Math.round(a.tasksToday * days * (0.9 + rnd() * 0.4));
  return { agentId: a.id, inputTokens: baseIn, outputTokens: baseOut, requests };
});

export const FX_USD_EUR = 0.92; // mock FX rate

export function convert(usd: number, currency: "USD" | "EUR") {
  return currency === "USD" ? usd : usd * FX_USD_EUR;
}

export function formatMoney(amount: number, currency: "USD" | "EUR") {
  const sym = currency === "USD" ? "$" : "€";
  const rounded = amount >= 100 ? amount.toFixed(0) : amount.toFixed(2);
  return `${sym}${Number(rounded).toLocaleString(currency === "USD" ? "en-US" : "de-DE")}`;
}

export function formatTokens(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${n}`;
}

export function pricingFor(agent: Agent, pricing: ModelPricing[]) {
  return (
    pricing.find((p) => p.id === agent.llm) ??
    pricing.find((p) => p.provider === agent.provider) ??
    { id: agent.llm, provider: agent.provider, inputPer1M: 1, outputPer1M: 3, currency: "USD" as const }
  );
}

export function costForAgent(agent: Agent, usage: AgentUsage, pricing: ModelPricing[]) {
  const p = pricingFor(agent, pricing);
  const inputCost = (usage.inputTokens / 1_000_000) * p.inputPer1M;
  const outputCost = (usage.outputTokens / 1_000_000) * p.outputPer1M;
  return { inputCost, outputCost, total: inputCost + outputCost, pricing: p };
}

export function costsByAgent(pricing: ModelPricing[]) {
  return agents.map((a) => {
    const usage = agentUsage.find((u) => u.agentId === a.id)!;
    const cost = costForAgent(a, usage, pricing);
    return { agent: a, usage, ...cost };
  });
}

export function costsByDepartment(pricing: ModelPricing[]) {
  const rows = costsByAgent(pricing);
  return departments.map((d) => {
    const inDept = rows.filter((r) => r.agent.departmentId === d.id);
    const totals = inDept.reduce(
      (acc, r) => {
        acc.inputTokens += r.usage.inputTokens;
        acc.outputTokens += r.usage.outputTokens;
        acc.requests += r.usage.requests;
        acc.inputCost += r.inputCost;
        acc.outputCost += r.outputCost;
        acc.total += r.total;
        return acc;
      },
      { inputTokens: 0, outputTokens: 0, requests: 0, inputCost: 0, outputCost: 0, total: 0 },
    );
    return { department: d, agents: inDept, ...totals };
  });
}

// ============= Reactive stores =============

function createStore<T>(initial: T) {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => state,
    set: (next: T) => {
      state = next;
      listeners.forEach((l) => l());
    },
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
}

const pricingStore = createStore<ModelPricing[]>(defaultPricing);
const limitsStore = createStore<CostLimit[]>(defaultLimits);

export function usePricing(): [ModelPricing[], (next: ModelPricing[]) => void] {
  const value = useSyncExternalStore(
    pricingStore.subscribe,
    pricingStore.get,
    pricingStore.get,
  );
  return [value, pricingStore.set];
}

export function useLimits(): [CostLimit[], (next: CostLimit[]) => void] {
  const value = useSyncExternalStore(
    limitsStore.subscribe,
    limitsStore.get,
    limitsStore.get,
  );
  return [value, limitsStore.set];
}

// Compute current spend for a limit, in USD, based on mock 30-day usage.
// Daily = monthly / 30 approximation for demo purposes.
export function currentSpendForLimit(limit: CostLimit, pricing: ModelPricing[]): number {
  const rows = costsByAgent(pricing);
  let monthly = 0;
  if (limit.scope === "agent") {
    monthly = rows.find((r) => r.agent.id === limit.targetId)?.total ?? 0;
  } else if (limit.scope === "department") {
    monthly = rows
      .filter((r) => r.agent.departmentId === limit.targetId)
      .reduce((s, r) => s + r.total, 0);
  } else if (limit.scope === "model") {
    monthly = rows
      .filter((r) => r.agent.llm === limit.targetId)
      .reduce((s, r) => s + r.total, 0);
  }
  return limit.period === "daily" ? monthly / 30 : monthly;
}

/** The org time zone when the browser knows it, otherwise UTC. */
export function resolveTimeZone(timeZone: string | undefined): string {
  if (!timeZone) return "UTC";
  try {
    new Intl.DateTimeFormat("en", { timeZone });
    return timeZone;
  } catch {
    return "UTC";
  }
}

/** Axis and tooltip labels for the bucket starts, in the given time zone. */
export function bucketFormatters(
  lang: string,
  bucket: "hour" | "day",
  range: UsageRange,
  timeZone: string,
) {
  const tick = new Intl.DateTimeFormat(
    lang,
    bucket === "hour"
      ? range === "24h"
        ? { hour: "2-digit", minute: "2-digit", timeZone }
        : { weekday: "short", hour: "2-digit", timeZone }
      : { day: "2-digit", month: "2-digit", timeZone },
  );
  const tooltip = new Intl.DateTimeFormat(
    lang,
    bucket === "hour"
      ? {
          weekday: "short",
          day: "2-digit",
          month: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          timeZone,
        }
      : { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric", timeZone },
  );
  const format = (fmt: Intl.DateTimeFormat) => (iso: unknown) =>
    typeof iso === "string" ? fmt.format(new Date(iso)) : "";
  return { tick: format(tick), tooltip: format(tooltip) };
}
