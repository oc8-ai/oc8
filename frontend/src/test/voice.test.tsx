// Voice in the chat (spec 2026-11-27 §5.4, §9): dictation in the composer
// and reading an answer aloud. MediaRecorder, getUserMedia and Audio are
// stubbed; the two speech routes are stubbed at `@/lib/api`.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const audioApi = vi.hoisted(() => ({
  capabilities: { speechToText: true, textToSpeech: true, textToSpeechLocal: false },
  transcribe: vi.fn(),
  speech: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: vi.fn(async (path: string) => {
        if (path === "/chat/audio/capabilities") return audioApi.capabilities;
        throw new Error(`unexpected GET ${path}`);
      }),
    },
    transcribeChatAudio: audioApi.transcribe,
    fetchAnswerSpeech: audioApi.speech,
  };
});
vi.mock("sonner", () => ({ toast }));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({ data: [] }),
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useContextSearch: () => [],
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { ApiError } from "@/lib/api";
import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { Composer, type ComposerProps } from "@/components/conversation/composer";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

// ---- browser stubs ---------------------------------------------------------

class FakeRecorder {
  static instances: FakeRecorder[] = [];
  static isTypeSupported = vi.fn((type: string) => type === "audio/webm;codecs=opus");
  /** What the next recording holds. */
  static nextData: BlobPart = "voice";
  state: "inactive" | "recording" = "inactive";
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor(
    public stream: MediaStream,
    public options: MediaRecorderOptions,
  ) {
    FakeRecorder.instances.push(this);
  }
  start = vi.fn(() => {
    this.state = "recording";
  });
  stop = vi.fn(() => {
    if (this.state === "inactive") return;
    this.state = "inactive";
    this.ondataavailable?.({
      data: new Blob([FakeRecorder.nextData], { type: this.options.mimeType }),
    });
    this.onstop?.();
  });
}

const SILENT_PREFIX = "data:audio/wav;base64,";

class FakeAudio {
  static instances: FakeAudio[] = [];
  /** Rejects the next play of a fetched answer with NotAllowedError. */
  static refuseNext = false;
  src = "";
  paused = true;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeAudio.instances.push(this);
  }
  play = vi.fn(async () => {
    if (FakeAudio.refuseNext && !this.src.startsWith(SILENT_PREFIX)) {
      FakeAudio.refuseNext = false;
      throw new DOMException("not allowed", "NotAllowedError");
    }
    this.paused = false;
  });
  pause = vi.fn(() => {
    this.paused = true;
  });
}

const track = { stop: vi.fn() };
const getUserMedia = vi.fn();

beforeEach(() => {
  FakeRecorder.instances = [];
  FakeRecorder.nextData = "voice";
  FakeRecorder.isTypeSupported.mockReset();
  FakeRecorder.isTypeSupported.mockImplementation(
    (type: string) => type === "audio/webm;codecs=opus",
  );
  FakeAudio.instances = [];
  FakeAudio.refuseNext = false;
  track.stop.mockReset();
  getUserMedia.mockReset().mockResolvedValue({ getTracks: () => [track] });
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  vi.stubGlobal("Audio", FakeAudio);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  URL.createObjectURL = vi.fn(() => "blob:speech");
  URL.revokeObjectURL = vi.fn();
  audioApi.capabilities = { speechToText: true, textToSpeech: true, textToSpeechLocal: false };
  audioApi.transcribe.mockReset().mockResolvedValue({ text: "dictated" });
  audioApi.speech.mockReset().mockResolvedValue(new Blob(["mp3"], { type: "audio/mpeg" }));
  toast.error.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ---- composer --------------------------------------------------------------

function ComposerHarness(props: Partial<ComposerProps> & { initial?: string }) {
  const { initial = "", ...rest } = props;
  const [draft, setDraft] = useState(initial);
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <Composer
        sessionId="s1"
        ensureSession={async () => "s1"}
        draft={draft}
        onDraftChange={setDraft}
        busy={false}
        onSend={vi.fn()}
        onStop={vi.fn()}
        modelValue={null}
        showModelPicker={false}
        {...rest}
      />
    </QueryClientProvider>
  );
}

/** A new chat whose `ensureSession` makes "fresh" and shows it, as the page does. */
function NewChatHarness({ ensure }: { ensure: () => void }) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  return (
    <ComposerHarness
      sessionId={sessionId}
      ensureSession={async () => {
        ensure();
        setSessionId("fresh");
        return "fresh";
      }}
    />
  );
}

async function micButton() {
  return screen.findByRole("button", { name: "Dictate a message" });
}

