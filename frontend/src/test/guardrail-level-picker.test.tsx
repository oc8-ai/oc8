import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GuardrailLevelPicker } from "@/components/guardrail-level-picker";

function renderPicker(onChange = vi.fn()) {
  render(
    <GuardrailLevelPicker
      value="read_only"
      disabledLevels={["ask_first", "allowed"]}
      disabledReason="set by department"
      scope="member"
      onChange={onChange}
    />,
  );
  return onChange;
}

describe("GuardrailLevelPicker", () => {
  it("shows the current level as a pop-up button", () => {
    renderPicker();
    const button = screen.getByRole("button", { name: /read only/i });
    expect(button).toHaveAttribute("aria-haspopup", "dialog");
    expect(button.querySelector("svg")).not.toBeNull();
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });

  it("opens a radiogroup of four levels with the current one checked and focused", async () => {
    renderPicker();
    fireEvent.click(screen.getByRole("button", { name: /read only/i }));
    const group = await screen.findByRole("radiogroup");
    expect(group).toBeInTheDocument();
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(4);
    const current = screen.getByRole("radio", { name: /read only/i });
    expect(current).toHaveAttribute("aria-checked", "true");
    await waitFor(() => expect(current).toHaveFocus());
  });

  it("moves with the arrow keys and picks with Enter", async () => {
    const onChange = renderPicker();
    fireEvent.click(screen.getByRole("button", { name: /read only/i }));
    const current = await screen.findByRole("radio", { name: /read only/i });
    await waitFor(() => expect(current).toHaveFocus());
    fireEvent.keyDown(current, { key: "ArrowUp" });
    const off = screen.getByRole("radio", { name: /^off/i });
    expect(off).toHaveFocus();
    fireEvent.keyDown(off, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("off");
    await waitFor(() => expect(screen.queryByRole("radiogroup")).toBeNull());
  });

  it("keeps a disabled level focusable, marked and explained, and never picks it", async () => {
    const onChange = renderPicker();
    fireEvent.click(screen.getByRole("button", { name: /read only/i }));
    const current = await screen.findByRole("radio", { name: /read only/i });
    await waitFor(() => expect(current).toHaveFocus());
    fireEvent.keyDown(current, { key: "ArrowDown" });
    const ask = screen.getByRole("radio", { name: /ask me first.*set by department/i });
    expect(ask).toHaveFocus();
    expect(ask).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(ask, { key: "Enter" });
    fireEvent.click(ask);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("closes on Escape and returns focus to the button", async () => {
    renderPicker();
    const button = screen.getByRole("button", { name: /read only/i });
    fireEvent.click(button);
    const current = await screen.findByRole("radio", { name: /read only/i });
    await waitFor(() => expect(current).toHaveFocus());
    fireEvent.keyDown(current, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("radiogroup")).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());
  });
});
