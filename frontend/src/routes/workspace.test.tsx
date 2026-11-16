import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as boardsApi from "@/lib/boards-api";
import * as hooks from "@/lib/hooks";
import * as notificationsApi from "@/lib/notifications-api";

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    createFileRoute: () => (opts: unknown) => opts,
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("WorkspacePage", () => {
  it("shows the template picker when the caller has no saved layout", async () => {
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: null,
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useDashboardTemplates").mockReturnValue({
      data: [{ id: "focus-chat", name: { en: "Focus Chat", de: "Fokus-Chat" }, widgets: [] }],
      isPending: false,
    } as never);

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });
    await waitFor(() => screen.getByText(/choose a starting layout/i));
  });

  it("shows the grid once a layout exists", async () => {
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: {
        widgets: [{ id: "w1", type: "budget", x: 0, y: 0, w: 3, h: 3, config: {} }],
        templateId: "focus-chat",
      },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useSaveDashboardLayout").mockReturnValue({ mutate: vi.fn() } as never);
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: undefined,
      isPending: true,
    } as never);

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });
    await waitFor(() => screen.getByText("Budget"));
  });

  it("does not show the template picker when the layout query errors, and never PUTs the picked-template replacement", async () => {
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new Error("network error"),
      refetch: vi.fn(),
    } as never);
    vi.spyOn(hooks, "useDashboardTemplates").mockReturnValue({
      data: [{ id: "focus-chat", name: { en: "Focus Chat", de: "Fokus-Chat" }, widgets: [] }],
      isPending: false,
    } as never);
    const mutate = vi.fn();
    vi.spyOn(hooks, "useSaveDashboardLayout").mockReturnValue({ mutate } as never);

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });

    await waitFor(() => screen.getByRole("button", { name: /retry/i }));
    expect(screen.queryByText(/choose a starting layout/i)).not.toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();
  });

  it("does not PUT the layout back on a page load that changes nothing", async () => {
    const mutate = vi.fn();
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: {
        widgets: [{ id: "w1", type: "budget", x: 0, y: 0, w: 3, h: 3, config: {} }],
        templateId: "focus-chat",
      },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useSaveDashboardLayout").mockReturnValue({ mutate } as never);
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: undefined,
      isPending: true,
    } as never);

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });
    await waitFor(() => screen.getByText("Budget"));

    // The debounced save fires 800ms after the value it watches changes --
    // wait past that with no user interaction and confirm it never ran.
    await new Promise((resolve) => setTimeout(resolve, 900));
    expect(mutate).not.toHaveBeenCalled();
  }, 3000);

  it("opens the layout switcher, confirms, and replaces the layout with a picked preset", async () => {
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: {
        widgets: [{ id: "w1", type: "budget", x: 0, y: 0, w: 3, h: 3, config: {} }],
        templateId: "focus-chat",
      },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useSaveDashboardLayout").mockReturnValue({ mutate: vi.fn() } as never);
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: undefined,
      isPending: true,
    } as never);
    vi.spyOn(hooks, "useDashboardTemplates").mockReturnValue({
      data: [],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useDashboardPresets").mockReturnValue({
      data: [
        {
          id: "preset-1",
          name: "My preset",
          widgets: [{ id: "w2", type: "chat", x: 0, y: 0, w: 6, h: 6, config: {} }],
          scope: "personal",
          mine: true,
        },
      ],
    } as never);
    vi.spyOn(hooks, "useDeleteDashboardPreset").mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
    } as never);

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });
    await waitFor(() => screen.getByText("Budget"));

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: /switch layout/i }));
    fireEvent.click(await screen.findByRole("button", { name: "My preset" }));

    expect(await screen.findByText(/switch layout\?/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Switch$/i }));

    await waitFor(() => expect(screen.queryByText(/switch layout\?/i)).not.toBeInTheDocument());
  });

  it("opens the save-as-preset dialog and submits the current widgets", async () => {
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: {
        widgets: [{ id: "w1", type: "budget", x: 0, y: 0, w: 3, h: 3, config: {} }],
        templateId: "focus-chat",
      },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useSaveDashboardLayout").mockReturnValue({ mutate: vi.fn() } as never);
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: undefined,
      isPending: true,
    } as never);
    const saveMutate = vi.fn();
    vi.spyOn(hooks, "useSaveDashboardPreset").mockReturnValue({
      mutate: saveMutate,
      isPending: false,
    } as never);

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });
    await waitFor(() => screen.getByText("Budget"));

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: /save as preset/i }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "My preset" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() =>
      expect(saveMutate).toHaveBeenCalledWith(
        {
          name: "My preset",
          widgets: [{ id: "w1", type: "budget", x: 0, y: 0, w: 3, h: 3, config: {} }],
          scope: "personal",
        },
        expect.anything(),
      ),
    );
  });

  it("hides the toolbar until Edit is clicked, then offers Done", async () => {
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: {
        widgets: [{ id: "w1", type: "budget", x: 0, y: 0, w: 3, h: 6, config: {} }],
        templateId: null,
      },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useSaveDashboardLayout").mockReturnValue({ mutate: vi.fn() } as never);
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: undefined,
      isPending: true,
    } as never);

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });
    await waitFor(() => screen.getByText("Budget"));
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add widget/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /save as preset/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /switch layout/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /remove widget/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add widget/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save as preset/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /switch layout/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /remove widget/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("button", { name: /add widget/i })).toBeNull();
  });
});

