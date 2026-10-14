import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FollowupCard, ResponsibilityCard } from "@/components/copilot-cards";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";

describe("copilot cards", () => {
  it("FollowupCard marks a research follow-up as read-only", () => {
    render(
      <FollowupCard
        props={{
          id: "f2",
          responsibilityTitle: "Kunde X",
          kind: "cron",
          when: "2026-10-06T09:00:00+02:00",
          timezone: "Europe/Berlin",
          endsAt: "2026-12-01T00:00:00+01:00",
          purpose: "research",
        }}
      />,
    );
    expect(screen.getByText(/Research · read-only/)).toBeInTheDocument();
  });

  it("FollowupCard shows no research badge for a check-in", () => {
    render(
      <FollowupCard
        props={{
          id: "f3",
          responsibilityTitle: "x",
          kind: "once",
          when: "2026-10-06T09:00:00+02:00",
        }}
      />,
    );
    expect(screen.queryByText(/Research · read-only/)).not.toBeInTheDocument();
  });

  it("ResponsibilityCard shows title, goal and state", () => {
    render(
      <ResponsibilityCard
        props={{
          id: "r1",
          title: "Chase invoice",
          goal: "Get it paid",
          state: "waiting",
          nextStep: "Ping",
        }}
      />,
    );
    expect(screen.getByText("Chase invoice")).toBeInTheDocument();
    expect(screen.getByText("Get it paid")).toBeInTheDocument();
    expect(screen.getByText(/waiting/i)).toBeInTheDocument();
  });
  it("FollowupCard shows timezone, end date and recurring label", () => {
    render(
      <FollowupCard
        props={{
          id: "f1",
          responsibilityTitle: "Chase invoice",
          kind: "cron",
          when: "2026-10-05T09:00:00+02:00",
          timezone: "Europe/Berlin",
          endsAt: "2026-12-01T00:00:00+01:00",
        }}
      />,
    );
    expect(screen.getByText(/Europe\/Berlin/)).toBeInTheDocument();
    expect(screen.getByText(/recurring/i)).toBeInTheDocument();
    expect(
      screen.getByText(/2026/, { selector: "[data-testid=followup-ends]" }),
    ).toBeInTheDocument();
  });
  it("FollowupCard says once and tolerates null timezone and bad dates", () => {
    render(
      <FollowupCard
        props={{
          id: "f2",
          responsibilityTitle: "X",
          kind: "once",
          when: "not-a-date",
          timezone: null,
          endsAt: null,
        }}
      />,
    );
    expect(screen.getByText(/once/i)).toBeInTheDocument();
    expect(screen.getByText(/not-a-date/)).toBeInTheDocument();
  });
  it("is registered in the run component registry", () => {
    expect(RUN_COMPONENT_REGISTRY.responsibility_card).toBe(ResponsibilityCard);
    expect(RUN_COMPONENT_REGISTRY.followup_card).toBe(FollowupCard);
  });
});
