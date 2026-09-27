import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StalenessNotice } from "@/routes/agents.$id";

// A fixed "now" so every case's elapsed time is exact, not flaky against
// wall-clock jitter.
const NOW = new Date("2026-09-21T12:00:00Z");

function minutesAgo(min: number): string {
  return new Date(NOW.getTime() - min * 60_000).toISOString();
}

describe("StalenessNotice", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders nothing for a run that heartbeat recently", () => {
    const { container } = render(<StalenessNotice updatedAt={minutesAgo(1)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing right at the noise threshold (under 2 minutes)", () => {
    const { container } = render(<StalenessNotice updatedAt={minutesAgo(1.9)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows elapsed time and a countdown to the reconciler once past 2 minutes", () => {
    render(<StalenessNotice updatedAt={minutesAgo(3)} />);
    expect(screen.getByText(/No response for 3m 0s/)).toBeInTheDocument();
    // ABANDON_AFTER_MS is 10 min, so 3 min stale leaves ~7 min.
    expect(screen.getByText(/about 7 min/)).toBeInTheDocument();
  });

  it("switches wording once the abandon window has fully elapsed", () => {
    render(<StalenessNotice updatedAt={minutesAgo(11)} />);
    expect(screen.getByText(/No response for 11m 0s/)).toBeInTheDocument();
    expect(screen.getByText(/oc8 should be repairing this run now/)).toBeInTheDocument();
  });
});
