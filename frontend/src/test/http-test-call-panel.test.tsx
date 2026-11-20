import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HttpTestCallPanel } from "@/components/http-test-call-panel";
import { defaultTestTool, exampleArguments, orderTestTools } from "@/lib/http-tools";
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
  it("starts on the first read endpoint, or on the first endpoint without one", () => {
    expect(defaultTestTool(TOOLS)).toBe("get_ticket");
    expect(defaultTestTool([TOOLS[0], { ...TOOLS[2], method: "head" }])).toBe("list_tickets");
    expect(defaultTestTool([TOOLS[0]])).toBe("create_ticket");
    expect(defaultTestTool([])).toBe("");
  });

  it("lists read endpoints first and keeps the order within each group", () => {
    const tools = [
      { name: "a_post", method: "POST", paramSchema: {} },
      { name: "b_get", method: "get", paramSchema: {} },
      { name: "c_delete", method: "DELETE", paramSchema: {} },
      { name: "d_head", method: "HEAD", paramSchema: {} },
      { name: "e_put", method: "PUT", paramSchema: {} },
    ];
    expect(orderTestTools(tools).map((x) => x.name)).toEqual([
      "b_get",
      "d_head",
      "a_post",
      "c_delete",
      "e_put",
    ]);
    render(<HttpTestCallPanel tools={tools} run={vi.fn()} />);
    const select = screen.getByLabelText("Endpoint to test") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      "b_get",
      "d_head",
      "a_post",
      "c_delete",
      "e_put",
    ]);
    expect(select.value).toBe("b_get");
  });

  it("fills only the required arguments with a placeholder of their type", () => {
    expect(
      exampleArguments({
        type: "object",
        required: ["s", "n", "i", "b", "a", "o", "e", "u"],
        properties: {
          s: { type: "string" },
          n: { type: "number" },
          i: { type: "integer" },
          b: { type: "boolean" },
          a: { type: "array", items: { type: "string" } },
          o: { type: "object" },
          e: { type: "string", enum: ["open", "closed"] },
          u: {},
          optional: { type: "string" },
        },
      }),
    ).toEqual({ s: "", n: 0, i: 0, b: false, a: [], o: {}, e: "open", u: "" });
    expect(exampleArguments({ type: "object", properties: { q: { type: "string" } } })).toEqual({});
    expect(exampleArguments({ required: "nope" })).toEqual({});
    expect(exampleArguments({ required: ["id"] })).toEqual({ id: "" });
  });

  it("prefills the arguments of the chosen endpoint", () => {
    render(<HttpTestCallPanel tools={TOOLS} run={vi.fn()} />);
    const args = screen.getByLabelText("Arguments (JSON)") as HTMLTextAreaElement;
    expect(JSON.parse(args.value)).toEqual({ id: "" });
    fireEvent.change(screen.getByLabelText("Endpoint to test"), {
      target: { value: "list_tickets" },
    });
    expect(JSON.parse(args.value)).toEqual({});
  });

  it("keeps what was typed for an endpoint when switching away and back", () => {
    render(<HttpTestCallPanel tools={TOOLS} run={vi.fn()} />);
    const args = screen.getByLabelText("Arguments (JSON)") as HTMLTextAreaElement;
    fireEvent.change(args, { target: { value: '{"id": "42"}' } });
    fireEvent.change(screen.getByLabelText("Endpoint to test"), {
      target: { value: "create_ticket" },
    });
    expect(JSON.parse(args.value)).toEqual({});
    fireEvent.change(screen.getByLabelText("Endpoint to test"), {
      target: { value: "get_ticket" },
    });
    expect(args.value).toBe('{"id": "42"}');
  });

  it("sends the prefilled arguments when nothing was typed", async () => {
    const run = vi.fn().mockResolvedValue(result({}));
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    await waitFor(() => expect(run).toHaveBeenCalledWith("get_ticket", { id: "" }));
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
    const run = vi
      .fn()
      .mockResolvedValue(result({ ok: false, errorClass, status: 401, message: "" }));
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("shows the server's reason for a blocked call when it gives one", async () => {
    const reason =
      "This connection's base URL contains a login. Delete this connection and create it again with the login in the login fields.";
    const run = vi
      .fn()
      .mockResolvedValue(
        result({ ok: false, errorClass: "blocked", status: null, message: reason }),
      );
    render(<HttpTestCallPanel tools={TOOLS} run={run} />);
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(screen.queryByText("This address is not allowed.")).not.toBeInTheDocument();
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
