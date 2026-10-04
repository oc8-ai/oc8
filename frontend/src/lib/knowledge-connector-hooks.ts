// Connector-framework mutations (§11.2) for the knowledge-sources UI: create a
// data source, trigger a sync, and preview a source's discoverable items.
// Kept in a separate module from `hooks.ts` (not edited here) — invalidations
// below target the exact same query key `useDataSources` reads in hooks.ts
// (`keys.sources = ["knowledge", "sources"]`), so the sources list refreshes
// after create/sync.

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

const SOURCES_KEY = ["knowledge", "sources"] as const;

// Ingestion is async: POST /sync returns a queued job immediately and the
// actual crawl/chunk/embed work happens on the worker. `useIngestionJob`
// polls GET /knowledge/jobs/{id} until the job reaches a terminal status —
// the refetchInterval below MUST stop polling at that point, or it would
// hammer the API forever.
export const JOB_TERMINAL_STATUSES = new Set(["succeeded", "partial", "failed"]);

export interface DataSource {
  id: string;
  kind: string;
  name: string;
  connected: boolean;
  lastSync: string;
  docCount: number;
  connectorType: string;
  // Non-secret connector config only -- a `credential`-typed key holds a
  // credential id, never a resolved secret value.
  config?: Record<string, unknown>;
}

export interface IngestionJob {
  id: string;
  status: string;
  // Mostly counters (fetched/ingested/skipped/chunks/...), but a fatal
  // connector-level failure lands here as `error: string` (ingest.py's
  // `stats["error"] = fatal`) -- e.g. "unknown connector type: 's3'". A
  // `Record<string, number>` type silently dropped that string in practice.
  stats: Record<string, number | string>;
}

export interface ConnectorCatalogEntry {
  typeId: string;
  label?: string | null;
  description?: string | null;
  configSchema: {
    properties?: Record<
      string,
      {
        type?: string;
        title?: string;
        description?: string;
        default?: unknown;
        minimum?: number;
        maximum?: number;
        // Names a `credential_type` (e.g. "s3_api") this field resolves
        // through the unified credentials framework -- the field's value is
        // a `Credential` row's id, submitted in `config` as-is. Rendered as
        // a <CredentialPicker> instead of a raw input.
        credentialType?: string;
      }
    >;
    required?: string[];
  };
  requiresOauth?: string | null;
}

export interface OAuthConnection {
  id: string;
  provider: string;
  accountLabel: string;
  status: string;
}

export function useKnowledgeConnectors() {
  return useQuery({
    queryKey: ["knowledge", "connectors"],
    queryFn: () => api.get<ConnectorCatalogEntry[]>("/knowledge/connectors"),
  });
}

export interface VectorIndexCatalogEntry {
  typeId: string;
  label?: string | null;
  description?: string | null;
  configSchema: {
    properties?: Record<
      string,
      {
        type?: string;
        title?: string;
        description?: string;
        default?: unknown;
      }
    >;
    required?: string[];
  };
  credentialType: string;
}

export function useKnowledgeVectorIndexes() {
  return useQuery({
    queryKey: ["knowledge", "vector-indexes"],
    queryFn: () => api.get<VectorIndexCatalogEntry[]>("/knowledge/vector-indexes"),
  });
}

export function useOAuthConnections() {
  return useQuery({
    queryKey: ["oauth", "connections"],
    queryFn: () => api.get<OAuthConnection[]>("/oauth/connections"),
  });
}

export function useCreateSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      connectorType: string;
      name: string;
      config: Record<string, unknown>;
      kbId: string;
      classification?: string;
      oauthConnectionId?: string;
    }) => api.post<DataSource>("/knowledge/sources", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: SOURCES_KEY }),
  });
}

// PATCH /knowledge/sources/{id} -- deliberately narrow, matching the
// backend's UpdateSourceRequest: name and classification only. Not
// connectorType/config (a different, riskier operation) and not
// scheduleCron -- the backend column it writes to is never read back by
// datasource_to_dto (which reads config.schedule instead), so exposing an
// editor for it here would silently appear to do nothing.
export function useUpdateSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      sourceId,
      ...body
    }: {
      sourceId: string;
      name?: string;
      classification?: string;
      config?: Record<string, unknown>;
    }) => api.patch<DataSource>(`/knowledge/sources/${sourceId}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: SOURCES_KEY }),
  });
}

// DELETE /knowledge/sources/{id} -- irreversible (§12.5.1): tombstones the
// source and erases every chunk it ever contributed, in every knowledge base
// it was ever synced into, not just one. Invalidates the bases list too
// since a base's sourceIds/doc counts are derived live from KbChunk, not
// stored, so they change the instant this lands.
export function useDeleteSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) =>
      api.delete<{ sourceId: string; documents: number; chunks: number }>(
        `/knowledge/sources/${sourceId}`,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SOURCES_KEY });
      qc.invalidateQueries({ queryKey: ["knowledge", "bases"] });
    },
  });
}

export function useSyncSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ sourceId, kbId }: { sourceId: string; kbId: string }) =>
      api.post<IngestionJob>(`/knowledge/sources/${sourceId}/sync`, { kbId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: SOURCES_KEY }),
  });
}

// Polls a single ingestion job to terminal. `enabled: !!jobId` lets callers
// pass `null` when nothing is in flight (query stays idle, no request).
export function useIngestionJob(jobId: string | null) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["knowledge", "jobs", jobId] as const,
    queryFn: () => api.get<IngestionJob>(`/knowledge/jobs/${jobId}`),
    enabled: !!jobId,
    // Pure poll-or-stop decision — no side effects here. Returning `false`
    // once terminal is what actually stops the polling loop.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && JOB_TERMINAL_STATUSES.has(status) ? false : 1500;
    },
  });
  const status = query.data?.status;
  useEffect(() => {
    if (status && JOB_TERMINAL_STATUSES.has(status)) {
      // Reached succeeded/partial/failed — refresh the sources/bases lists
      // so docCount/lastSync reflect the finished run.
      qc.invalidateQueries({ queryKey: SOURCES_KEY });
    }
  }, [jobId, status, qc]);
  return query;
}

export function usePreviewSource() {
  return useMutation({
    mutationFn: (sourceId: string) =>
      api.post<{ items: { uri: string; title: string }[]; sampleText: string }>(
        `/knowledge/sources/${sourceId}/preview`,
        {},
      ),
  });
}
