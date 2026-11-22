import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GuardrailUsageLimits } from "@/components/guardrail-usage-limits";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";

const base: GuardrailValue = {
  read: true,
  modify: true,
  approvalActions: [],
  approvalEur: null,
  only: [],
  conditions: [],
  limits: [{ tools: ["mail_send", "mail_reply"], max: 50, per: "day" }],
};
const catalog = ["mail_get", "mail_send", "mail_reply", "mail_forward"];

describe("GuardrailUsageLimits", () => {
  it("shows each limit with its tools, max and today's usage", () => {
    render(
      <GuardrailUsageLimits
        value={base}
        onChange={vi.fn()}
        toolCatalog={catalog}
        usage={[
          {
            toolKey: "email",
            tools: ["mail_reply", "mail_send"],
            per: "day",
            used: 12,
            resetsAt: "2026-11-23T00:00:00+00:00",
          },
        ]}
      />,
    );
    expect(screen.getByText("mail_send")).toBeInTheDocument();
    expect(screen.getByText("mail_reply")).toBeInTheDocument();
    expect((screen.getByRole("spinbutton") as HTMLInputElement).value).toBe("50");
    expect(screen.getByText(/12 \/ 50/)).toBeInTheDocument();
  });

  it("shows 0 used when nothing was counted yet", () => {
    render(
      <GuardrailUsageLimits value={base} onChange={vi.fn()} toolCatalog={catalog} usage={[]} />,
    );
    expect(screen.getByText(/0 \/ 50/)).toBeInTheDocument();
  });

  it("changes max", () => {
    const onChange = vi.fn();
    render(<GuardrailUsageLimits value={base} onChange={onChange} toolCatalog={catalog} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "20" } });
    expect(onChange).toHaveBeenCalledWith({
      ...base,
      limits: [{ tools: ["mail_send", "mail_reply"], max: 20, per: "day" }],
    });
  });

  it("removes a limit", () => {
    const onChange = vi.fn();
    render(<GuardrailUsageLimits value={base} onChange={onChange} toolCatalog={catalog} />);
    fireEvent.click(screen.getByRole("button", { name: /remove limit|limit entfernen/i }));
    expect(onChange).toHaveBeenCalledWith({ ...base, limits: [] });
  });

  it("adds a limit on a real tool, never an empty one", () => {
    const onChange = vi.fn();
    render(
      <GuardrailUsageLimits
        value={{ ...base, limits: [] }}
        onChange={onChange}
        toolCatalog={catalog}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /add limit|limit hinzufügen/i }));
    const next = onChange.mock.calls[0][0] as GuardrailValue;
    expect(next.limits).toHaveLength(1);
    expect(next.limits[0].tools.length).toBeGreaterThan(0);
    expect(catalog).toContain(next.limits[0].tools[0]);
  });

  it("adds another tool to a limit and removes one", () => {
    const onChange = vi.fn();
    render(<GuardrailUsageLimits value={base} onChange={onChange} toolCatalog={catalog} />);
    fireEvent.change(screen.getByRole("combobox", { name: /count also|auch zählen/i }), {
      target: { value: "mail_forward" },
    });
    expect(onChange).toHaveBeenLastCalledWith({
      ...base,
      limits: [{ tools: ["mail_send", "mail_reply", "mail_forward"], max: 50, per: "day" }],
    });
    fireEvent.click(screen.getByRole("button", { name: /mail_reply/ }));
    expect(onChange).toHaveBeenLastCalledWith({
      ...base,
      limits: [{ tools: ["mail_send"], max: 50, per: "day" }],
    });
  });

  it("tolerates a value without limits", () => {
    const { limits: _dropped, ...legacy } = base;
    render(
      <GuardrailUsageLimits
        value={legacy as GuardrailValue}
        onChange={vi.fn()}
        toolCatalog={catalog}
      />,
    );
    expect(screen.getByRole("button", { name: /add limit|limit hinzufügen/i })).toBeInTheDocument();
  });

  it("shows the department's max as the one in force when it is lower", () => {
    render(
      <GuardrailUsageLimits
        value={base}
        onChange={vi.fn()}
        toolCatalog={catalog}
        ceiling={[{ tools: ["mail_reply", "mail_send"], max: 20, per: "day" }]}
        usage={[]}
      />,
    );
    expect(screen.getByText(/set by department|von der abteilung festgelegt/i)).toBeInTheDocument();
    expect(screen.getByText(/0 \/ 20/)).toBeInTheDocument();
  });

  it("shows a department limit the agent does not repeat as a read-only row with its usage", () => {
    render(
      <GuardrailUsageLimits
        value={{ ...base, limits: [] }}
        onChange={vi.fn()}
        toolCatalog={catalog}
        ceiling={[{ tools: ["mail_forward"], max: 5, per: "hour" }]}
        usage={[
          {
            toolKey: "email",
            tools: ["mail_forward"],
            per: "hour",
            used: 3,
            resetsAt: "2026-11-22T13:00:00+00:00",
          },
        ]}
      />,
    );
    expect(screen.getByText("mail_forward")).toBeInTheDocument();
    expect(screen.getByText(/3 \/ 5/)).toBeInTheDocument();
    expect(screen.getByText(/set by department|von der abteilung festgelegt/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /remove limit|limit entfernen/i })).toBeNull();
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("warns that changing which functions a limit counts restarts its count", () => {
    const { rerender } = render(
      <GuardrailUsageLimits value={base} onChange={vi.fn()} toolCatalog={catalog} usage={[]} />,
    );
    expect(screen.queryByText(/starts again at 0|beginnt wieder bei 0/i)).toBeNull();
    rerender(
      <GuardrailUsageLimits
        value={{ ...base, limits: [{ tools: ["mail_send"], max: 50, per: "day" }] }}
        onChange={vi.fn()}
        toolCatalog={catalog}
        usage={[]}
      />,
    );
    expect(screen.getByText(/starts again at 0|beginnt wieder bei 0/i)).toBeInTheDocument();
  });

  it("says calls count once they reached the tool, not only successful ones", () => {
    render(<GuardrailUsageLimits value={base} onChange={vi.fn()} toolCatalog={catalog} />);
    expect(
      screen.getByText(/even if it reported an error|auch wenn es einen fehler/i),
    ).toBeInTheDocument();
  });
});
