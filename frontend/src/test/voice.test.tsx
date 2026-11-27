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
    this.ondataavailable?.({ data: new Blob(["voice"], { type: this.options.mimeType }) });
    this.onstop?.();
  });
}

class FakeAudio {
  static instances: FakeAudio[] = [];
  paused = true;
  onended: (() => void) | null = null;
  constructor(public src: string) {
    FakeAudio.instances.push(this);
  }
  play = vi.fn(async () => {
    this.paused = false;
  });
  pause = vi.fn(() => {
    this.paused = true;
  });
  removeAttribute = vi.fn();
  load = vi.fn();
}

const track = { stop: vi.fn() };
const getUserMedia = vi.fn();

beforeEach(() => {
  FakeRecorder.instances = [];
  FakeAudio.instances = [];
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

async function micButton() {
  return screen.findByRole("button", { name: "Dictate a message" });
}

describe("dictation", () => {
  it("shows no mic when speech to text is not configured", async () => {
    audioApi.capabilities = { speechToText: false, textToSpeech: false, textToSpeechLocal: false };
    render(<ComposerHarness />);
    // Let the capabilities query settle before asserting the absence.
    await act(async () => {});
    await waitFor(() => expect(screen.getByRole("textbox", { name: /Message/ })).toBeVisible());
    expect(screen.queryByRole("button", { name: "Dictate a message" })).not.toBeInTheDocument();
  });

  it("records at 16 kbit/s, transcribes and inserts the text at the caret without sending", async () => {
    const onSend = vi.fn();
    render(<ComposerHarness initial="Hello world" onSend={onSend} />);
    const box = screen.getByRole("textbox", { name: /Message/ }) as HTMLTextAreaElement;
    box.setSelectionRange(5, 5);
    const mic = await micButton();
    expect(mic).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(mic);
    await waitFor(() => expect(FakeRecorder.instances).toHaveLength(1));
    const recorder = FakeRecorder.instances[0];
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(recorder.options).toEqual({
      mimeType: "audio/webm;codecs=opus",
      audioBitsPerSecond: 16000,
    });
    const stop = screen.getByRole("button", { name: "Stop dictation" });
    expect(stop).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status")).toHaveTextContent("Recording");
    expect(screen.getByText("0:00")).toBeInTheDocument();

    fireEvent.click(stop);
    await waitFor(() => expect(box.value).toBe("Hello dictated world"));
    expect(audioApi.transcribe).toHaveBeenCalledTimes(1);
    const [sessionId, blob] = audioApi.transcribe.mock.calls[0];
    expect(sessionId).toBe("s1");
    expect((blob as Blob).type).toBe("audio/webm;codecs=opus");
    expect(track.stop).toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("falls back to ogg, then mp4, when webm is not supported", async () => {
    FakeRecorder.isTypeSupported.mockImplementation((type: string) => type === "audio/mp4");
    render(<ComposerHarness />);
    fireEvent.click(await micButton());
    await waitFor(() => expect(FakeRecorder.instances).toHaveLength(1));
    expect(FakeRecorder.instances[0].options.mimeType).toBe("audio/mp4");
    FakeRecorder.isTypeSupported.mockImplementation(
      (type: string) => type === "audio/webm;codecs=opus",
    );
  });

  it("creates the chat first in a new chat, as the first send does", async () => {
    const ensureSession = vi.fn(async () => "fresh");
    render(<ComposerHarness sessionId={null} ensureSession={ensureSession} />);
    fireEvent.click(await micButton());
    await waitFor(() => expect(FakeRecorder.instances).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Stop dictation" }));
    await waitFor(() => expect(audioApi.transcribe).toHaveBeenCalled());
    expect(ensureSession).toHaveBeenCalledTimes(1);
    expect(audioApi.transcribe.mock.calls[0][0]).toBe("fresh");
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
    fireEvent.click(await micButton());
    await waitFor(() => expect(FakeRecorder.instances).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Stop dictation" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("today's dictation allowance is used up"),
    );
  });

  it("stops at 10 minutes, with a countdown from 9:30", async () => {
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

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9 * 60_000 + 30_000 - 65_000 + 1_000);
    });
    // 9:31 elapsed: the countdown shows what is left.
    expect(screen.getByText(/0:29 left/)).toBeInTheDocument();
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

describe("read aloud", () => {
  it("shows no speaker when text to speech is not configured", async () => {
    audioApi.capabilities = { speechToText: false, textToSpeech: false, textToSpeechLocal: false };
    wrap(<Turn />);
    await screen.findByRole("button", { name: "Copy answer" });
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Read aloud" })).not.toBeInTheDocument();
  });

  it("plays the answer's audio and toggles to stop", async () => {
    wrap(<Turn />);
    fireEvent.click(await screen.findByRole("button", { name: "Read aloud" }));
    const stop = await screen.findByRole("button", { name: "Stop reading" });
    expect(stop).toHaveAttribute("aria-pressed", "true");
    expect(audioApi.speech).toHaveBeenCalledWith("s1", "a1", expect.any(AbortSignal));
    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].src).toBe("blob:speech");
    expect(FakeAudio.instances[0].play).toHaveBeenCalled();

    fireEvent.click(stop);
    expect(FakeAudio.instances[0].pause).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:speech");
    expect(await screen.findByRole("button", { name: "Read aloud" })).toBeInTheDocument();
  });

  it("goes back to the speaker when the reading ends", async () => {
    wrap(<Turn />);
    fireEvent.click(await screen.findByRole("button", { name: "Read aloud" }));
    await screen.findByRole("button", { name: "Stop reading" });
    act(() => FakeAudio.instances[0].onended?.());
    expect(await screen.findByRole("button", { name: "Read aloud" })).toBeInTheDocument();
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
    await screen.findByRole("button", { name: "Stop reading" });
    fireEvent.click(second);
    await waitFor(() => expect(FakeAudio.instances).toHaveLength(2));
    expect(FakeAudio.instances[0].pause).toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Stop reading" })).toHaveLength(1),
    );
  });

  it("stops when the answer leaves the page (chat switch)", async () => {
    const view = wrap(<Turn />);
    fireEvent.click(await screen.findByRole("button", { name: "Read aloud" }));
    await screen.findByRole("button", { name: "Stop reading" });
    view.unmount();
    expect(FakeAudio.instances[0].pause).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:speech");
  });

  it("is disabled with a reason on a restricted answer when the speech model is not local", async () => {
    wrap(<Turn message={{ ...answer, restricted: true }} />);
    const speaker = await screen.findByRole("button", { name: "Read aloud" });
    expect(speaker).toBeDisabled();
    expect(speaker).toHaveAccessibleDescription(
      "Read aloud is not available for restricted answers with this speech model.",
    );
  });

  it("treats an answer without a restriction flag as restricted", async () => {
    const { restricted: _drop, ...unknown } = answer;
    void _drop;
    wrap(<Turn message={unknown as ChatMessageDTO} />);
    expect(await screen.findByRole("button", { name: "Read aloud" })).toBeDisabled();
  });

  it("reads a restricted answer with a local speech model", async () => {
    audioApi.capabilities = { speechToText: false, textToSpeech: true, textToSpeechLocal: true };
    wrap(<Turn message={{ ...answer, restricted: true }} />);
    const speaker = await screen.findByRole("button", { name: "Read aloud" });
    await waitFor(() => expect(speaker).toBeEnabled());
  });

  it("shows the server's reason for a refused reading", async () => {
    audioApi.speech.mockRejectedValue(
      new ApiError("this answer is too long to read aloud (at most 20000 characters)", 409),
    );
    wrap(<Turn />);
    fireEvent.click(await screen.findByRole("button", { name: "Read aloud" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "this answer is too long to read aloud (at most 20000 characters)",
      ),
    );
    expect(await screen.findByRole("button", { name: "Read aloud" })).toBeEnabled();
  });

  it("says a generic sentence for a provider failure", async () => {
    audioApi.speech.mockRejectedValue(new ApiError("ReadTimeout", 502));
    wrap(<Turn />);
    fireEvent.click(await screen.findByRole("button", { name: "Read aloud" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("The answer could not be read aloud. Try again."),
    );
  });
});
