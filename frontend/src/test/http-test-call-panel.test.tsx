import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HttpTestCallPanel, defaultTestTool } from "@/components/http-test-call-panel";
import type { HttpTestCallResult } from "@/lib/http-tools";

const TOOLS = [
  { name: "create_ticket", method: "POST", paramSchema: { type: "object" } },
  {
    name: "get_ticket",
    method: "GET",
    paramSchema: { type: "object", required: ["id"], properties: { id: { type: "string" } } },
  },
  { name: "list_tickets", method: "GET", paramSchema: { type: "object", properties: {} } },
];

function result(patch: Partial<HttpTestCallResult>): HttpTestCallResult {
  return {
    ok: true,
    status: 200,
    durationMs: 12,
    excerpt: '{"items": []}',
    truncated: false,
    errorClass: null,
    message: "The API answered 200.",
    ...patch,
  };
}

describe("HttpTestCallPanel", () => {
  it("prefers a GET endpoint that needs no arguments", () => {
    expect(defaultTestTool(TOOLS)).toBe("list_tickets");
    expect(defaultTestTool([TOOLS[0]])).toBe("create_ticket");
  });

  it("runs the chosen endpoint with the parsed arguments and shows the answer", async () => {
    const run = vi.fn().mockResolvedValue(result({}));
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.change(screen.getByLabelText("Endpoint to test"), {
      target: { value: "get_ticket" },
    });
    fireEvent.change(screen.getByLabelText("Arguments (JSON)"), {
      target: { value: '{"id": "7"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    await waitFor(() => expect(run).toHaveBeenCalledWith("get_ticket", { id: "7" }));
    expect(await screen.findByText("The call worked.")).toBeInTheDocument();
    expect(screen.getByText('{"items": []}')).toBeInTheDocument();
  });

  it("refuses arguments that are not a JSON object", () => {
    const run = vi.fn();
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.change(screen.getByLabelText("Arguments (JSON)"), { target: { value: "[1" } });
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(run).not.toHaveBeenCalled();
    expect(screen.getByText("The arguments must be a JSON object.")).toBeInTheDocument();
  });

  it.each([
    ["auth", "The API refused the login. Check the login."],
    [
      "token",
      "No access token: the token URL refused or did not answer. The API itself was not called.",
    ],
    ["blocked", "This address is not allowed."],
    ["unreachable", "The API is not reachable."],
    ["http", "The API answered with an error."],
  ] as const)("explains the %s error class", async (errorClass, text) => {
    const run = vi.fn().mockResolvedValue(result({ ok: false, errorClass, status: 401 }));
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("says when the answer was cut", async () => {
    const run = vi.fn().mockResolvedValue(result({ truncated: true }));
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(
      await screen.findByText("Only the beginning of the answer is shown."),
    ).toBeInTheDocument();
  });

  it("says the call could not be made, and why, when the API never answered", async () => {
    const run = vi.fn().mockResolvedValue(
      result({
        ok: false,
        status: null,
        errorClass: "http",
        message: "Header 'X-Note' can only carry ASCII text.",
      }),
    );
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(await screen.findByText("The call could not be made.")).toBeInTheDocument();
    expect(screen.getByText("Header 'X-Note' can only carry ASCII text.")).toBeInTheDocument();
    expect(screen.queryByText("The API answered with an error.")).not.toBeInTheDocument();
  });

  it("warns that a call other than GET can change data", () => {
    render(<HttpTestCallPanel tools={TOOLS} run={vi.fn()} />);
    expect(screen.queryByText("This call can change data in the API.")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Endpoint to test"), {
      target: { value: "create_ticket" },
    });
    expect(screen.getByText("This call can change data in the API.")).toBeInTheDocument();
  });

  it("explains an empty endpoint list", () => {
    render(<HttpTestCallPanel tools={[]} run={vi.fn()} />);
    expect(screen.getByText("Give each endpoint a name first.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run test call" })).toBeDisabled();
  });
});