async function startRecording() {
  fireEvent.click(await micButton());
  await waitFor(() => expect(FakeRecorder.instances).toHaveLength(1));
  return FakeRecorder.instances[0];
}

function stopRecording() {
  fireEvent.click(screen.getByRole("button", { name: "Dictate a message", pressed: true }));
}

function box() {
  return screen.getByRole("textbox", { name: /Message/ }) as HTMLTextAreaElement;
}

describe("dictation", () => {
  it("shows no mic when speech to text is not configured", async () => {
    audioApi.capabilities = { speechToText: false, textToSpeech: false, textToSpeechLocal: false };
    render(<ComposerHarness />);
    // Let the capabilities query settle before asserting the absence.
    await act(async () => {});
    await waitFor(() => expect(box()).toBeVisible());
    expect(screen.queryByRole("button", { name: "Dictate a message" })).not.toBeInTheDocument();
  });

  it("shows no mic when the browser offers no microphone", async () => {
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
    render(<ComposerHarness />);
    await act(async () => {});
    await waitFor(() => expect(box()).toBeVisible());
    expect(screen.queryByRole("button", { name: "Dictate a message" })).not.toBeInTheDocument();
  });

  it("records at 16 kbit/s, transcribes and inserts the text at the caret without sending", async () => {
    const onSend = vi.fn();
    render(<ComposerHarness initial="Hello world" onSend={onSend} />);
    box().setSelectionRange(5, 5);
    const mic = await micButton();
    expect(mic).toHaveAttribute("aria-pressed", "false");

    const recorder = await startRecording();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(recorder.options).toEqual({
      mimeType: "audio/webm;codecs=opus",
      audioBitsPerSecond: 16000,
    });
    // One stable label; the pressed state says it is recording.
    expect(mic).toHaveAttribute("aria-pressed", "true");
    expect(mic).toHaveAccessibleName("Dictate a message");
    expect(screen.getByRole("status")).toHaveTextContent("Recording");
    expect(screen.getByText("0:00")).toBeInTheDocument();

    stopRecording();
    await waitFor(() => expect(box().value).toBe("Hello dictated world"));
    expect(audioApi.transcribe).toHaveBeenCalledTimes(1);
    const [sessionId, blob] = audioApi.transcribe.mock.calls[0];
    expect(sessionId).toBe("s1");
    expect((blob as Blob).type).toBe("audio/webm;codecs=opus");
    expect(track.stop).toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("prefers ogg over mp4 when webm is not supported", async () => {
    FakeRecorder.isTypeSupported.mockImplementation(
      (type: string) => type === "audio/ogg;codecs=opus" || type === "audio/mp4",
    );
    render(<ComposerHarness />);
    const recorder = await startRecording();
    expect(recorder.options.mimeType).toBe("audio/ogg;codecs=opus");
  });

  it("falls back to mp4 when neither webm nor ogg is supported", async () => {
    FakeRecorder.isTypeSupported.mockImplementation((type: string) => type === "audio/mp4");
    render(<ComposerHarness />);
    const recorder = await startRecording();
    expect(recorder.options.mimeType).toBe("audio/mp4");
  });

  it("creates the chat only for the upload, as the first send does, and keeps the words", async () => {
    const ensure = vi.fn();
    render(<NewChatHarness ensure={ensure} />);
    await startRecording();
    expect(ensure).not.toHaveBeenCalled();
    stopRecording();
    await waitFor(() => expect(box().value).toBe("dictated"));
    expect(ensure).toHaveBeenCalledTimes(1);
    expect(audioApi.transcribe.mock.calls[0][0]).toBe("fresh");
  });

  it("drops a recording when another chat opens while it records (new chat)", async () => {
    const ensureSession = vi.fn(async () => "fresh");
    const view = render(<ComposerHarness sessionId={null} ensureSession={ensureSession} />);
    const recorder = await startRecording();
    view.rerender(<ComposerHarness sessionId="other" ensureSession={ensureSession} />);
    await waitFor(() => expect(recorder.stop).toHaveBeenCalled());
    expect(track.stop).toHaveBeenCalled();
    expect(await micButton()).toHaveAttribute("aria-pressed", "false");
    await act(async () => {});
    expect(ensureSession).not.toHaveBeenCalled();
    expect(audioApi.transcribe).not.toHaveBeenCalled();
    expect(box().value).toBe("");
  });

  it("drops the transcript when the chat switches while it is transcribed", async () => {
    let finish: (value: { text: string }) => void = () => undefined;
    audioApi.transcribe.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const view = render(<ComposerHarness sessionId="s1" />);
    await startRecording();
    stopRecording();
    await waitFor(() => expect(audioApi.transcribe).toHaveBeenCalled());
    view.rerender(<ComposerHarness sessionId="s2" />);
    await act(async () => finish({ text: "dictated" }));
    expect(box().value).toBe("");
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("stops and frees the microphone when the composer goes away", async () => {
    const view = render(<ComposerHarness />);
    const recorder = await startRecording();
    view.unmount();
    expect(recorder.stop).toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
    await act(async () => {});
    expect(audioApi.transcribe).not.toHaveBeenCalled();
  });

  it("refuses a recording over 12 MB before making a chat or uploading", async () => {
    FakeRecorder.nextData = new Uint8Array(12 * 1024 * 1024 + 1);
    const ensureSession = vi.fn(async () => "fresh");
    render(<ComposerHarness sessionId={null} ensureSession={ensureSession} />);
    await startRecording();
    stopRecording();
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "This recording is larger than 12 MB. Try a shorter one.",
      ),
    );
    expect(ensureSession).not.toHaveBeenCalled();
    expect(audioApi.transcribe).not.toHaveBeenCalled();
  });

  it("says so when nothing was recognized, and inserts nothing", async () => {
    audioApi.transcribe.mockResolvedValue({ text: "  " });
    render(<ComposerHarness initial="Hi" />);
    await startRecording();
    stopRecording();
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("No speech was recognized."));
    expect(box().value).toBe("Hi");
  });

  it("says so when the microphone is refused", async () => {
    getUserMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    render(<ComposerHarness />);
    fireEvent.click(await micButton());
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Microphone access was denied."));
    expect(FakeRecorder.instances).toHaveLength(0);
    expect(await micButton()).toHaveAttribute("aria-pressed", "false");
  });

  it("shows the server's reason when the daily allowance is used up", async () => {
    audioApi.transcribe.mockRejectedValue(
      new ApiError("today's dictation allowance is used up", 429),
    );
    render(<ComposerHarness />);
    await startRecording();
    stopRecording();
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("today's dictation allowance is used up"),
    );
  });

  it("stops at 10 minutes, with a countdown from 9:30 that is announced", async () => {
    render(<ComposerHarness />);
    const mic = await micButton();
    vi.useFakeTimers({ shouldAdvanceTime: false });
    fireEvent.click(mic);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(FakeRecorder.instances).toHaveLength(1);
    const recorder = FakeRecorder.instances[0];

    await act(async () => {
      await vi.advanceTimersByTimeAsync(65_000);
    });
    expect(screen.getByText("1:05")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/^Recording$/);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9 * 60_000 + 30_000 - 65_000 + 1_000);
    });
    // 9:31 elapsed: the countdown shows what is left, and says so once.
    expect(screen.getByText(/0:29 left/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Recording. It stops in 30 seconds.");
    expect(recorder.stop).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(recorder.stop).toHaveBeenCalled();
    vi.useRealTimers();
    await waitFor(() => expect(audioApi.transcribe).toHaveBeenCalledTimes(1));
  });
});