describe("WorkspacePage board notices", () => {
  afterEach(() => vi.restoreAllMocks());

  const notice = {
    id: "n1",
    kind: "board.available",
    subjectId: "b1",
    title: "Dashboard shared with you",
    body: "Revenue · Nora",
    link: "/workspace",
    createdAt: new Date().toISOString(),
    read: false,
  };

  function mockPage({ unassigned, notices }: { unassigned: boolean; notices: (typeof notice)[] }) {
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      pending: false,
      seats: [],
      unrestricted: false,
      decidesEverywhere: false,
      unassigned,
      displayName: "Lona",
      role: "member",
    });
    vi.spyOn(hooks, "useDashboardLayout").mockReturnValue({
      data: null,
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useDashboardTemplates").mockReturnValue({
      data: [{ id: "focus-chat", name: { en: "Focus Chat", de: "Fokus-Chat" }, widgets: [] }],
      isPending: false,
    } as never);
    vi.spyOn(notificationsApi, "useNotifications").mockReturnValue({
      data: notices,
      isPending: false,
    } as never);
    vi.spyOn(notificationsApi, "useMarkRead").mockReturnValue({
      mutate: vi.fn(),
      mutateAsync: vi.fn(() => Promise.resolve()),
    } as never);
    vi.spyOn(boardsApi, "useBoard").mockReturnValue({
      data: undefined,
      error: null,
    } as never);
    const mutate = vi.fn();
    vi.spyOn(hooks, "useSaveDashboardLayout").mockReturnValue({ mutate } as never);
    return mutate;
  }

  const savedTile = {
    widgets: [
      expect.objectContaining({ type: "data_board", config: { boardId: "b1" }, x: 0, y: 0 }),
    ],
    templateId: null,
  };

  it("lets a member without a department add a board shared with them", async () => {
    const mutate = mockPage({ unassigned: true, notices: [notice] });

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });

    // The hint stays, as a note, beside the notice -- not instead of it.
    expect(screen.getByText(/you are not assigned to a department/i)).toBeInTheDocument();
    expect(screen.getByText("Revenue · Nora")).toBeInTheDocument();
    expect(screen.queryByText(/choose a starting layout/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /add to my work/i }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(savedTile, expect.anything()), {
      timeout: 2000,
    });
  }, 4000);

  it("still tells a member without a department or a share that they are unassigned", async () => {
    mockPage({ unassigned: true, notices: [] });

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });

    expect(
      screen.getByRole("heading", { name: /you are not assigned to a department/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/choose a starting layout/i)).not.toBeInTheDocument();
  });

  it("shows the notices above the template picker and adds a board as the first layout", async () => {
    const mutate = mockPage({ unassigned: false, notices: [notice] });

    const { WorkspacePage } = await import("./workspace");
    render(<WorkspacePage />, { wrapper });

    await waitFor(() => screen.getByText(/choose a starting layout/i));
    expect(screen.getByText("Revenue · Nora")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /add to my work/i }));
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(savedTile, expect.anything()), {
      timeout: 2000,
    });
  }, 4000);
});
