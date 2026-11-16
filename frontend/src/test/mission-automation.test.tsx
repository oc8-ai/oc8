import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({ triggers: [] as unknown[] }));

vi.mock("@/lib/hooks", () => ({
  useAgentTriggers: () => ({ data: m.triggers }),
  useAutomationEventCatalogue: () => ({
    data: [
      {
        source: "calendar",
        type: "event.created",
        label: "New calendar event",
        description: "A calendar event was created.",
      },
    ],
  }),
  useCreateAgentTrigger: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateAgentAutomation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { MissionAutomation } from "@/components/mission-automation";

describe("MissionAutomation", () => {
  it("uses plain-language mission and start-condition fields", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MissionAutomation
          agentId="agent-1"
          agentName="Ada"
          initialMission="Prepare weekly reports"
        />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("textbox", { name: "Mission" })).toHaveValue("Prepare weekly reports");
    expect(screen.getByRole("combobox", { name: "Start condition" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Mission" }), {
      target: { value: "Keep the team informed" },
    });
    expect(screen.getByRole("textbox", { name: "Mission" })).toHaveValue("Keep the team informed");
    expect(screen.getByText("Autonomy & escalation")).toBeInTheDocument();
  });

  it("names the zone each schedule runs in", () => {
    m.triggers = [
      {
        id: "t1",
        kind: "cron",
        cronExpression: "0 7 * * 1-5",
        timezone: "Europe/Berlin",
        taskText: "a",
      },
      { id: "t2", kind: "cron", cronExpression: "0 9 * * *", timezone: null, taskText: "b" },
    ];
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MissionAutomation agentId="agent-1" agentName="Ada" initialMission="" />
      </QueryClientProvider>,
    );
    expect(screen.getByText(/0 7 \* \* 1-5 \(Europe\/Berlin\)/)).toBeInTheDocument();
    expect(screen.getByText(/0 9 \* \* \* \(UTC\)/)).toBeInTheDocument();
    m.triggers = [];
  });
});
