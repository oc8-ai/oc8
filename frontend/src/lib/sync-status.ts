import type { DataSource } from "@/lib/mock-data";

/** A source whose newest sync job is still queued or running. */
export function syncInFlight(source: Pick<DataSource, "latestJobStatus">): boolean {
  return source.latestJobStatus === "queued" || source.latestJobStatus === "running";
}
