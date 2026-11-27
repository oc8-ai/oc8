// Settings: voice in the chat (spec 2026-11-27 §5.4, §9). One model for
// dictation and one for reading answers aloud, each optional (the chat hides
// the button without one), and the per-member daily allowance for each.
import { useState } from "react";
import { AudioLines, Loader2, Save } from "lucide-react";
import { toast } from "sonner";

import { Panel } from "@/components/app-shell";
import { ApiError } from "@/lib/api";
import { useMay } from "@/lib/governance-hooks";
import { useModels, type ModelCapability, type ModelDTO } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import {
  useSpeechSettings,
  useUpdateSpeechSettings,
  type SpeechSettingsDTO,
} from "@/lib/speech-settings";

interface Form {
  speechToTextModelId: string;
  textToSpeechModelId: string;
  dailyTranscriptionMinutes: string;
  dailyReadAloudCharacters: string;
}

function formOf(data: SpeechSettingsDTO): Form {
  return {
    speechToTextModelId: data.speechToTextModelId ?? "",
    textToSpeechModelId: data.textToSpeechModelId ?? "",
    dailyTranscriptionMinutes: String(data.dailyTranscriptionMinutes),
    dailyReadAloudCharacters: String(data.dailyReadAloudCharacters),
  };
}

function sameForm(a: Form, b: Form): boolean {
  return (
    a.speechToTextModelId === b.speechToTextModelId &&
    a.textToSpeechModelId === b.textToSpeechModelId &&
    a.dailyTranscriptionMinutes.trim() === b.dailyTranscriptionMinutes &&
    a.dailyReadAloudCharacters.trim() === b.dailyReadAloudCharacters
  );
}

/** A whole number from 0 to `max`, or null. */
function limitOf(raw: string, max: number): number | null {
  const value = Number(raw.trim());
  return raw.trim() !== "" && Number.isInteger(value) && value >= 0 && value <= max ? value : null;
}

const FIELD =
  "mt-1 w-full rounded-md border border-border bg-background/30 px-3 py-2 text-sm text-foreground disabled:opacity-60";

function modelsFor(models: ModelDTO[], capability: ModelCapability): ModelDTO[] {
  return models.filter((m) => m.capability === capability);
}

export function SpeechSetting() {
  const t = useT();
  const may = useMay();
  const canManage = may("settings:manage");
  const { data } = useSpeechSettings();
  const { data: models = [] } = useModels();
  const update = useUpdateSpeechSettings();
  const [form, setForm] = useState<Form>({
    speechToTextModelId: "",
    textToSpeechModelId: "",
    dailyTranscriptionMinutes: "",
    dailyReadAloudCharacters: "",
  });
  // The form starts from what the server says, and follows it while nobody
  // is editing: a refetch never overwrites a change in progress.
  const [seen, setSeen] = useState<SpeechSettingsDTO | undefined>(undefined);
  const dirty = !!seen && !sameForm(form, formOf(seen));
  if (data && data !== seen) {
    setSeen(data);
    if (!dirty) setForm(formOf(data));
  }
  const set = (patch: Partial<Form>) => setForm((prev) => ({ ...prev, ...patch }));
  const minutes = limitOf(form.dailyTranscriptionMinutes, 24 * 60);
  const characters = limitOf(form.dailyReadAloudCharacters, 10_000_000);
  const valid = minutes !== null && characters !== null;

  async function save() {
    if (minutes === null || characters === null) return;
    try {
      const saved = await update.mutateAsync({
        speechToTextModelId: form.speechToTextModelId || null,
        textToSpeechModelId: form.textToSpeechModelId || null,
        dailyTranscriptionMinutes: minutes,
        dailyReadAloudCharacters: characters,
      });
      setSeen(saved);
      setForm(formOf(saved));
      toast.success(t("Speech saved", "Sprache gespeichert"));
    } catch (error) {
      toast.error(
        error instanceof ApiError && error.status === 422
          ? error.message
          : t("Could not save speech.", "Sprache konnte nicht gespeichert werden."),
      );
    }
  }

  const disabled = !canManage || !data;
  const picker = (
    label: string,
    value: string,
    capability: ModelCapability,
    onChange: (id: string) => void,
  ) => (
    <label className="block text-xs text-muted-foreground">
      {label}
      <select
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className={FIELD}
      >
        <option value="">{t("None", "Keines")}</option>
        {/* A pick this list does not show (models not visible to this role). */}
        {value && !models.some((m) => m.id === value) && (
          <option value={value}>{t("Current model", "Aktuelles Modell")}</option>
        )}
        {modelsFor(models, capability).map((m) => (
          <option key={m.id} value={m.id}>
            {m.displayName || m.model} · {m.provider}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <Panel id="speech" className="p-5">
      <header className="flex items-start gap-3">
        <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
          <AudioLines className="h-4 w-4" aria-hidden />
        </div>
        <div>
          <h3 className="font-serif text-lg">{t("Speech", "Sprache")}</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {t(
              "With a model, the chat offers dictation and reading answers aloud. Add speech models under Models.",
              "Mit einem Modell bietet der Chat Diktat und Vorlesen von Antworten an. Sprachmodelle legst du unter Modelle an.",
            )}
          </p>
        </div>
      </header>
      <div className="mt-4 space-y-3">
        {picker(
          t("Dictation model", "Diktiermodell"),
          form.speechToTextModelId,
          "speech_to_text",
          (id) => set({ speechToTextModelId: id }),
        )}
        {picker(
          t("Read-aloud model", "Vorlesemodell"),
          form.textToSpeechModelId,
          "text_to_speech",
          (id) => set({ textToSpeechModelId: id }),
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-xs text-muted-foreground">
            {t("Dictation minutes per day", "Diktatminuten pro Tag")}
            <input
              type="number"
              min={0}
              max={24 * 60}
              step={1}
              value={form.dailyTranscriptionMinutes}
              disabled={disabled}
              onChange={(event) => set({ dailyTranscriptionMinutes: event.target.value })}
              className={FIELD}
            />
          </label>
          <label className="block text-xs text-muted-foreground">
            {t("Read-aloud characters per day", "Vorlesezeichen pro Tag")}
            <input
              type="number"
              min={0}
              max={10_000_000}
              step={1}
              value={form.dailyReadAloudCharacters}
              disabled={disabled}
              onChange={(event) => set({ dailyReadAloudCharacters: event.target.value })}
              className={FIELD}
            />
          </label>
        </div>
        <p className="text-xs text-muted-foreground">
          {t(
            "Per member per day. 0 switches it off. Dictation is counted by upload size, so silence-heavy recordings can exceed it.",
            "Pro Mitglied und Tag. 0 schaltet es aus. Diktat wird nach Upload-Größe gezählt, sehr stille Aufnahmen können darüber liegen.",
          )}
        </p>
        <p className="text-xs text-muted-foreground">
          {t(
            "Speech usage shows as 0 in Costs for now.",
            "Sprachnutzung erscheint vorerst mit 0 in den Kosten.",
          )}
        </p>
        <button
          type="button"
          onClick={() => void save()}
          disabled={disabled || !dirty || !valid || update.isPending}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-40"
        >
          {update.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
          ) : (
            <Save className="h-3 w-3" aria-hidden />
          )}
          {t("Save", "Speichern")}
        </button>
      </div>
    </Panel>
  );
}
