import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { renameMock, deleteMock } = vi.hoisted(() => ({ renameMock: vi.fn(), deleteMock: vi.fn() }));

vi.mock("@/lib/hooks-chat", () => ({
  useRenameChatSession: () => ({ mutate: renameMock }),
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
function openPicker(name: string) {
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
    expect(trigger).toHaveTextContent("Untitled chat");
    expect(trigger).not.toHaveTextContent("VPN Tickets");
  });

  it("lists every session, falling back to Untitled for an unnamed one", () => {
    renderPicker("s1");
    openPicker("Switch chat");
    expect(screen.getAllByText(/VPN Tickets/).length).toBeGreaterThan(1);
    expect(screen.getAllByText(/Untitled chat/).length).toBeGreaterThan(0);
  });

  it("renaming commits the new title on Enter", async () => {
    renderPicker("s1");
    openPicker("Switch chat");
    fireEvent.click(screen.getByRole("button", { name: "Rename VPN Tickets" }));
    const input = screen.getByDisplayValue("VPN Tickets");
    fireEvent.change(input, { target: { value: "Zugriff Laufwerk" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(renameMock).toHaveBeenCalledWith({ sessionId: "s1", title: "Zugriff Laufwerk" }),
    );
  });

  it("a blank title on blur does not rename", () => {
    renderPicker("s1");
    openPicker("Switch chat");
    fireEvent.click(screen.getByRole("button", { name: "Rename VPN Tickets" }));
    const input = screen.getByDisplayValue("VPN Tickets");
    fireEvent.change(input, { target: { value: "  " } });
    fireEvent.blur(input);
    expect(renameMock).not.toHaveBeenCalled();
  });

  it("deleting asks for confirmation before deleting", async () => {
    renderPicker("s1");
    openPicker("Switch chat");
    fireEvent.click(screen.getByRole("button", { name: "Delete VPN Tickets" }));
    expect(deleteMock).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("s1", expect.anything()));
  });

  it("cancelling the confirmation leaves the session as it was", async () => {
    renderPicker("s1");
    openPicker("Switch chat");
    fireEvent.click(screen.getByRole("button", { name: "Delete VPN Tickets" }));
    fireEvent.click(await screen.findByRole("button", { name: /cancel/i }));
    expect(deleteMock).not.toHaveBeenCalled();
  });
});
