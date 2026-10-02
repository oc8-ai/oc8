// Download helper for GET /usage/export (Cost Center screen). Mirrors
// audit-hooks.ts's downloadAuditExport exactly -- same fetch-blob-anchor-click
// pattern, same "throw on !res.ok so the caller can toast an error" contract.

import { API_URL, getToken } from "./api";

export interface UsageFilters {
  agentId?: string;
  departmentId?: string;
  model?: string;
  provider?: string;
  from?: string;
  to?: string;
}

function toParams(filters: UsageFilters): URLSearchParams {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (!v) continue;
    const key = k === "agentId" ? "agent_id" : k === "departmentId" ? "department_id" : k;
    p.set(key, v);
  }
  return p;
}

export async function downloadUsageExport(
  filters: UsageFilters,
  format: "csv" | "jsonl",
): Promise<void> {
  const p = toParams(filters);
  p.set("format", format);
  const res = await fetch(`${API_URL}/usage/export?${p.toString()}`, {
    headers: { authorization: `Bearer ${await getToken()}` },
  });
  if (!res.ok) throw new Error(`export failed: ${res.status}`);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = `usage-export.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}
