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
  /** A figure's width and height, each. */
  imagePixels: 4096,
  tables: 10,
  /** Cells over all tables. */
  tableCells: 20_000,
  rows: 200,
  columns: 100,
  cell: 1_000,
  /** Characters of everything a run shows: text, tables and images. */
  total: 2_000_000,
  /** "Send result to chat". */
  message: 4_000,
  /** A chat upload offered to the code. */
  fileBytes: 20 * 1024 * 1024,
  /** Wall clock for the code itself, from when its packages are loaded. */
  timeoutMs: 30_000,
  /** Before that: starting Python and loading the packages the code imports
   *  (a cold Firefox compiles pandas for many seconds). */
  loadTimeoutMs: 120_000,
} as const;

/** The server has no Pyodide files (an image built without them). */
export class PythonNotInstalled extends Error {
  constructor() {
    super("Python is not installed on this server.");
    this.name = "PythonNotInstalled";
  }
}

export const PNG_PREFIX = "data:image/png;base64,";

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

function capped(value: unknown, max: number): string {
  if (typeof value !== "string") return "";
  return value.length > max ? `${value.slice(0, max)}\n…` : value;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/** Width and height from a PNG's IHDR chunk, or null when the bytes do not
 *  start like a PNG. */
function pngSize(base64: string): { width: number; height: number } | null {
  let head: string;
  try {
    head = atob(base64.slice(0, 44));
  } catch {
    return null;
  }
  if (head.length < 24) return null;
  const byte = (i: number) => head.charCodeAt(i);
  if (PNG_SIGNATURE.some((b, i) => byte(i) !== b)) return null;
  if (head.slice(12, 16) !== "IHDR") return null;
  const u32 = (i: number) =>
    ((byte(i) << 24) | (byte(i + 1) << 16) | (byte(i + 2) << 8) | byte(i + 3)) >>> 0;
  return { width: u32(16), height: u32(20) };
}

function image(value: unknown, room: number): string | null {
  if (typeof value !== "string" || value.length > room) return null;
  if (!value.startsWith(PNG_PREFIX)) return null;
  const data = value.slice(PNG_PREFIX.length);
  if (!BASE64.test(data)) return null;
  const size = pngSize(data);
  if (!size) return null;
  const { width, height } = size;
  const max = LIMITS.imagePixels;
  return width >= 1 && height >= 1 && width <= max && height <= max ? value : null;
}

function cell(value: unknown): string {
  return typeof value === "string" ? capped(value, LIMITS.cell) : "";
}

/** A table within `cellsLeft` cells (rows cut to fit), or null. */
function table(value: unknown, cellsLeft: number): PythonTable | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as { columns?: unknown; rows?: unknown };
  const columns = asArray(raw.columns).slice(0, LIMITS.columns).map(cell);
  if (columns.length === 0 || columns.length > cellsLeft) return null;
  const fit = Math.min(LIMITS.rows, Math.floor(cellsLeft / columns.length) - 1);
  const rows = asArray(raw.rows)
    .slice(0, Math.max(0, fit))
    .map((row) => {
      const cells = asArray(row).slice(0, columns.length).map(cell);
      while (cells.length < columns.length) cells.push("");
      return cells;
    });
  return { columns, rows };
}

function tableChars(t: PythonTable): number {
  let n = 0;
  for (const row of [t.columns, ...t.rows]) for (const c of row) n += c.length;
  return n;
}

/** A run's output as the frame posted it, made safe to show: each part
 *  capped, and all of it within LIMITS.total characters (text first, then
 *  tables, then figures -- what does not fit is left out). */
export function readOutput(raw: unknown, timedOut = false): PythonResult {
  const out = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  let room = LIMITS.total;
  const text = (value: unknown, max: number) => {
    const t = capped(value, Math.min(max, room));
    room -= t.length;
    return t;
  };
  const stdout = text(out.stdout, LIMITS.text);
  const stderr = text(out.stderr, LIMITS.text);
  const result = typeof out.result === "string" ? text(out.result, LIMITS.result) : null;
  const tables: PythonTable[] = [];
  let cellsLeft = LIMITS.tableCells;
  for (const value of asArray(out.tables).slice(0, LIMITS.tables)) {
    const t = table(value, cellsLeft);
    if (!t) continue;
    const chars = tableChars(t);
    if (chars > room) break;
    room -= chars;
    cellsLeft -= (t.rows.length + 1) * t.columns.length;
    tables.push(t);
  }
  const images: string[] = [];
  for (const value of asArray(out.images).slice(0, LIMITS.images)) {
    const src = image(value, room);
    if (!src) continue;
    room -= src.length;
    images.push(src);
  }
  return { stdout, stderr, result, images, tables, timedOut };
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
