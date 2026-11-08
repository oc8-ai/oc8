// frontend/src/test/conversation-model-picker.test.tsx
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";

const setModel = vi.hoisted(() => vi.fn());
vi.mock("@/lib/conversation-api", () => ({
  useSessionModels: () => ({
    data: [
      { id: null, label: "Auto", provider: "", locality: "", isDefault: false },
      { id: "m1", label: "GPT X", provider: "openai", locality: "cloud", isDefault: true },
      { id: "m2", label: "Llama", provider: "ollama", locality: "local", isDefault: false },
    ],
  }),
  useSetSessionModel: () => ({ mutate: setModel, isPending: false }),
}));

import { ModelPicker } from "@/components/conversation/model-picker";

beforeEach(() => setModel.mockReset());

describe("ModelPicker", () => {
  it("opens a radiogroup, moves with arrows and picks with Enter", () => {
    render(<ModelPicker sessionId="s" value={null} />);
    const trigger = screen.getByRole("button", { name: /Model: Auto/ });
    fireEvent.click(trigger);
    const group = screen.getByRole("radiogroup", { name: /Model/ });
    const radios = screen.getAllByRole("radio");
    expect(radios[0]).toHaveAttribute("aria-checked", "true");
    expect(radios[0]).toHaveFocus();
    fireEvent.keyDown(group, { key: "ArrowDown" });
    fireEvent.keyDown(group, { key: "ArrowDown" });
    expect(radios[2]).toHaveFocus();
    expect(screen.getByText("Local")).toBeInTheDocument();
    fireEvent.keyDown(group, { key: "Enter" });
    expect(setModel).toHaveBeenCalledWith("m2", expect.anything());
  });

  it("wraps with ArrowUp and picks with Space", () => {
    render(<ModelPicker sessionId="s" value={null} />);
    fireEvent.click(screen.getByRole("button", { name: /Model: Auto/ }));
    const group = screen.getByRole("radiogroup");
    fireEvent.keyDown(group, { key: "ArrowUp" });
    expect(screen.getAllByRole("radio")[2]).toHaveFocus();
    fireEvent.keyDown(group, { key: " " });
    expect(setModel).toHaveBeenCalledWith("m2", expect.anything());
  });

  it("closes on Escape and returns focus to the trigger", () => {
    render(<ModelPicker sessionId="s" value="m1" />);
    const trigger = screen.getByRole("button", { name: /Model: GPT X/ });
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "Escape" });
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("shows the pick at once and reverts with an inline error on a 422", () => {
    render(<ModelPicker sessionId="s" value="m1" />);
    fireEvent.click(screen.getByRole("button", { name: /Model: GPT X/ }));
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "Enter" });
    expect(screen.getByRole("button", { name: /Model: Llama/ })).toBeInTheDocument();
    const [, options] = setModel.mock.calls[0] as [string, { onError: (e: Error) => void }];
    act(() => options.onError(new ApiError("not allowed", 422)));
    expect(screen.getByRole("button", { name: /Model: GPT X/ })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/isn't available for this chat/);
  });

  it("gives the trigger and the options targets of at least 36 px", () => {
    render(<ModelPicker sessionId="s" value={null} />);
    const trigger = screen.getByRole("button", { name: /Model: Auto/ });
    expect(trigger.className).toMatch(/\bh-9\b/);
    fireEvent.click(trigger);
    for (const radio of screen.getAllByRole("radio"))
      expect(radio.className).toMatch(/\bmin-h-11\b/);
  });

  it("closes on a pointerdown outside and when focus leaves", () => {
    render(
      <div>
        <ModelPicker sessionId="s" value={null} />
        <button type="button">elsewhere</button>
      </div>,
    );
    const trigger = screen.getByRole("button", { name: /Model: Auto/ });
    fireEvent.click(trigger);
    fireEvent.pointerDown(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByRole("radiogroup")).toBeNull();
    fireEvent.click(trigger);
    fireEvent.blur(screen.getAllByRole("radio")[0], {
      relatedTarget: screen.getByRole("button", { name: "elsewhere" }),
    });
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });

  it("drops a pick in flight when the chat or its model changes", () => {
    const { rerender } = render(<ModelPicker sessionId="s" value="m1" />);
    fireEvent.click(screen.getByRole("button", { name: /Model: GPT X/ }));
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "Enter" });
    expect(screen.getByRole("button", { name: /Model: Llama/ })).toBeInTheDocument();
    rerender(<ModelPicker sessionId="t" value="m1" />);
    expect(screen.getByRole("button", { name: /Model: GPT X/ })).toBeInTheDocument();
  });
});
