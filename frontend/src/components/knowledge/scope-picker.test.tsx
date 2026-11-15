// <ScopePicker> lists a connector's folders from a draft config (POST
// /knowledge/connectors/{typeId}/browse) so a source's scope is picked before
// it is saved. Folders expand lazily; an "app" entry offers an "Everything" ("Alles") row that
// stands for everything in it; an entry with a note shows the note instead of
// children and cannot be ticked.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const postMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { post: (path: string, body: unknown) => postMock(path, body) },
}));

let lang: "en" | "de" = "de";
vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (en: string, de?: string) => (lang === "de" ? (de ?? en) : en) };
});

import { ScopePicker, type ScopeItem } from "@/components/knowledge/scope-picker";

const ROOT = {
  entries: [
    { id: "app:documents", name: "Documents", isFolder: true, kind: "app" },
    { id: "app:knowledge", name: "Knowledge", isFolder: true, kind: "app", note: "Not installed" },
    { id: "f:1", name: "Contracts", isFolder: true, kind: "folder" },
    { id: "d:9", name: "readme.txt", isFolder: false, kind: "file" },
  ],
  truncated: false,
};
const DOCUMENTS = {
  entries: [
    { id: "f:2", name: "Invoices", isFolder: true, kind: "folder" },
    { id: "f:3", name: "HR", isFolder: true, kind: "folder" },
  ],
  truncated: false,
};

function listing(path: string) {
  if (path === "/") return ROOT;
  if (path === "app:documents") return DOCUMENTS;
  return { entries: [], truncated: false };
}

function Harness({
  initial = [],
  disabled = false,
  onChange,
}: {
  initial?: ScopeItem[];
  disabled?: boolean;
  onChange?: (v: ScopeItem[]) => void;
}) {
  const [value, setValue] = useState<ScopeItem[]>(initial);
  return (
    <ScopePicker
      typeId="demo"
      config={{ credential: "cred-1" }}
      value={value}
      onChange={(v) => {
        setValue(v);
        onChange?.(v);
      }}
      disabled={disabled}
    />
  );
}

function renderPicker(props: Parameters<typeof Harness>[0] = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
}

describe("ScopePicker", () => {
  beforeEach(() => {
    lang = "de";
    postMock.mockReset();
    postMock.mockImplementation((_path: string, body: { path: string }) =>
      Promise.resolve(listing(body.path)),
    );
  });

  it("renders the root listing", async () => {
    renderPicker();
    expect(await screen.findByText("Documents")).toBeInTheDocument();
    expect(screen.getByText("Contracts")).toBeInTheDocument();
    expect(screen.getByText("readme.txt")).toBeInTheDocument();
    expect(postMock).toHaveBeenCalledWith("/knowledge/connectors/demo/browse", {
      config: { credential: "cred-1" },
      path: "/",
    });
  });

  it("expands a folder lazily, browsing with the entry's id as the path", async () => {
    renderPicker();
    fireEvent.click(await screen.findByRole("button", { name: "Documents" }));
    expect(await screen.findByText("Invoices")).toBeInTheDocument();
    expect(postMock).toHaveBeenCalledWith("/knowledge/connectors/demo/browse", {
      config: { credential: "cred-1" },
      path: "app:documents",
    });
    // Only the root and the expanded app were browsed.
    expect(postMock).toHaveBeenCalledTimes(2);
  });

  it("stores a ticked entry as {id, label, kind}", async () => {
    const onChange = vi.fn();
    renderPicker({ onChange });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Contracts" }));
    expect(onChange).toHaveBeenLastCalledWith([{ id: "f:1", label: "Contracts", kind: "folder" }]);
    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));
    expect(onChange).toHaveBeenLastCalledWith([
      { id: "f:1", label: "Contracts", kind: "folder" },
      { id: "d:9", label: "readme.txt", kind: "file" },
    ]);
    fireEvent.click(screen.getByRole("checkbox", { name: "Contracts" }));
    expect(onChange).toHaveBeenLastCalledWith([{ id: "d:9", label: "readme.txt", kind: "file" }]);
  });

  it("offers 'Alles' first in an app; ticking it clears and disables the app's other ticks", async () => {
    const onChange = vi.fn();
    renderPicker({ onChange });
    fireEvent.click(await screen.findByRole("button", { name: "Documents" }));
    const invoices = await screen.findByRole("checkbox", { name: "Invoices" });
    const all = screen.getByRole("checkbox", { name: "Alles" });
    // "Alles" is the app's first child row.
    expect(all.compareDocumentPosition(invoices) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(invoices);
    fireEvent.click(screen.getByRole("checkbox", { name: "Contracts" }));
    fireEvent.click(all);

    expect(onChange).toHaveBeenLastCalledWith([
      { id: "f:1", label: "Contracts", kind: "folder" },
      { id: "app:documents", label: "Alles", kind: "all" },
    ]);
    expect(screen.getByRole("checkbox", { name: "Invoices" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Invoices" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "HR" })).toBeDisabled();
  });

  it("marks a collapsed app whose 'Alles' is ticked", async () => {
    renderPicker({ initial: [{ id: "app:documents", label: "Alles", kind: "all" }] });
    const app = await screen.findByRole("button", { name: "Documents" });
    expect(app).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Alles: ausgewählt")).toBeInTheDocument();
  });

  it("waits for the config to settle before browsing it again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const view = (config: Record<string, unknown>) => (
        <QueryClientProvider client={qc}>
          <ScopePicker typeId="demo" config={config} value={[]} onChange={vi.fn()} />
        </QueryClientProvider>
      );
      const { rerender } = render(view({ credential: "cred-1" }));
      await screen.findByText("Contracts");
      expect(postMock).toHaveBeenCalledTimes(1);

      rerender(view({ credential: "cred-2" }));
      rerender(view({ credential: "cred-3" }));
      await vi.advanceTimersByTimeAsync(100);
      expect(postMock).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(500);
      await waitFor(() => expect(postMock).toHaveBeenCalledTimes(2));
      expect(postMock).toHaveBeenLastCalledWith("/knowledge/connectors/demo/browse", {
        config: { credential: "cred-3" },
        path: "/",
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows an entry's note instead of its children, without a checkbox", async () => {
    renderPicker();
    expect(await screen.findByText("Not installed")).toBeInTheDocument();
    expect(screen.getByText("Knowledge")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Knowledge" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Knowledge" })).not.toBeInTheDocument();
  });

  it("says when a listing was cut off", async () => {
    postMock.mockImplementation(() =>
      Promise.resolve({ entries: ROOT.entries.slice(2), truncated: true }),
    );
    renderPicker();
    expect(
      await screen.findByText("Mehr als 500 Einträge – wähle einen übergeordneten Ordner."),
    ).toBeInTheDocument();
  });

  it("says when a listing was cut off, in English", async () => {
    lang = "en";
    postMock.mockImplementation(() =>
      Promise.resolve({ entries: ROOT.entries.slice(2), truncated: true }),
    );
    renderPicker();
    expect(
      await screen.findByText("More than 500 entries – pick a parent folder."),
    ).toBeInTheDocument();
  });

  it("shows the server's reason when a listing fails", async () => {
    postMock.mockImplementation(() => Promise.reject(new Error("This source can't be browsed.")));
    renderPicker();
    expect(await screen.findByText("This source can't be browsed.")).toBeInTheDocument();
  });

  it("stays disabled, and browses nothing, until the credential is set", () => {
    renderPicker({ disabled: true });
    expect(postMock).not.toHaveBeenCalled();
    expect(screen.getByText(/Fülle zuerst die übrigen Pflichtfelder aus/)).toBeInTheDocument();
  });
});
