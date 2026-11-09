// Route file name deliberately deviates from the plan's illustrative
// `knowledge.bases.$kbId.content.tsx`: `knowledge.tsx` is a full page
// component with no `<Outlet />` (unlike `members.tsx`/`agents.tsx`, which
// are thin `<Outlet />`-only layouts for their `$id` children), so a literal
// `knowledge.bases...` filename would be auto-nested under it by TanStack
// Router's file-based codegen and would silently never render. The trailing
// underscore on the "knowledge" segment (`knowledge_.bases...`) is TanStack
// Router's documented "escape nesting" convention: it still produces the URL
// `/knowledge/bases/$kbId/content` (the underscore is stripped from the
// path), but registers the route's parent as root instead of `KnowledgeRoute`
// -- verified against the generated `routeTree.gen.ts` (`getParentRoute: ()
// => rootRouteImport`) before committing this file.
import { DiscussWithCopilot } from "@/components/conversation/discuss-with-copilot";
import { useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft, ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { Panel } from "@/components/app-shell";
import { useKbDocuments, useKbChunks, useSimilarChunks } from "@/lib/hooks";
import { ApiError } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const CHUNKS_PAGE_SIZE = 20;

export const Route = createFileRoute("/knowledge_/bases/$kbId/content")({
  component: KbContentRoute,
});

// `KbContentRoute` (router-aware chrome: back-link, page wrapper) is kept
// separate from `KbContentView` (the pure two-pane browser) deliberately --
// `KbContentView` is unit-tested directly with only a QueryClientProvider
// around it (see `kb-content-view.test.tsx`), and a `<Link>` would throw
// ("useRouter must be used inside a <RouterProvider>") without a full router
// context that test intentionally doesn't set up.
function KbContentRoute() {
  const { kbId } = Route.useParams();
  const t = useT();
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Link
          to="/knowledge"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-primary"
        >
          <ArrowLeft className="h-4 w-4" /> {t("Back to knowledge", "Zurück zu Wissen")}
        </Link>
        <DiscussWithCopilot
          kind="knowledge_base"
          id={kbId}
          label={t("this knowledge base", "diese Wissensbasis")}
        />
      </div>
      <KbContentView kbId={kbId} />
    </div>
  );
}

