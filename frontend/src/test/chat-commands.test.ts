import { describe, expect, it } from "vitest";

import {
  chatCommands,
  commandSummary,
  detectCommand,
  matchCommands,
  LOCAL_COMMANDS,
  type ChatCommand,
  type ChatModeDTO,
} from "@/lib/chat-commands";

const MODES: ChatModeDTO[] = [
  {
    key: "ask",
    summary: "Answer from what you already know.",
    allowsTools: false,
    allowsWrites: false,
  },
  {
    key: "plan",
    summary: "Work out the steps and show them.",
    allowsTools: true,
    allowsWrites: false,
  },
  { key: "do", summary: "Carry out what was agreed.", allowsTools: true, allowsWrites: true },
  {
    key: "summarise",
    summary: "Condense into a decision record.",
    allowsTools: false,
    allowsWrites: false,
  },
];
const ALL = chatCommands(MODES, () => true);
const en = (text: string) => text;

describe("the command list", () => {
  it("offers every server-declared mode plus the local ones", () => {
    expect(ALL.map((c) => c.key)).toEqual(["ask", "plan", "do", "summarise", "budget"]);
  });

  it("hides a local command the caller may not use", () => {
    // /budget reads GET /budgets/status, which is budget:view-gated -- offering
    // it to somebody who would be 403'd is the picker lying.
    const restricted = chatCommands(MODES, (p) => p !== "budget:view");
    expect(restricted.map((c) => c.key)).not.toContain("budget");
  });

  it("survives the modes endpoint not having answered yet", () => {
    expect(chatCommands(undefined, () => true).map((c) => c.key)).toEqual(["budget"]);
  });

  it("gives every command a one-line description", () => {
    // §5.2's anti-pattern list, enforced on the picker's own data.
    for (const command of ALL) {
      expect(command.summary.length).toBeGreaterThan(0);
      expect(command.summary).not.toContain("\n");
    }
  });

  it("marks exactly the client-answered commands as local", () => {
    expect(ALL.filter((c) => c.local).map((c) => c.key)).toEqual(["budget"]);
    expect(LOCAL_COMMANDS.every((c) => c.local)).toBe(true);
  });
});

describe("translating a summary", () => {
  it("prefers the known translation over the server's English", () => {
    const de = (_en: string, german?: string) => german ?? _en;
    expect(commandSummary("ask", "Answer from what you already know.", de)).toContain("ohne");
  });

  it("falls back to the server's text for a key it does not know", () => {
    expect(commandSummary("teleport", "Goes somewhere else.", en)).toBe("Goes somewhere else.");
  });
});

describe("detecting the command in a draft", () => {
  it("detects a known command with a body", () => {
    expect(detectCommand("/plan migrate the pipeline", ALL)).toEqual({
      command: expect.objectContaining({ key: "plan" }),
      body: "migrate the pipeline",
    });
  });

  it("detects a bare local command", () => {
    // /budget needs no body -- it is answered here, not sent.
    expect(detectCommand("/budget", ALL)).toEqual({
      command: expect.objectContaining({ key: "budget" }),
      body: "",
    });
  });

  it("it mirrors the backend parser", () => {
    // Every case here is asserted identically in
    // backend/tests/chat/test_modes.py. If one side changes, change both.
    expect(detectCommand("/ask", ALL)).toBeNull(); // bare non-local command
    expect(detectCommand("/etc/passwd is world readable?", ALL)).toBeNull();
    expect(detectCommand("/ASK something", ALL)).toBeNull();
    expect(detectCommand("please /ask about this", ALL)).toBeNull();
    expect(detectCommand(" /ask about this", ALL)).toBeNull();
    expect(detectCommand("/deploy the thing", ALL)).toBeNull();
    expect(detectCommand("", ALL)).toBeNull();
    expect(detectCommand("wie viele Tickets sind offen?", ALL)).toBeNull();
    expect(detectCommand("/do\nclose every solved ticket", ALL)).toEqual({
      command: expect.objectContaining({ key: "do" }),
      body: "close every solved ticket",
    });
  });
});

describe("filtering the picker", () => {
  it("matches on a prefix", () => {
    expect(matchCommands("su", ALL).map((c) => c.key)).toEqual(["summarise"]);
  });

  it("an empty query shows everything, in order", () => {
    expect(matchCommands("", ALL)).toEqual(ALL);
  });

  it("matches case-insensitively so a capitalised draft still finds it", () => {
    expect(matchCommands("PL", ALL).map((c) => c.key)).toEqual(["plan"]);
  });

  it("returns nothing for a query that matches nothing", () => {
    expect(matchCommands("zzz", ALL)).toEqual([] as ChatCommand[]);
  });
});
