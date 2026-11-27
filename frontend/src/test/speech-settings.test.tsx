// Settings for voice in the chat (spec 2026-11-27 §5.4, §9): the tenant's two
// speech models and daily allowances (GET/PUT /settings/speech), and the
// capability a model config declares in the models dialog.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelDTO, ModelProviderDTO } from "@/lib/hooks";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
const may = vi.hoisted(() => ({ manage: true }));
const modelsMock = vi.hoisted(() => vi.fn());
const providersMock = vi.hoisted(() => vi.fn());
const createModel = vi.hoisted(() => vi.fn());
const updateModel = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api,
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useModels: () => modelsMock(),
  useModelProviders: () => providersMock(),
  useCredentials: () => ({ data: [] }),
  useReasoningSupport: () => ({ data: undefined }),
  useCreateModel: () => ({ mutateAsync: createModel, isPending: false }),
  useUpdateModel: () => ({ mutateAsync: updateModel, isPending: false }),
}));
vi.mock("@/lib/governance-hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/governance-hooks")>()),
  useMay: () => () => may.manage,
}));
vi.mock("@/lib/model-prices-hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/model-prices-hooks")>()),
  useModelPrices: () => ({ data: [] }),
  useCreateModelPrice: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeactivateModelPrice: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { SpeechSetting } from "@/components/settings/speech-setting";
import { ModelsPage } from "@/routes/models";

function model(overrides: Partial<ModelDTO>): ModelDTO {
  return {
    id: "m",
    provider: "openai",
    name: "model",
    status: "healthy",
    costTier: "$",
    latency: "-",
    assignedTo: [],
    note: "",
    model: "gpt",
    locality: "cloud",
    displayName: null,
    usedByCopilot: false,
    credentialId: null,
    healthError: null,
    healthCheckedAt: null,
    ...overrides,
  };
}

const MODELS = [
  model({ id: "chat", model: "gpt-5", capability: "chat" }),
  model({ id: "stt", model: "whisper-1", capability: "speech_to_text", chatEnabled: false }),
  model({ id: "tts", model: "tts-1", capability: "text_to_speech", chatEnabled: false }),
];

function wrap(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  may.manage = true;
  modelsMock.mockReset().mockReturnValue({ data: MODELS });
  providersMock.mockReset().mockReturnValue({
    data: [
      { canonical: "openai", locality: "cloud", available: true },
      { canonical: "anthropic", locality: "cloud", available: true },
    ] satisfies ModelProviderDTO[],
  });
  createModel.mockReset().mockResolvedValue({});
  updateModel.mockReset().mockResolvedValue({});
  api.get.mockReset().mockImplementation(async (path: string) => {
    if (path === "/settings/speech")
      return {
        speechToTextModelId: "stt",
        textToSpeechModelId: null,
        dailyTranscriptionMinutes: 60,
        dailyReadAloudCharacters: 200000,
      };
    throw new Error(`unexpected GET ${path}`);
  });
  api.put.mockReset().mockImplementation(async (_path: string, body: unknown) => body);
});

describe("SpeechSetting", () => {
  it("offers only speech models of each kind and saves all four fields", async () => {
    wrap(<SpeechSetting />);
    const dictation = (await screen.findByRole("combobox", {
      name: "Dictation model",
    })) as HTMLSelectElement;
    await waitFor(() => expect(dictation.value).toBe("stt"));
    expect(
      within(dictation)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["None", "whisper-1 · openai"]);
    const reading = screen.getByRole("combobox", { name: "Read-aloud model" });
    expect(
      within(reading)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["None", "tts-1 · openai"]);
    expect(
      screen.getByText(
        "Per member per day. 0 switches it off. Dictation is counted by upload size, so silence-heavy recordings can exceed it.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Speech usage shows as 0 in Costs for now.")).toBeInTheDocument();

    fireEvent.change(reading, { target: { value: "tts" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Dictation minutes per day" }), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith("/settings/speech", {
        speechToTextModelId: "stt",
        textToSpeechModelId: "tts",
        dailyTranscriptionMinutes: 0,
        dailyReadAloudCharacters: 200000,
      }),
    );
  });

  it("is read-only without settings:manage", async () => {
    may.manage = false;
    wrap(<SpeechSetting />);
    const dictation = await screen.findByRole("combobox", { name: "Dictation model" });
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(dictation).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});

describe("model capability", () => {
  function openCreate() {
    wrap(<ModelsPage />);
    fireEvent.click(screen.getByRole("button", { name: /new model/i }));
  }

  it("offers the capability for an OpenAI model and sends it", async () => {
    openCreate();
    const capability = screen.getByRole("combobox", { name: "Capability" }) as HTMLSelectElement;
    expect(
      within(capability)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Chat", "Speech to text", "Text to speech"]);
    fireEvent.change(capability, { target: { value: "speech_to_text" } });
    // A speech model is never the Copilot's model.
    expect(screen.queryByText("Use this model for the Copilot")).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/llama3\.1:8b/i), {
      target: { value: "whisper-1" },
    });
    const dialog = screen
      .getByText(/registers a model config/i)
      .closest(".max-w-lg") as HTMLElement;
    fireEvent.click(within(dialog).getByRole("button", { name: /^add model$/i }));
    await waitFor(() => expect(createModel).toHaveBeenCalled());
    expect(createModel.mock.calls[0][0]).toMatchObject({
      provider: "openai",
      model: "whisper-1",
      capability: "speech_to_text",
      usedByCopilot: false,
    });
  });

  it("offers no capability for a provider without OpenAI's audio endpoints", () => {
    openCreate();
    fireEvent.change(screen.getByRole("combobox", { name: "Provider" }), {
      target: { value: "anthropic" },
    });
    expect(screen.queryByRole("combobox", { name: "Capability" })).not.toBeInTheDocument();
  });

  it("shows a speech model's capability instead of the chat switch", () => {
    wrap(<ModelsPage />);
    expect(screen.getAllByRole("switch", { name: "Available in chat" })).toHaveLength(1);
    expect(screen.getByText("Speech to text")).toBeInTheDocument();
    expect(screen.getByText("Text to speech")).toBeInTheDocument();
  });
});
