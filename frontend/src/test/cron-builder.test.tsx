import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CronBuilder } from "@/components/cron-builder";

function builder(initial: string, onChange = vi.fn()) {
  return render(
    <CronBuilder initial={initial} onChange={onChange} onSave={() => {}} saveLabel="Save" />,
  );
}

describe("CronBuilder follows the expression it is given", () => {
  it("reads a five-field cron into the matching controls", () => {
    builder("*/15 * * * *");
    expect(screen.getByLabelText("Frequency")).toHaveValue("every-n-min");
    expect(screen.getByText("Every 15 minutes")).toBeInTheDocument();
  });

  it("adopts an expression that arrives after mount", () => {
    // The agent's schedule loads asynchronously: the card mounts on the agent's
    // own value and only later learns the trigger's cron. Showing the value it
    // mounted with would describe a schedule the agent does not have.
    const view = builder("0 9 * * 1-5");
    expect(screen.getByLabelText("Frequency")).toHaveValue("weekly");
    view.rerender(
      <CronBuilder initial="*/15 * * * *" onChange={() => {}} onSave={() => {}} saveLabel="Save" />,
    );
    expect(screen.getByLabelText("Frequency")).toHaveValue("every-n-min");
    expect(screen.getByText("Every 15 minutes")).toBeInTheDocument();
  });

  it("keeps an edit the person just made", () => {
    const onChange = vi.fn();
    const view = builder("0 9 * * 1-5", onChange);
    fireEvent.change(screen.getByLabelText("Frequency"), { target: { value: "hourly" } });
    expect(onChange).toHaveBeenCalledWith("0 * * * *");
    view.rerender(
      <CronBuilder initial="0 * * * *" onChange={onChange} onSave={() => {}} saveLabel="Save" />,
    );
    expect(screen.getByLabelText("Frequency")).toHaveValue("hourly");
  });

  it("does not rewrite a cron the controls cannot express", () => {
    // "0 */2 * * *" survives the round trip only as an expression, not as
    // picker state; saving must still send the expression, not a normalised one.
    const view = builder("0 */2 * * *");
    view.rerender(
      <CronBuilder initial="0 */2 * * *" onChange={() => {}} onSave={() => {}} saveLabel="Save" />,
    );
    expect(screen.getByText("0 */2 * * *")).toBeInTheDocument();
  });
});
