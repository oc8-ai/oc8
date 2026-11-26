import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The "Per employee" tab charts who spent the tokens over time and lists the
// totals per person, with readable names for the rows that are no single
// member (unknown person, no person, everyone beyond the top ten).

const get = vi.fn((_path: string): Promise<unknown> => Promise.resolve(emptyTimeseries()));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: (path: string) => get(path) },
}));

import { bucketFormatters, resolveTimeZone } from "@/lib/costs";
import { useUsageTimeseries } from "@/lib/hooks";
import { MembersTab } from "@/routes/costs";

const BUCKETS = ["2026-11-25T00:00:00+01:00", "2026-11-26T00:00:00+01:00"];

function emptyTimeseries() {
  return { range: "7d", bucket: "day", timezone: "Europe/Berlin", buckets: BUCKETS, series: [] };
}

function timeseriesWithPeople() {
  return {
    ...emptyTimeseries(),
    series: [
      { key: "m-1", memberId: "m-1", name: "Anna Schmidt", total: 3_000, points: [1_000, 2_000] },
      { key: "m-2", memberId: "m-2", name: null, total: 1_500, points: [500, 1_000] },
      { key: "others", memberId: null, name: null, total: 800, points: [300, 500] },
      { key: "none", memberId: null, name: null, total: 200, points: [200, 0] },
    ],
  };
}

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

function noRetryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(() => {
  get.mockClear();
  get.mockImplementation(() => Promise.resolve(emptyTimeseries()));
});

describe("useUsageTimeseries", () => {
  it("asks for the range it is given", async () => {
    renderHook(() => useUsageTimeseries("30d"), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/usage/timeseries?range=30d"));
  });
});

describe("MembersTab", () => {
  it("shows the last seven days first and switches to 24 hours", async () => {
    render(<MembersTab />, { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/usage/timeseries?range=7d"));
    expect(screen.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "24 h" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/usage/timeseries?range=24h"));
    expect(screen.getByRole("button", { name: "24 h" })).toHaveAttribute("aria-pressed", "true");
  });

  it("lists each person's total with readable names for the special rows", async () => {
    get.mockImplementation(() => Promise.resolve(timeseriesWithPeople()));
    render(<MembersTab />, { wrapper });

    const table = await screen.findByRole("table");
    const rows = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.textContent);
    expect(rows).toEqual(["Anna Schmidt3.0k", "Unknown person1.5k", "Others800", "No person200"]);
  });

  it("says so when nothing was used in the range instead of drawing an empty chart", async () => {
    render(<MembersTab />, { wrapper });
    expect(await screen.findByText("No token usage in this period.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says the usage could not be loaded when the first request fails", async () => {
    get.mockImplementation(() => Promise.reject(new Error("boom")));
    const client = noRetryClient();
    render(
      <QueryClientProvider client={client}>
        <MembersTab />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Usage could not be loaded.")).toBeInTheDocument();
  });

  it("keeps the chart data when a later refetch fails", async () => {
    get.mockImplementation(() => Promise.resolve(timeseriesWithPeople()));
    const client = noRetryClient();
    render(
      <QueryClientProvider client={client}>
        <MembersTab />
      </QueryClientProvider>,
    );
    await screen.findByRole("table");

    get.mockImplementation(() => Promise.reject(new Error("boom")));
    await client.refetchQueries({ queryKey: ["usage-timeseries", "7d"] });

    await waitFor(() =>
      expect(client.getQueryState(["usage-timeseries", "7d"])?.status).toBe("error"),
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.queryByText("Usage could not be loaded.")).not.toBeInTheDocument();
  });

  it("falls back to UTC when the browser does not know the organisation's time zone", async () => {
    get.mockImplementation(() =>
      Promise.resolve({ ...timeseriesWithPeople(), timezone: "Mars/Olympus_Mons" }),
    );
    render(<MembersTab />, { wrapper });
    expect(await screen.findByText("Times in UTC")).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("bucket time labels", () => {
  // Midnight in Berlin is 08:00 in Tokyo and 23:00 UTC: whatever zone the
  // test runner sits in, the label must follow the zone the response names.
  const berlinMidnight = "2026-11-26T00:00:00+01:00";

  it("shows hours in the response's time zone", () => {
    expect(bucketFormatters("de", "hour", "24h", "Asia/Tokyo").tick(berlinMidnight)).toBe("08:00");
    expect(bucketFormatters("de", "hour", "24h", "Europe/Berlin").tick(berlinMidnight)).toBe(
      "00:00",
    );
  });

  it("shows the day in the response's time zone", () => {
    expect(bucketFormatters("de", "day", "30d", "Europe/Berlin").tick(berlinMidnight)).toBe(
      "26.11.",
    );
    expect(bucketFormatters("de", "day", "30d", "UTC").tick(berlinMidnight)).toBe("25.11.");
  });

  it("keeps a known zone and replaces an unknown one with UTC", () => {
    expect(resolveTimeZone("Asia/Tokyo")).toBe("Asia/Tokyo");
    expect(resolveTimeZone("Mars/Olympus_Mons")).toBe("UTC");
  });
});
