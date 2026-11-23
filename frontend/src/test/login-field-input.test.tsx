// Review of F15: a select login field must submit its default untouched, and
// a form's own attributes (disabled, name) reach the control.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LoginFieldInput } from "@/components/connections/login-field-input";

const security = { kind: "select", default: "ssl", options: ["ssl", "starttls"] };

describe("LoginFieldInput", () => {
  it("puts an untouched select's default into the form", () => {
    const onChange = vi.fn();
    render(
      <LoginFieldInput aria-label="Encryption" field={security} value="" onChange={onChange} />,
    );
    expect(onChange).toHaveBeenCalledWith("ssl");
  });

  it("leaves a chosen value alone", () => {
    const onChange = vi.fn();
    render(
      <LoginFieldInput
        aria-label="Encryption"
        field={security}
        value="starttls"
        onChange={onChange}
      />,
    );
    expect(onChange).not.toHaveBeenCalled();
  });

  it("passes disabled and name through to a select and an input", () => {
    render(
      <>
        <LoginFieldInput
          aria-label="Encryption"
          name="incoming_security"
          disabled
          field={security}
          value="ssl"
          onChange={vi.fn()}
        />
        <LoginFieldInput
          aria-label="Host"
          name="incoming_host"
          disabled
          field={{ kind: "text" }}
          value=""
          onChange={vi.fn()}
        />
      </>,
    );
    const select = screen.getByLabelText("Encryption");
    const input = screen.getByLabelText("Host");
    expect(select).toBeDisabled();
    expect(select).toHaveAttribute("name", "incoming_security");
    expect(input).toBeDisabled();
    expect(input).toHaveAttribute("name", "incoming_host");
  });
});
