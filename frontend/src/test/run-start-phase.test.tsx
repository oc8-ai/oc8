import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunStartPhase } from "@/components/run-start-phase";
import { isStartPhase, phaseLabel } from "@/lib/run-start-phase";
import { LanguageProvider } from "@/lib/i18n";

function renderLine(phase: string | null | undefined, state = "running") {
  return render(
    <LanguageProvider>
      <RunStartPhase phase={phase} state={state} />
    </LanguageProvider>,
  );
}

describe("RunStartPhase", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.removeItem("oc8-lang");
  });

  it("says the workspace is starting right away while provisioning", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderLine("provisioning");
    expect(screen.getByRole("status")).toHaveTextContent("Arbeitsumgebung wird gestartet …");
    expect(
      screen.queryByText("Das kann beim ersten Lauf nach einer Pause bis zu drei Minuten dauern."),
    ).toBeNull();
  });

  it("adds the patience line after 20 seconds", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderLine("starting");
    act(() => {
      vi.advanceTimersByTime(19_000);
    });
    expect(
      screen.queryByText("Das kann beim ersten Lauf nach einer Pause bis zu drei Minuten dauern."),
    ).toBeNull();
    act(() => {
      vi.advanceTimersByTime(1_500);
    });
    expect(
      screen.getByText("Das kann beim ersten Lauf nach einer Pause bis zu drei Minuten dauern."),
    ).toBeInTheDocument();
  });

  it("speaks English too", () => {
    window.localStorage.setItem("oc8-lang", "en");
    renderLine("provisioning");
    expect(screen.getByText("Starting the workspace …")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(21_000);
    });
    expect(
      screen.getByText("After a quiet spell the first run can take up to three minutes."),
    ).toBeInTheDocument();
  });

  it("shows nothing without a start phase (an in-process run) or once the run ended", () => {
    const { container, rerender } = renderLine(null);
    expect(container).toBeEmptyDOMElement();
    rerender(
      <LanguageProvider>
        <RunStartPhase phase="starting" state="failed" />
      </LanguageProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("knows the start phases", () => {
    expect(isStartPhase("provisioning")).toBe(true);
    expect(isStartPhase("starting")).toBe(true);
    expect(isStartPhase("planning")).toBe(false);
    expect(isStartPhase(null)).toBe(false);
  });
  it("labels a phase in the reader's language, never the raw value for a known one", () => {
    const de = (_en: string, deText: string) => deText;
    const en = (enText: string) => enText;
    expect(phaseLabel("provisioning", de)).toBe("Arbeitsumgebung wird bereitgestellt");
    expect(phaseLabel("starting", de)).toBe("Arbeitsumgebung startet");
    expect(phaseLabel("provisioning", en)).toBe("Preparing the workspace");
    expect(phaseLabel("starting", en)).toBe("Workspace starting");
    expect(phaseLabel("planning", de)).toBe("planning");
  });
});
