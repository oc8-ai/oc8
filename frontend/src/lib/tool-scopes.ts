// A server-offered tool's read/modify classification on one connection
// (GET/PUT /mcp/connections/{id}/tool-scopes).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface ToolScopesSnapshot {
  connectionId: string;
  name: string;
  tools: string[];
  read: string[];
  modify: string[];
  editable: boolean;
}

const toolScopesKey = (connectionId: string) => ["mcp", "tool-scopes", connectionId] as const;

export function useToolScopes(connectionId: string | null) {
  return useQuery<ToolScopesSnapshot>({
    queryKey: toolScopesKey(connectionId ?? ""),
    queryFn: () => api.get<ToolScopesSnapshot>(`/mcp/connections/${connectionId}/tool-scopes`),
    enabled: connectionId !== null,
  });
}

export function useSaveToolScopes(connectionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { read: string[]; modify: string[] }) =>
      api.put<ToolScopesSnapshot>(`/mcp/connections/${connectionId}/tool-scopes`, body),
    onSuccess: (snapshot) => {
      qc.setQueryData(toolScopesKey(connectionId), snapshot);
      // The guardrail editors read the same classification by connection name.
      void qc.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}
