// The sidebar's writes land in the cached lists at once: no refetch of the list.
import { QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  put: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
  get: vi.fn(),
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api }));

import { groupSessions } from "@/components/conversation/copilot-sidebar";
import {
  useDeleteProject,
  useSaveProject,
  useSetSessionPinned,
  useSetSessionProject,
  type ChatProjectDTO,
} from "@/lib/conversation-api";
import {
  chatKeys,
  useDeleteChatSession,
  useRenameChatSession,
  type ChatSessionDTO,
} from "@/lib/hooks-chat";

const s = (id: string, over: Partial<ChatSessionDTO> = {}): ChatSessionDTO => ({
  id,
  agentId: "x",
  title: id,
  createdAt: "",
  lastMessageAt: "2026-11-08T08:00:00Z",
  pinned: false,
  projectId: null,
  ...over,
});

let qc: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client: qc }, children);
const listOf = () => qc.getQueryData<ChatSessionDTO[]>(chatKeys.sessions("x"))!;

beforeEach(() => {
  vi.clearAllMocks();
  qc = new QueryClient();
  qc.setQueryData(chatKeys.sessions("x"), [
    s("a", { lastMessageAt: "2026-11-08T10:00:00Z" }),
    s("b", { lastMessageAt: "2026-11-08T09:00:00Z" }),
    s("c", { lastMessageAt: "2026-11-08T08:00:00Z", projectId: "p1" }),
  ]);
  qc.setQueryData<ChatProjectDTO[]>(
    ["chat", "projects"],
    [{ id: "p1", name: "P", instructions: "", pinnedRefs: [], createdAt: "" }],
  );
});

describe("sidebar cache writes", () => {
  it("pinning writes the reply into the list, pinned first, without a refetch", async () => {
    api.put.mockResolvedValue({ ...s("b"), pinned: true });
    const { result } = renderHook(() => useSetSessionPinned(), { wrapper });
    result.current.mutate({ sessionId: "b", pinned: true });
    await waitFor(() => expect(listOf()[0].id).toBe("b"));
    expect(listOf()[0].pinned).toBe(true);
    expect(api.get).not.toHaveBeenCalled();
    expect(groupSessions(listOf(), new Date(2026, 10, 8, 12)).pinned.map((x) => x.id)).toEqual([
      "b",
    ]);
  });

  it("moving into a project regroups at once", async () => {
    api.put.mockResolvedValue({ ...s("a"), projectId: "p1" });
    const { result } = renderHook(() => useSetSessionProject(), { wrapper });
    result.current.mutate({ sessionId: "a", projectId: "p1" });
    await waitFor(() => expect(listOf().find((x) => x.id === "a")?.projectId).toBe("p1"));
    const groups = groupSessions(listOf(), new Date(2026, 10, 8, 12));
    expect(groups.byProject.get("p1")?.map((x) => x.id)).toEqual(["a", "c"]);
    expect(groups.today.map((x) => x.id)).toEqual(["b"]);
  });

  it("deleting a project detaches its chats into the date groups", async () => {
    api.delete.mockResolvedValue(undefined);
    qc.setQueryData(["chat", "session", "c"], { ...s("c", { projectId: "p1" }) });
    const { result } = renderHook(() => useDeleteProject(), { wrapper });
    result.current.mutate("p1");
    await waitFor(() =>
      expect(qc.getQueryData<ChatProjectDTO[]>(["chat", "projects"])).toEqual([]),
    );
    expect(listOf().find((x) => x.id === "c")?.projectId).toBeNull();
    expect(
      qc.getQueryData<{ projectId: string | null }>(["chat", "session", "c"])?.projectId,
    ).toBeNull();
    expect(groupSessions(listOf(), new Date(2026, 10, 8, 12)).today.map((x) => x.id)).toEqual([
      "a",
      "b",
      "c",
    ]);
  });

  it("renaming and deleting a chat update the list", async () => {
    api.patch.mockResolvedValue(s("b", { title: "Budget" }));
    api.delete.mockResolvedValue(undefined);
    const rename = renderHook(() => useRenameChatSession("x"), { wrapper });
    rename.result.current.mutate({ sessionId: "b", title: "Budget" });
    await waitFor(() => expect(listOf().find((x) => x.id === "b")?.title).toBe("Budget"));
    const del = renderHook(() => useDeleteChatSession("x"), { wrapper });
    qc.setQueryData(["chat", "session", "a"], s("a"));
    qc.setQueryData(chatKeys.messages("a"), []);
    del.result.current.mutate("a");
    await waitFor(() => expect(listOf().map((x) => x.id)).toEqual(["b", "c"]));
    expect(qc.getQueryData(["chat", "session", "a"])).toBeUndefined();
    expect(qc.getQueryData(chatKeys.messages("a"))).toBeUndefined();
  });

  it("saves a project: POST creates and appends, PATCH sends only what was given", async () => {
    api.post.mockResolvedValue({
      id: "p2",
      name: "N",
      instructions: "",
      pinnedRefs: [],
      createdAt: "",
    });
    api.patch.mockResolvedValue({
      id: "p1",
      name: "Renamed",
      instructions: "",
      pinnedRefs: [],
      createdAt: "",
    });
    const { result } = renderHook(() => useSaveProject(), { wrapper });
    result.current.mutate({ name: "N", instructions: "i", pinnedRefs: [] });
    await waitFor(() =>
      expect(qc.getQueryData<ChatProjectDTO[]>(["chat", "projects"])).toHaveLength(2),
    );
    expect(api.post).toHaveBeenCalledWith("/chat/projects", {
      name: "N",
      instructions: "i",
      pinnedRefs: [],
    });
    result.current.mutate({ id: "p1", name: "Renamed" });
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/chat/projects/p1", { name: "Renamed" }),
    );
    await waitFor(() =>
      expect(qc.getQueryData<ChatProjectDTO[]>(["chat", "projects"])?.[0].name).toBe("Renamed"),
    );
  });
});
