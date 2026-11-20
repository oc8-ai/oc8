import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/lib/i18n";
import type { TriggerDTO } from "@/lib/hooks";

const auth = vi.fn();
const update = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useAuth: () => auth(),
    useUpdateAgentTrigger: () => ({ mutate: update, isPending: false }),
  };
});

import { TriggerOwner } from "@/components/connections/trigger-owner";

const BASE: TriggerDTO = {
  id: "t1",
  agentId: "a1",
  kind: "webhook",
  taskText: "w",
  enabled: true,
  cronExpression: null,
  nextRunAt: null,
  lastRunAt: null,
  eventSource: null,
  eventType: null,
  webhookUrl: "https://example.test/api/v1/webhooks/x",
  createdByMemberId: "anna",
  createdByName: "Anna",
  actsForCreator: false,
  needsConfirmation: false,
};

function renderOwner(trigger: Partial<TriggerDTO> = {}) {
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <LanguageProvider>
        <TriggerOwner agentId="a1" trigger={{ ...BASE, ...trigger }} />
      </LanguageProvider>
    </QueryClientProvider>,
  );
}

const TOGGLE = "Im Namen des Erstellers ausführen";

describe("TriggerOwner", () => {
  beforeEach(() => {
    window.localStorage.setItem("oc8-lang", "de");
    auth.mockReturnValue({ data: { memberId: "anna" } });
    update.mockReset();
  });

  it("names the owner and lets the creator turn acting for them on", async () => {
    renderOwner();
    expect(screen.getByText("Angelegt von Anna")).toBeInTheDocument();
    const toggle = screen.getByLabelText(TOGGLE);
    expect(toggle).toBeEnabled();
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        { agentId: "a1", triggerId: "t1", actsForCreator: true },
        expect.anything(),
      ),
    );
  });

  it("shows the toggle disabled with the reason to everyone else", () => {
    auth.mockReturnValue({ data: { memberId: "ben" } });
    renderOwner({ actsForCreator: true });
    const toggle = screen.getByLabelText(TOGGLE);
    expect(toggle).toBeDisabled();
    expect(toggle).toBeChecked();
    expect(screen.getByText("Nur Anna kann das ändern.")).toBeInTheDocument();
  });

  it("offers the toggle for event triggers too", () => {
    renderOwner({ kind: "event", webhookUrl: null });
    expect(screen.getByLabelText(TOGGLE)).toBeInTheDocument();
  });

  it("has no toggle for a schedule, whose runs always act for its owner", () => {
    renderOwner({ kind: "cron", cronExpression: "0 9 * * *", webhookUrl: null });
    expect(screen.getByText("Angelegt von Anna")).toBeInTheDocument();
    expect(screen.queryByLabelText(TOGGLE)).not.toBeInTheDocument();
  });

  it("asks to save a schedule without owner once again", () => {
    renderOwner({
      kind: "cron",
      cronExpression: "0 9 * * *",
      webhookUrl: null,
      createdByMemberId: null,
      createdByName: null,
    });
    expect(
      screen.getByText("Dieser Zeitplan hat keinen Besitzer. Bitte speicher ihn einmal neu."),
    ).toBeInTheDocument();
  });

  it("asks its creator to save a schedule again after the agent changed", async () => {
    renderOwner({
      kind: "cron",
      cronExpression: "0 9 * * *",
      webhookUrl: null,
      needsConfirmation: true,
    });
    expect(
      screen.getByText(
        "Der Agent wurde geändert. Bitte öffne und speicher den Zeitplan erneut, damit er wieder in deinem Namen arbeitet.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Zeitplan erneut speichern" }));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        { agentId: "a1", triggerId: "t1", cronExpression: "0 9 * * *" },
        expect.anything(),
      ),
    );
  });

  it("shows no confirmation notice while the schedule is confirmed", () => {
    renderOwner({ kind: "cron", cronExpression: "0 9 * * *", webhookUrl: null });
    expect(screen.queryByText(/Der Agent wurde geändert/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Zeitplan erneut speichern" })).toBeNull();
  });

  it("keeps a webhook without owner switched off", () => {
    renderOwner({ createdByMemberId: null, createdByName: null });
    expect(screen.getByLabelText(TOGGLE)).toBeDisabled();
    expect(screen.getByText("Dieser Webhook hat keinen Besitzer.")).toBeInTheDocument();
  });
});
