// The two speech requests (lib/api.ts) as the server reads them: the
// multipart field names, the bearer header (never the token in the URL) and
// encoded ids in the read-aloud path.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchAnswerSpeech, setCommunityToken, transcribeChatAudio } from "@/lib/api";

const TOKEN = "tok-123";
const fetchMock = vi.fn();

beforeEach(() => {
  setCommunityToken(TOKEN);
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

function call() {
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  return { url, init, headers: init.headers as Record<string, string> };
}

describe("transcribeChatAudio", () => {
  it("posts the recording as `file` and the chat as `session_id`, with the bearer token", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ text: "hi" }), { status: 200 }));
    const recording = new Blob(["voice"], { type: "audio/webm;codecs=opus" });

    await expect(transcribeChatAudio("s 1", recording)).resolves.toEqual({ text: "hi" });

    const { url, init, headers } = call();
    expect(url).toMatch(/\/chat\/audio\/transcribe$/);
    expect(url).not.toContain(TOKEN);
    expect(init.method).toBe("POST");
    expect(headers.authorization).toBe(`Bearer ${TOKEN}`);
    const form = init.body as FormData;
    expect(form.get("session_id")).toBe("s 1");
    const file = form.get("file") as File;
    expect(file.name).toBe("dictation.webm");
    expect(file.type).toBe("audio/webm;codecs=opus");
  });

  it("names an mp4 recording .m4a and an ogg one .ogg", async () => {
    fetchMock.mockImplementation(async () => new Response('{"text":""}', { status: 200 }));
    await transcribeChatAudio("s", new Blob(["v"], { type: "audio/mp4" }));
    await transcribeChatAudio("s", new Blob(["v"], { type: "audio/ogg;codecs=opus" }));
    const names = fetchMock.mock.calls.map(
      ([, init]) => ((init as RequestInit).body as FormData).get("file") as File,
    );
    expect(names.map((f) => f.name)).toEqual(["dictation.m4a", "dictation.ogg"]);
  });

  it("rejects with the server's detail and status", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "allowance used up" }), { status: 429 }),
    );
    await expect(transcribeChatAudio("s", new Blob(["v"]))).rejects.toMatchObject({
      status: 429,
      message: "allowance used up",
    });
  });
});

describe("fetchAnswerSpeech", () => {
  it("posts to the message's speech path with encoded ids and the bearer token", async () => {
    fetchMock.mockResolvedValue(
      new Response("mp3", { status: 200, headers: { "content-type": "audio/mpeg" } }),
    );
    const signal = new AbortController().signal;

    const blob = await fetchAnswerSpeech("s/1", "m?2", signal);

    expect(blob.size).toBe(3);
    const { url, init, headers } = call();
    expect(url).toMatch(/\/chat\/sessions\/s%2F1\/messages\/m%3F2\/speech$/);
    expect(url).not.toContain(TOKEN);
    expect(init.method).toBe("POST");
    expect(init.signal).toBe(signal);
    expect(headers.authorization).toBe(`Bearer ${TOKEN}`);
  });
});
