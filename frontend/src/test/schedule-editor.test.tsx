import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TriggerDTO } from "@/lib/hooks";

const m = vi.hoisted(() => ({
  triggers: [] as TriggerDTO[] | undefined,
  orgTimezone: "Europe/Berlin" as string | undefined,
  create: vi.fn(),
  update: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("@/lib/hooks", () => ({
  useAgentTriggers: () => ({ data: m.triggers }),
  useCreateAgentTrigger: () => ({ mutate: m.create, isPending: false }),
  useUpdateAgentTrigger: () => ({ mutate: m.update, isPending: false }),
  useDeleteAgentTrigger: () => ({ mutate: vi.fn(), isPending: false }),
  useAuth: () => ({ data: { memberId: "me", organisationTimezone: m.orgTimezone } }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: (...a: unknown[]) => m.toastError(...a) },
}));

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to, hash }: { children: ReactNode; to: string; hash?: string }) => (
    <a href={hash ? `${to}#${hash}` : to}>{children}</a>
  ),
}));

import { ScheduleEditor } from "@/components/schedule-editor";

const saved: TriggerDTO = {
  id: "t1",
  agentId: "a1",
  kind: "cron",
  taskText: "Scheduled run for Ada",
  enabled: true,
  cronExpression: "0 7 * * 1-5",
  timezone: "America/New_York",
  nextRunAt: null,
  lastRunAt: null,
  eventSource: null,
  eventType: null,
  webhookUrl: null,
  createdByMemberId: "me",
  createdByName: "Me",
  actsForCreator: false,
  needsConfirmation: false,
};

function zoneInput(): HTMLInputElement {
  return screen.getByLabelText("Timezone") as HTMLInputElement;
}

beforeEach(() => {
  m.triggers = [];
  m.orgTimezone = "Europe/Berlin";
  m.create.mockReset();
  m.update.mockReset();
  m.toastError.mockReset();
});

describe("ScheduleEditor timezone", () => {
  it("starts a new schedule in the organisation's timezone and sends it", () => {
    render(<ScheduleEditor initial="On-Demand" agentName="Ada" agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Time scheduled" }));
    expect(zoneInput().value).toBe("Europe/Berlin");
    expect(screen.getByText(/\(Europe\/Berlin\)$/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(m.create).toHaveBeenCalledWith(
      expect.objectContaining({ agentId: "a1", timezone: "Europe/Berlin" }),
      expect.anything(),
    );
  });

  it("falls back to this browser's zone when the server names none", () => {
    m.orgTimezone = undefined;
    render(<ScheduleEditor initial="On-Demand" agentName="Ada" agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Time scheduled" }));
    expect(zoneInput().value).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
  });

  it("shows a saved schedule's own zone and sends a newly picked one", () => {
    m.triggers = [saved];
    render(<ScheduleEditor initial="" agentName="Ada" agentId="a1" />);
    expect(zoneInput().value).toBe("America/New_York");
    expect(screen.getByText("Every weekday at 7:00 (America/New_York)")).toBeInTheDocument();
    fireEvent.change(zoneInput(), { target: { value: "Asia/Tokyo" } });
    expect(screen.getByText("Every weekday at 7:00 (Asia/Tokyo)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(m.update).toHaveBeenCalledWith(
      expect.objectContaining({
        triggerId: "t1",
        cronExpression: "0 7 * * 1-5",
        timezone: "Asia/Tokyo",
      }),
      expect.anything(),
    );
  });

  it("shows a schedule saved without a zone as UTC", () => {
    m.triggers = [{ ...saved, timezone: null }];
    render(<ScheduleEditor initial="" agentName="Ada" agentId="a1" />);
    expect(zoneInput().value).toBe("UTC");
    expect(screen.getByText("Every weekday at 7:00 (UTC)")).toBeInTheDocument();
  });

  it("never sends a half-typed zone", () => {
    m.triggers = [saved];
    render(<ScheduleEditor initial="" agentName="Ada" agentId="a1" />);
    fireEvent.change(zoneInput(), { target: { value: "Europe/Ber" } });
    fireEvent.blur(zoneInput());
    expect(zoneInput().value).toBe("America/New_York");
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(m.update).toHaveBeenCalledWith(
      expect.objectContaining({ timezone: "America/New_York" }),
      expect.anything(),
    );
  });

  it("points to the settings while the organisation's zone is still UTC", () => {
    m.orgTimezone = "UTC";
    render(<ScheduleEditor initial="On-Demand" agentName="Ada" agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Time scheduled" }));
    const link = screen.getByRole("link", { name: /set it in settings/i });
    expect(link).toHaveAttribute("href", "/settings#organization");
    expect(screen.getByText(/Organisation timezone is UTC/)).toBeInTheDocument();
  });

  it("gives no hint once the organisation has a zone", () => {
    render(<ScheduleEditor initial="On-Demand" agentName="Ada" agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Time scheduled" }));
    expect(screen.queryByRole("link", { name: /set it in settings/i })).toBeNull();
  });
});

describe("ScheduleEditor with a schedule that loads after mount", () => {
  it("shows and saves the trigger's cron, not the agent's free-text value", () => {
    m.triggers = undefined;
    const view = render(<ScheduleEditor initial="Weekly schedule" agentName="Ada" agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Time scheduled" }));
    m.triggers = [saved];
    view.rerender(<ScheduleEditor initial="Weekly schedule" agentName="Ada" agentId="a1" />);
    expect(screen.getByLabelText("Frequency")).toHaveValue("weekly");
    expect(screen.getByText("Every weekday at 7:00 (America/New_York)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(m.update).toHaveBeenCalledWith(
      expect.objectContaining({ triggerId: "t1", cronExpression: "0 7 * * 1-5" }),
      expect.anything(),
    );
  });

  it("names the server's reason when a save is refused", () => {
    m.triggers = [saved];
    m.update.mockImplementation((_vars, opts: { onError: (e: unknown) => void }) =>
      opts.onError(new Error("Bad Gateway")),
    );
    render(<ScheduleEditor initial="" agentName="Ada" agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Time scheduled" }));
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(m.toastError).toHaveBeenCalledWith(
      "Couldn't update schedule",
      expect.objectContaining({ description: "Bad Gateway" }),
    );
  });
});
