// What a Python run gives back, read from the sandboxed frame. Everything the
// frame posts is untrusted (the code in it is the model's answer): it is
// shape-checked and capped here, and the UI renders it as text only --
// images only as PNG data URLs, tables as text cells.

export type PythonTable = { columns: string[]; rows: string[][] };

export type PythonResult = {
  stdout: string;
  stderr: string;
  /** The last expression's value (its repr), unless it was a table. */
  result: string | null;
  /** matplotlib figures, `data:image/png;base64,…`. */
  images: string[];
  /** pandas DataFrames / Series the code ended with. */
  tables: PythonTable[];
  /** The 30 s wall-clock limit stopped the code. */
  timedOut: boolean;
};

export type PythonFile = { name: string; bytes: ArrayBuffer };

export const LIMITS = {
  /** stdout and stderr, each. */
  text: 100_000,
  result: 100_000,
  images: 10,
  /** One image's data URL. */
  imageChars: 8_000_000,
  tables: 10,
  rows: 200,
  columns: 100,
  cell: 1_000,
  /** "Send result to chat". */
  message: 4_000,
  /** A chat upload offered to the code. */
  fileBytes: 20 * 1024 * 1024,
  /** Wall clock for the code itself (not the first download). */
  timeoutMs: 30_000,
} as const;

export const PNG_PREFIX = "data:image/png;base64,";

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

function capped(value: unknown, max: number): string {
  if (typeof value !== "string") return "";
  return value.length > max ? `${value.slice(0, max)}\n…` : value;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function image(value: unknown): string | null {
  if (typeof value !== "string" || value.length > LIMITS.imageChars) return null;
  if (!value.startsWith(PNG_PREFIX)) return null;
  return BASE64.test(value.slice(PNG_PREFIX.length)) ? value : null;
}

function cell(value: unknown): string {
  return typeof value === "string" ? capped(value, LIMITS.cell) : "";
}

function table(value: unknown): PythonTable | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as { columns?: unknown; rows?: unknown };
  const columns = asArray(raw.columns).slice(0, LIMITS.columns).map(cell);
  if (columns.length === 0) return null;
  const rows = asArray(raw.rows)
    .slice(0, LIMITS.rows)
    .map((row) => {
      const cells = asArray(row).slice(0, columns.length).map(cell);
      while (cells.length < columns.length) cells.push("");
      return cells;
    });
  return { columns, rows };
}

/** A run's output as the frame posted it, made safe to show. */
export function readOutput(raw: unknown, timedOut = false): PythonResult {
  const out = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    stdout: capped(out.stdout, LIMITS.text),
    stderr: capped(out.stderr, LIMITS.text),
    result: typeof out.result === "string" ? capped(out.result, LIMITS.result) : null,
    images: asArray(out.images)
      .slice(0, LIMITS.images)
      .map(image)
      .filter((x): x is string => x !== null),
    tables: asArray(out.tables)
      .slice(0, LIMITS.tables)
      .map(table)
      .filter((x): x is PythonTable => x !== null),
    timedOut,
  };
}

/** A table as tab-separated text (for "Send result to chat"). */
function tableText(t: PythonTable): string {
  return [t.columns, ...t.rows].map((row) => row.join("\t")).join("\n");
}

/** What "Send result to chat" posts: the printed output, the last value and
 *  tables as text, in one fenced block, at most LIMITS.message characters.
 *  Empty when there is nothing to send. */
export function resultMessage(result: PythonResult, intro: string): string {
  const parts = [result.stdout.trimEnd(), result.result ?? "", ...result.tables.map(tableText)]
    .map((p) => p.trim())
    .filter(Boolean);
  if (result.stderr.trim() && parts.length === 0) parts.push(result.stderr.trim());
  if (parts.length === 0) return "";
  const head = `${intro}\n\n\`\`\`text\n`;
  const tail = "\n```";
  const room = LIMITS.message - head.length - tail.length;
  let body = parts.join("\n\n").replace(/```/g, "ˋˋˋ");
  if (body.length > room) body = `${body.slice(0, Math.max(0, room - 1))}…`;
  return head + body + tail;
}
