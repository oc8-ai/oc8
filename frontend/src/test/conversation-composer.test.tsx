// frontend/src/test/conversation-composer.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

const upload = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  uploadChatAttachment: upload,
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({
    data: [{ key: "plan", summary: "Plan", allowsTools: true, allowsWrites: false }],
  }),
}));
vi.mock("@/lib/governance-hooks", () => ({ useCan: () => () => true }));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useContextSearch: () => [{ kind: "knowledge_base", id: "kb1", label: "Handbook" }],
  useSessionModels: () => ({ data: [] }),
  useSetSessionModel: () => ({ mutate: vi.fn() }),
}));

import { Composer, type ComposerProps } from "@/components/conversation/composer";

function Harness(props: Partial<ComposerProps>) {
  const [draft, setDraft] = useState("");
  return (
    <QueryClientProvider client={new QueryClient()}>
      <Composer
        sessionId="s"
        ensureSession={async () => "s"}
        draft={draft}
        onDraftChange={setDraft}
        busy={false}
        onSend={vi.fn()}
        onStop={vi.fn()}
        modelValue={null}
        showModelPicker={false}
        {...props}
      />
    </QueryClientProvider>
  );
}

describe("Composer", () => {
  it("sends on Enter and keeps Shift+Enter as a newline", () => {
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith({ text: "hello", attachmentIds: [], contextRefs: [] });
  });

  it("turns Send into Stop while busy and stops on Escape", () => {
    const onStop = vi.fn();
    render(<Harness busy onStop={onStop} />);
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("textbox", { name: /Message/ }), { key: "Escape" });
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("edits the last message with ArrowUp in an empty composer", () => {
    const onEditLast = vi.fn();
    render(<Harness onEditLast={onEditLast} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: /Message/ }), { key: "ArrowUp" });
    expect(onEditLast).toHaveBeenCalled();
  });

  it("attaches # context with the keyboard", () => {
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    const box = screen.getByRole("textbox", { name: /Message/ }) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "#hand", selectionStart: 5 } });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(screen.getByText("Handbook")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "summarise it" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith({
      text: "summarise it",
      attachmentIds: [],
      contextRefs: [{ kind: "knowledge_base", id: "kb1", label: "Handbook" }],
    });
  });

  it("uploads a pasted image and sends its id", async () => {
    upload.mockResolvedValue({
      id: "att1",
      filename: "shot.png",
      contentType: "image/png",
      sizeBytes: 3,
      isImage: true,
      createdAt: "",
    });
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    const box = screen.getByRole("textbox", { name: /Message/ });
    const file = new File(["abc"], "shot.png", { type: "image/png" });
    fireEvent.paste(box, { clipboardData: { files: [file], items: [] } });
    await waitFor(() => expect(screen.getByText("shot.png")).toBeInTheDocument());
    fireEvent.change(box, { target: { value: "what is this" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith({
      text: "what is this",
      attachmentIds: ["att1"],
      contextRefs: [],
    });
  });

  it("opens the + menu as a keyboard menu", () => {
    render(<Harness />);
    const plus = screen.getByRole("button", { name: /Add/ });
    fireEvent.click(plus);
    const menu = screen.getByRole("menu");
    expect(screen.getByRole("menuitem", { name: /Upload file/ })).toHaveFocus();
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(plus).toHaveFocus();
  });

  it("uploads a dropped file into a chat it creates first", async () => {
    upload.mockResolvedValue({
      id: "att2",
      filename: "notes.pdf",
      contentType: "application/pdf",
      sizeBytes: 3,
      isImage: false,
      createdAt: "",
    });
    const ensureSession = vi.fn(async () => "new");
    render(<Harness sessionId={null} ensureSession={ensureSession} />);
    const file = new File(["abc"], "notes.pdf", { type: "application/pdf" });
    fireEvent.drop(screen.getByRole("textbox", { name: /Message/ }), {
      dataTransfer: { files: [file] },
    });
    await waitFor(() => expect(screen.getByText("notes.pdf")).toBeInTheDocument());
    expect(ensureSession).toHaveBeenCalledTimes(1);
    expect(upload).toHaveBeenCalledWith("new", file);
  });

  it("says inline when a file is too large and does not upload it", () => {
    upload.mockClear();
    render(<Harness />);
    const big = new File(["x"], "huge.mov", { type: "video/quicktime" });
    Object.defineProperty(big, "size", { value: 26 * 1024 * 1024 });
    fireEvent.paste(screen.getByRole("textbox", { name: /Message/ }), {
      clipboardData: { files: [big], items: [] },
    });
    expect(screen.getByRole("alert")).toHaveTextContent(/huge.mov is larger than 25 MB/);
    expect(upload).not.toHaveBeenCalled();
  });

  it("picks a / command with the keyboard", () => {
    render(<Harness />);
    const box = screen.getByRole("textbox", { name: /Message/ }) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "/pl", selectionStart: 3 } });
    expect(screen.getByRole("option", { name: /\/plan/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(box, { key: "Tab" });
    expect(box.value).toBe("/plan ");
  });

  it("addresses an agent with @", () => {
    const onAddressAgent = vi.fn();
    render(<Harness agentItems={[{ id: "a1", label: "Sales" }]} onAddressAgent={onAddressAgent} />);
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "@sa", selectionStart: 3 } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onAddressAgent).toHaveBeenCalledWith("a1");
  });

  it("does not stop on Escape when idle and does not edit with text typed", () => {
    const onStop = vi.fn();
    const onEditLast = vi.fn();
    render(<Harness onStop={onStop} onEditLast={onEditLast} />);
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.keyDown(box, { key: "Escape" });
    fireEvent.change(box, { target: { value: "draft" } });
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(onStop).not.toHaveBeenCalled();
    expect(onEditLast).not.toHaveBeenCalled();
  });

  it("does not send while busy and stops from the button", () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    render(<Harness busy onSend={onSend} onStop={onStop} />);
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "more" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("renders the above, target and trailing slots and extra + items", () => {
    const onSelect = vi.fn();
    render(
      <Harness
        above={<div>question card</div>}
        target={<span>To the running run</span>}
        trailing={<button type="button">Dictate</button>}
        extraPlusItems={[{ id: "kb", label: "Knowledge base", icon: null, onSelect }]}
      />,
    );
    expect(screen.getByText("question card")).toBeInTheDocument();
    expect(screen.getByText("To the running run")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dictate" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Add/ }));
    const menu = screen.getByRole("menu");
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: /Knowledge base/ })).toHaveFocus();
    fireEvent.keyDown(menu, { key: "Enter" });
    expect(onSelect).toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("keeps the composer's own controls at least 36 px", () => {
    render(<Harness />);
    expect(screen.getByRole("button", { name: /Add/ }).className).toMatch(/\bsize-9\b/);
    expect(screen.getByRole("button", { name: "Send" }).className).toMatch(/\bsize-9\b/);
  });
});