export function KbContentView({ kbId }: { kbId: string }) {
  const t = useT();
  const [selectedSourceUri, setSelectedSourceUri] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [similarChunkId, setSimilarChunkId] = useState<string | null>(null);
  // 1-indexed, matching `Page<T>`/`ListToolbar`'s convention elsewhere. Reset
  // to 1 at every call site that changes `selectedSourceUri` or `search`
  // below -- a new filter with a stale page number can point past the end of
  // the new result set and render an empty pane that looks like "no chunks"
  // rather than "page too far".
  const [page, setPage] = useState(1);
  const isSearchMode = search.trim().length > 0;
  const { data: documents, isLoading: documentsLoading } = useKbDocuments(kbId);
  const {
    data: chunksPage,
    isLoading: chunksLoading,
    isError: chunksIsError,
    error: chunksError,
  } = useKbChunks(kbId, {
    search: isSearchMode ? search : undefined,
    sourceUri: isSearchMode ? undefined : (selectedSourceUri ?? undefined),
    page,
    pageSize: CHUNKS_PAGE_SIZE,
  });
  // `GET .../chunks` (and .../similar) are gated at `knowledge:manage`, one
  // tier above the `knowledge:view` that gates `GET .../documents` -- so a
  // VIEW-only operator reaches this page, the document list populates
  // normally, and only the chunk pane 403s. Distinguished from a genuinely
  // empty knowledge base by checking `ApiError.status`, not by the message
  // text, which is a permission string that isn't meant for parsing.
  const chunksPermissionDenied =
    chunksIsError && chunksError instanceof ApiError && chunksError.status === 403;
  const {
    data: similarChunks,
    isLoading: similarLoading,
    isError: similarError,
  } = useSimilarChunks(kbId, similarChunkId);
  const chunks = chunksPage?.items ?? [];

  return (
    <>
      <input
        type="text"
        placeholder={t("Search this knowledge base…", "Diese Wissensdatenbank durchsuchen…")}
        value={search}
        onChange={(e) => {
          setSearch(e.target.value);
          setPage(1);
        }}
        className="mb-3 w-full rounded-md border border-border bg-panel px-3 py-1.5 text-sm"
      />
      <Panel className="flex h-[70vh] gap-4 p-0">
        <aside
          className={cn(
            "w-64 shrink-0 overflow-y-auto border-r border-border p-4",
            isSearchMode && "opacity-50",
          )}
        >
          <h3 className="mb-2 text-xs uppercase tracking-widest text-muted-foreground">
            {t("Documents", "Dokumente")}
          </h3>
          {documentsLoading ? (
            <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              {t("Loading…", "Wird geladen…")}
            </div>
          ) : (documents ?? []).length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">
              {t("No documents yet.", "Noch keine Dokumente.")}
            </p>
          ) : (
            <ul className="space-y-1">
              {(documents ?? []).map((doc) => (
                // Keyed on dataSourceId AND sourceUri, not sourceUri alone: two
                // data sources crawling overlapping scopes (or a source removed
                // and re-added) legitimately put the SAME source_uri in one base
                // twice, and `GET /documents` returns a row per
                // (data_source_id, source_uri) pair -- seen live on the dev
                // stack, where the base listed .../datenschutz twice. A bare
                // sourceUri key is then duplicated, which React silently
                // mis-reconciles in a production build (the dev-only duplicate
                // key warning is stripped).
                <li key={`${doc.dataSourceId ?? "none"}:${doc.sourceUri}`}>
                  <button
                    type="button"
                    disabled={isSearchMode}
                    // The pane is 16rem wide and every URI in a crawled base
                    // shares a long prefix, so the truncated labels are visually
                    // identical without a tooltip carrying the full value.
                    title={doc.sourceUri}
                    onClick={() => {
                      setSelectedSourceUri(doc.sourceUri);
                      setPage(1);
                    }}
                    className={cn(
                      "w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-panel/60",
                      selectedSourceUri === doc.sourceUri && "bg-panel/60 font-medium text-primary",
                      isSearchMode && "cursor-not-allowed hover:bg-transparent",
                    )}
                  >
                    {doc.sourceUri}
                    <span className="ml-1 text-xs text-muted-foreground">({doc.chunks})</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <section className="flex-1 overflow-y-auto p-4">
          <h3 className="mb-2 text-xs uppercase tracking-widest text-muted-foreground">
            {isSearchMode
              ? t("Chunks · search results", "Chunks · Suchergebnisse")
              : selectedSourceUri
                ? t("Chunks · ", "Chunks · ") + selectedSourceUri
                : t("Chunks · all documents", "Chunks · alle Dokumente")}
          </h3>
          {chunksLoading ? (
            <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              {t("Loading…", "Wird geladen…")}
            </div>
          ) : chunksPermissionDenied ? (
            <p className="py-4 text-sm text-muted-foreground">
              {t(
                "You don't have permission to view this content.",
                "Sie haben keine Berechtigung, diesen Inhalt anzuzeigen.",
              )}
            </p>
          ) : chunks.length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">
              {t("No chunks found.", "Keine Chunks gefunden.")}
            </p>
          ) : (
            <div className="space-y-3">
              {chunks.map((chunk) => (
                <div key={chunk.id} className="rounded-md border border-border p-3">
                  <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="rounded bg-panel px-1.5 py-0.5">{chunk.classification}</span>
                    {/* Only shown in "all documents"/search mode -- once a
                        document is selected (and search is empty) its
                        sourceUri is already the section heading, and
                        repeating the bare sourceUri string here would
                        collide with the document-list button's own text in
                        exact-text queries (`getByText(doc.sourceUri)`). In
                        search mode results can span documents even if a
                        document was previously selected, so the label is
                        needed there too. */}
                    {(isSearchMode || !selectedSourceUri) && (
                      <span className="truncate">
                        {t("Source: ", "Quelle: ")}
                        {chunk.sourceUri}
                      </span>
                    )}
                  </div>
                  <p className="whitespace-pre-wrap text-sm">{chunk.content}</p>
                  <button
                    type="button"
                    onClick={() => setSimilarChunkId(chunk.id)}
                    // `text-primary` (Electric Blue, identical in both themes),
                    // NOT `text-accent`: in this design system `--accent` is a
                    // SURFACE token (#eef1f6 light / #141827 dark), so
                    // `text-accent` painted this control near-black on the dark
                    // panel -- invisible on the live stack until it was measured.
                    className="mt-2 text-xs text-primary hover:underline"
                  >
                    {t("Find similar", "Ähnliche finden")}
                  </button>
                </div>
              ))}
            </div>
          )}
          {!chunksLoading &&
          !chunksPermissionDenied &&
          (chunksPage?.totalCount ?? 0) > CHUNKS_PAGE_SIZE ? (
            <div className="mt-3 flex items-center justify-center gap-3 text-sm">
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-muted-foreground hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
              >
                <ChevronLeft className="h-4 w-4" />
                {t("Previous", "Zurück")}
              </button>
              <span className="text-xs text-muted-foreground">
                {t("Page ", "Seite ")}
                {page}
                {t(" of ", " von ")}
                {Math.max(1, Math.ceil((chunksPage?.totalCount ?? 0) / CHUNKS_PAGE_SIZE))}
              </span>
              <button
                type="button"
                disabled={page >= Math.ceil((chunksPage?.totalCount ?? 0) / CHUNKS_PAGE_SIZE)}
                onClick={() =>
                  setPage((p) =>
                    Math.min(Math.ceil((chunksPage?.totalCount ?? 0) / CHUNKS_PAGE_SIZE), p + 1),
                  )
                }
                className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-muted-foreground hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t("Next", "Weiter")}
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          ) : null}
          {/* Keyed on `similarChunkId` alone, not `&& similarChunks`: the
              endpoint 404s for a chunk that has no embedding yet (an embedding
              model that was unreachable at ingest leaves the column NULL), and
              on that branch the panel used to render nothing at all -- clicking
              "Find similar" looked like a dead button. Seen live on the dev
              stack against chunks ingested before an embedding model was
              available. */}
          {similarChunkId ? (
            <div className="mt-4 border-t border-border pt-3">
              <h3 className="mb-2 text-xs uppercase text-muted-foreground">
                {t("Similar chunks", "Ähnliche Chunks")}
              </h3>
              {similarLoading ? (
                <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  {t("Loading…", "Wird geladen…")}
                </div>
              ) : similarError || (similarChunks ?? []).length === 0 ? (
                <p className="py-2 text-sm text-muted-foreground">
                  {t(
                    "No similar chunks for this one yet.",
                    "Noch keine ähnlichen Chunks für diesen.",
                  )}
                </p>
              ) : null}
              {(similarChunks ?? []).map((s) => (
                <div key={s.id} className="mb-3 rounded-md border border-border p-3">
                  <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="rounded bg-panel px-1.5 py-0.5">{s.classification}</span>
                    <span>{s.sourceUri}</span>
                    <span className="ml-auto">{Math.round(s.similarity * 100)}%</span>
                  </div>
                  <p className="whitespace-pre-wrap text-sm">{s.content}</p>
                </div>
              ))}
            </div>
          ) : null}
        </section>
      </Panel>
    </>
  );
}