// ---- read aloud ------------------------------------------------------------

const answer: ChatMessageDTO = {
  id: "a1",
  sessionId: "s1",
  role: "assistant",
  content: "The answer.",
  runId: null,
  renderedComponents: [],
  createdAt: "2026-11-27T09:00:00Z",
  attachments: [],
  mode: null,
  contextRefs: [],
  authorMemberId: null,
  authorName: null,
  replyToId: "u1",
  restricted: false,
};

function Turn({ message = answer }: { message?: ChatMessageDTO }) {
  return (
    <AssistantTurn
      sessionId="s1"
      turn={{
        kind: "assistant",
        key: message.id,
        replyToId: "u1",
        variants: [message],
        liveRunId: null,
      }}
    />
  );
}

function wrap(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

async function speaker() {
  return screen.findByRole("button", { name: "Read aloud" });
}

/** The element the reading plays on (the first one made). */
function element() {
  return FakeAudio.instances[0];
}

describe("read aloud", () => {
  it("shows no speaker when text to speech is not configured", async () => {
    audioApi.capabilities = { speechToText: false, textToSpeech: false, textToSpeechLocal: false };
    wrap(<Turn />);
    await screen.findByRole("button", { name: "Copy answer" });
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Read aloud" })).not.toBeInTheDocument();
  });

  it("unlocks the element in the tap, then plays the fetched answer on it and toggles", async () => {
    let deliver: (blob: Blob) => void = () => undefined;
    audioApi.speech.mockReturnValue(new Promise<Blob>((resolve) => (deliver = resolve)));
    wrap(<Turn />);
    const button = await speaker();
    fireEvent.click(button);
    // Made and started inside the click, before the audio has arrived.
    expect(FakeAudio.instances).toHaveLength(1);
    expect(element().src.startsWith(SILENT_PREFIX)).toBe(true);
    expect(element().play).toHaveBeenCalledTimes(1);
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(audioApi.speech).toHaveBeenCalledWith("s1", "a1", expect.any(AbortSignal));

    await act(async () => deliver(new Blob(["mp3"], { type: "audio/mpeg" })));
    expect(FakeAudio.instances).toHaveLength(1);
    expect(element().src).toBe("blob:speech");
    expect(element().play).toHaveBeenCalledTimes(2);
    expect(button).toHaveAccessibleName("Read aloud");

    fireEvent.click(button);
    expect(element().pause).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:speech");
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("asks for a second tap when the browser blocks playback, without fetching again", async () => {
    FakeAudio.refuseNext = true;
    wrap(<Turn />);
    const button = await speaker();
    fireEvent.click(button);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Tap again to play."));
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();

    fireEvent.click(button);
    expect(audioApi.speech).toHaveBeenCalledTimes(1);
    const second = FakeAudio.instances[1];
    expect(second.src).toBe("blob:speech");
    expect(second.play).toHaveBeenCalled();
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
  });

  it("goes back to the speaker when the reading ends", async () => {
    wrap(<Turn />);
    const button = await speaker();
    fireEvent.click(button);
    await waitFor(() => expect(element().src).toBe("blob:speech"));
    act(() => element().onended?.());
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:speech");
  });

  it("resets when the audio cannot be played", async () => {
    wrap(<Turn />);
    const button = await speaker();
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    await waitFor(() => expect(element().src).toBe("blob:speech"));
    act(() => element().onerror?.());
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:speech");
  });

  it("plays one answer at a time", async () => {
    wrap(
      <>
        <Turn message={{ ...answer, id: "a1" }} />
        <Turn message={{ ...answer, id: "a2" }} />
      </>,
    );
    const [first, second] = await screen.findAllByRole("button", { name: "Read aloud" });
    fireEvent.click(first);
    await waitFor(() => expect(first).toHaveAttribute("aria-pressed", "true"));
    fireEvent.click(second);
    expect(FakeAudio.instances).toHaveLength(2);
    expect(FakeAudio.instances[0].pause).toHaveBeenCalled();
    expect(first).toHaveAttribute("aria-pressed", "false");
    expect(second).toHaveAttribute("aria-pressed", "true");
  });

  it("stops when the answer leaves the page (chat switch)", async () => {
    const view = wrap(<Turn />);
    fireEvent.click(await speaker());
    await waitFor(() => expect(element().src).toBe("blob:speech"));
    view.unmount();
    expect(element().pause).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:speech");
  });

  it("keeps reading in one view when another view of the same answer closes (dock)", async () => {
    const chat = wrap(<Turn />);
    const dock = wrap(<Turn />);
    const [inChat] = await screen.findAllByRole("button", { name: "Read aloud" });
    fireEvent.click(inChat);
    await waitFor(() => expect(element().src).toBe("blob:speech"));
    dock.unmount();
    expect(element().pause).not.toHaveBeenCalled();
    expect(inChat).toHaveAttribute("aria-pressed", "true");
    chat.unmount();
  });

  it("is disabled with a reason on a restricted answer when the speech model is not local", async () => {
    wrap(<Turn message={{ ...answer, restricted: true }} />);
    const button = await speaker();
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(
      "Read aloud is not available for restricted answers with this speech model.",
    );
  });

  it("treats an answer without a restriction flag as restricted", async () => {
    const { restricted: _drop, ...unknown } = answer;
    void _drop;
    wrap(<Turn message={unknown as ChatMessageDTO} />);
    expect(await speaker()).toBeDisabled();
  });

  it("reads a restricted answer with a local speech model", async () => {
    audioApi.capabilities = { speechToText: false, textToSpeech: true, textToSpeechLocal: true };
    wrap(<Turn message={{ ...answer, restricted: true }} />);
    const button = await speaker();
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("shows the server's reason for a refused reading", async () => {
    audioApi.speech.mockRejectedValue(
      new ApiError("this answer is too long to read aloud (at most 20000 characters)", 409),
    );
    wrap(<Turn />);
    const button = await speaker();
    fireEvent.click(button);
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "this answer is too long to read aloud (at most 20000 characters)",
      ),
    );
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("says a generic sentence for a provider failure", async () => {
    audioApi.speech.mockRejectedValue(new ApiError("ReadTimeout", 502));
    wrap(<Turn />);
    fireEvent.click(await speaker());
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("The answer could not be read aloud. Try again."),
    );
  });
});
