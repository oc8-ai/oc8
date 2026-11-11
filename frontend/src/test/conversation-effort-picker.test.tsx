// frontend/src/test/conversation-effort-picker.test.tsx
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";

const state = vi.hoisted(() => ({
  options: [] as unknown[],
  loading: false,
}));
const setEffort = vi.hoisted(() => vi.fn());
vi.mock("@/lib/conversation-api", () => ({
  useSessionModels: () => ({ data: state.options, isLoading: state.loading }),
  useSetSessionEffort: () => ({ mutate: setEffort, isPending: false }),
}));

import { EffortPicker } from "@/components/conversation/effort-picker";

function options(defaultEffort = "balanced") {
  return [
    {
      id: null,
      label: "Auto",
      provider: "",
      locality: "",
      isDefault: false,
      supportsReasoning: true,
      defaultEffort,
    },
    {
      id: "m1",
      label: "Claude",
      provider: "anthropic",
      locality: "cloud",
      isDefault: true,
      supportsReasoning: true,
      defaultEffort,
    },
    {
      id: "m2",
      label: "Llama",
      provider: "ollama",
      locality: "local",
      isDefault: false,
      supportsReasoning: false,
      defaultEffort: "balanced",
    },
  ];
}

beforeEach(() => {
  state.options = options();
  state.loading = false;
  setEffort.mockReset();
});

describe("EffortPicker", () => {
  it("lists Default · Balanced and the three levels with their hints", () => {
    render(<EffortPicker sessionId="s" modelValue={null} value={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Reasoning: Balanced" }));
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(4);
    expect(radios[0]).toHaveTextContent("Default · Balanced");
    expect(radios[0]).toHaveAttribute("aria-checked", "true");
    expect(radios[1]).toHaveTextContent("FastAnswers right away, barely thinks");
    expect(radios[2]).toHaveTextContent("BalancedThe usual choice");
    expect(radios[3]).toHaveTextContent(
      "ThoroughThinks longer for tricky questions; slower and costs more",
    );
  });

  it("opens toward the composer's left so a phone shows it whole", () => {
    // The control sits right of the model picker; a menu anchored on its left
    // edge ran past a 375px screen and scrolled the page sideways.
    render(<EffortPicker sessionId="s" modelValue={null} value={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Reasoning: Balanced" }));
    const menu = screen.getByRole("radiogroup", { name: "Reasoning" });
    expect(menu).toHaveClass("right-0");
    expect(menu).not.toHaveClass("left-0");
  });

  it("opens rightward instead when the control sits near the screen's left edge", () => {
    render(<EffortPicker sessionId="s" modelValue={null} value={null} />);
    const button = screen.getByRole("button", { name: "Reasoning: Balanced" });
    // A right-anchored w-72 menu would start 200px left of the screen.
    vi.spyOn(button, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 8, y: 600, width: 82, height: 36 }),
    );
    fireEvent.click(button);
    const menu = screen.getByRole("radiogroup", { name: "Reasoning" });
    expect(menu).toHaveClass("left-0");
    expect(menu).not.toHaveClass("right-0");
    expect(menu).toHaveClass("max-w-[calc(100vw-2rem)]");
  });

  it("names a configured value that maps to no level", () => {
    state.options = options("custom");
    render(<EffortPicker sessionId="s" modelValue={null} value={null} />);
    fireEvent.click(screen.getByRole("button", { name: /Reasoning:/ }));
    expect(screen.getAllByRole("radio")[0]).toHaveTextContent("Default · Custom setting");
  });

  it("is hidden when the chat's model cannot think, and the pick comes back", () => {
    const { rerender } = render(<EffortPicker sessionId="s" modelValue="m2" value="thorough" />);
    expect(screen.queryByRole("button", { name: /Reasoning:/ })).toBeNull();
    rerender(<EffortPicker sessionId="s" modelValue="m1" value="thorough" />);
    expect(screen.getByRole("button", { name: "Reasoning: Thorough" })).toBeInTheDocument();
  });

  it("shows a pick at once and reverts with an inline error", () => {
    render(<EffortPicker sessionId="s" modelValue={null} value={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Reasoning: Balanced" }));
    fireEvent.click(screen.getByRole("radio", { name: /Thorough/ }));
    expect(screen.getByRole("button", { name: "Reasoning: Thorough" })).toBeInTheDocument();
    const [level, handlers] = setEffort.mock.calls[0] as [string, { onError: (e: Error) => void }];
    expect(level).toBe("thorough");
    act(() => handlers.onError(new ApiError("boom", 500)));
    expect(screen.getByRole("button", { name: "Reasoning: Balanced" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn't be changed/);
  });

  it("drops the inline error when the chat changes", () => {
    const { rerender } = render(<EffortPicker sessionId="s" modelValue={null} value={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Reasoning: Balanced" }));
    fireEvent.click(screen.getByRole("radio", { name: /Thorough/ }));
    const [, handlers] = setEffort.mock.calls[0] as [string, { onError: (e: Error) => void }];
    act(() => handlers.onError(new ApiError("boom", 500)));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    rerender(<EffortPicker sessionId="s2" modelValue={null} value={null} />);

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("hands a new chat's pick to the caller instead of writing it", () => {
    const onPick = vi.fn();
    render(
      <EffortPicker sessionId={null} agentId="a" modelValue={null} value={null} onPick={onPick} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Reasoning:/ }));
    fireEvent.click(screen.getByRole("radio", { name: /Fast/ }));
    expect(onPick).toHaveBeenCalledWith("fast");
    expect(setEffort).not.toHaveBeenCalled();
  });

  it("shows a reader the chat's level without letting them change it", () => {
    state.options = [];
    render(<EffortPicker sessionId="s" modelValue={null} value="fast" disabled />);
    expect(screen.getByRole("button", { name: "Reasoning: Fast" })).toBeDisabled();
  });

  it("names its control through one catalogue msgid, not a msgid per level", () => {
    // A template-literal msgid is invisible to the i18n export, so no
    // translation could ever reach it.
    const po = (locale: string) =>
      readFileSync(join(process.cwd(), "..", "i18n", `${locale}.po`), "utf8");
    expect(po("de")).toContain('msgid "Reasoning: {level}"\nmsgstr "Denkstufe: {level}"');
    expect(readFileSync(join(process.cwd(), "..", "i18n", "oc8.pot"), "utf8")).toContain(
      'msgid "Reasoning: {level}"',
    );
  });
});
