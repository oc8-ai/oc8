import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { renameMock, deleteMock } = vi.hoisted(() => ({ renameMock: vi.fn(), deleteMock: vi.fn() }));

vi.mock("@/lib/hooks-chat", () => ({
  useRenameChatSession: () => ({ mutate: renameMock, isPending: false }),
  useDeleteChatSession: () => ({ mutate: deleteMock }),
}));

import { SessionPicker } from "@/components/conversation/session-picker";

const sessions = [
  {
    id: "s1",
    agentId: "agent-1",
    title: "VPN Tickets",
    createdAt: "2026-08-27T00:00:00Z",
  },
  { id: "s2", agentId: "agent-1", title: "", createdAt: "2026-08-26T00:00:00Z" },
] as never;

function renderPicker(sessionId: string | null = "s1", onSelect = vi.fn()) {
  render(<SessionPicker sessions={sessions} sessionId={sessionId} onSelect={onSelect} />);
  return onSelect;
}

// Radix's trigger opens on pointerdown, which fireEvent.click does not send.
function openPicker(name = "Switch chat") {
  fireEvent.pointerDown(screen.getByRole("button", { name }), { button: 0 });
}

describe("SessionPicker", () => {
  beforeEach(() => {
    renameMock.mockReset();
    deleteMock.mockReset();
  });

  it("shows the current session's title on the trigger", () => {
    renderPicker("s1");
    expect(screen.getByRole("button", { name: "Switch chat" })).toHaveTextContent("VPN Tickets");
  });

  it("preselects nothing when there is no current session", () => {
    renderPicker(null);
    const trigger = screen.getByRole("button", { name: "Switch chat" });
    // No chat selected is a new chat (E2E E), not an untitled one.
    expect(trigger).toHaveTextContent("New chat");
    expect(trigger).not.toHaveTextContent("VPN Tickets");
  });

  it("lists every chat as a menu radio item, the open one checked", () => {
    renderPicker("s1");
    openPicker();
    const items = screen.getAllByRole("menuitemradio");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("VPN Tickets");
    expect(items[0]).toHaveAttribute("aria-checked", "true");
    expect(items[1]).toHaveTextContent("Untitled chat");
  });

  it("works by keyboard: open with Enter, arrows move, Enter opens a chat", async () => {
    const onSelect = renderPicker("s1");
    const trigger = screen.getByRole("button", { name: "Switch chat" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const items = await screen.findAllByRole("menuitemradio");
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(items[1]));
    fireEvent.keyDown(items[1], { key: "Enter" });
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("s2"));
  });

  it("renames the open chat in a dialog", async () => {
    renderPicker("s1");
    openPicker();
    fireEvent.click(screen.getByRole("menuitem", { name: /Rename this chat/ }));
    const input = await screen.findByRole("textbox", { name: "Chat title" });
    expect(input).toHaveValue("VPN Tickets");
    fireEvent.change(input, { target: { value: "Zugriff Laufwerk" } });
    fireEvent.click(screen.getByRole("button", { name: "Save title" }));
    expect(renameMock).toHaveBeenCalledWith(
      { sessionId: "s1", title: "Zugriff Laufwerk" },
      expect.anything(),
    );
  });

  it("says inline when a rename fails", async () => {
    renameMock.mockImplementation((_v, opts: { onError: () => void }) => opts.onError());
    renderPicker("s1");
    openPicker();
    fireEvent.click(screen.getByRole("menuitem", { name: /Rename this chat/ }));
    const input = await screen.findByRole("textbox", { name: "Chat title" });
    fireEvent.change(input, { target: { value: "New" } });
    fireEvent.click(screen.getByRole("button", { name: "Save title" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The chat was not renamed.");
  });

  it("a blank title does not rename", async () => {
    renderPicker("s1");
    openPicker();
    fireEvent.click(screen.getByRole("menuitem", { name: /Rename this chat/ }));
    const input = await screen.findByRole("textbox", { name: "Chat title" });
    fireEvent.change(input, { target: { value: "  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save title" }));
    expect(renameMock).not.toHaveBeenCalled();
  });

  it("deleting asks for confirmation before deleting", async () => {
    renderPicker("s1");
    openPicker();
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete this chat/ }));
    expect(deleteMock).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("s1", expect.anything()));
  });

  it("says inline when a delete fails", async () => {
    deleteMock.mockImplementation((_id, opts: { onError: () => void }) => opts.onError());
    renderPicker("s1");
    openPicker();
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete this chat/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The chat was not deleted.");
  });

  it("cancelling the confirmation leaves the session as it was", async () => {
    renderPicker("s1");
    openPicker();
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete this chat/ }));
    fireEvent.click(await screen.findByRole("button", { name: /cancel/i }));
    expect(deleteMock).not.toHaveBeenCalled();
  });
});
