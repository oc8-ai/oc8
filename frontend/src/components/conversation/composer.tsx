// frontend/src/components/conversation/composer.tsx
// The only composer (spec §2.1). Owns typing, sigils, attachments and keys;
// sending, stopping and session creation belong to the caller.
//
// Slots, so later tasks extend it instead of forking it:
//   above          C2 QuestionCard, C3 PinnedBar
//   target         C4 "To the running run …"
//   extraPlusItems C3 data source, knowledge base, oc8 object
//   trailing       C5 dictation (Enterprise; core ships no mic)
import {
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { ArrowUp, BookOpen, Paperclip, Square, Upload, X } from "lucide-react";

import {
  SigilPopover,
  activeSigil,
  cycleIndex,
  replaceSigil,
  type SigilItem,
  type SigilToken,
} from "@/components/composer-sigils";
import { MAX_ATTACHMENT_BYTES, uploadChatAttachment, type FileAttachmentDTO } from "@/lib/api";
import {
  chatCommands,
  commandSummary,
  detectCommand,
  matchCommands,
  useChatModes,
} from "@/lib/chat-commands";
import { useContextSearch, type ContextItem } from "@/lib/conversation-api";
import { useCan } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { ModelPicker } from "./model-picker";
import { PlusMenu, type PlusMenuItem } from "./plus-menu";

export type { PlusMenuItem } from "./plus-menu";

export interface ComposerSubmit {
  text: string;
  attachmentIds: string[];
  contextRefs: ContextItem[];
}

export interface ComposerProps {
  sessionId: string | null;
  ensureSession: () => Promise<string>;
  draft: string;
  onDraftChange: (text: string) => void;
  /** A run is answering this chat: Send becomes Stop and Esc stops. */
  busy: boolean;
  onSend: (submit: ComposerSubmit) => void;
  onStop: () => void;
  /** ↑ in an empty composer. */
  onEditLast?: () => void;
  onLocalCommand?: (key: string) => void;
  placeholder?: string;
  modelValue: string | null;
  showModelPicker: boolean;
  agentItems?: SigilItem[];
  onAddressAgent?: (agentId: string) => void;
  /** C4: "To the running run …" */
  target?: ReactNode;
  /** C2 QuestionCard, C3 PinnedBar. */
  above?: ReactNode;
  /** C3: data source, knowledge base, oc8 object. */
  extraPlusItems?: PlusMenuItem[];
  /** C5: dictation button. */
  trailing?: ReactNode;
  /** Read-only share. */
  disabled?: boolean;
  sendLabel?: string;
  /** C3: lets the + menu and # add a ref from outside (data source browser). */
  contextRefs?: ContextItem[];
  onContextRefsChange?: (refs: ContextItem[]) => void;
}

const MAX_REFS = 10;

const ICON_BUTTON =
  "grid size-9 place-items-center rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

export function Composer(props: ComposerProps) {
  const t = useT();
  const can = useCan();
  const { data: modes } = useChatModes();
  const commands = chatCommands(modes, can);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [caret, setCaret] = useState(0);
  const [pickerIndex, setPickerIndex] = useState(0);
  const [ownRefs, setOwnRefs] = useState<ContextItem[]>([]);
  const refs = props.contextRefs ?? ownRefs;
  const setRefs = props.onContextRefsChange ?? setOwnRefs;
  const [attachments, setAttachments] = useState<FileAttachmentDTO[]>([]);
  const [uploading, setUploading] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sigil: SigilToken | null = props.disabled ? null : activeSigil(props.draft, caret);
  const contextItems = useContextSearch(sigil?.kind === "#" ? sigil.query : null);

  const sigilItems: SigilItem[] =
    sigil?.kind === "/"
      ? matchCommands(sigil.query, commands).map((c) => ({
          id: c.key,
          label: `/${c.key}`,
          hint: commandSummary(c.key, c.summary, t),
        }))
      : sigil?.kind === "@"
        ? (props.agentItems ?? [])
            .filter((a) => a.label.toLowerCase().includes(sigil.query.toLowerCase()))
            .slice(0, 8)
        : sigil?.kind === "#"
          ? contextItems
              .filter((c) => !refs.some((r) => r.kind === c.kind && r.id === c.id))
              .map((c) => ({ id: `${c.kind}:${c.id}`, label: c.label, hint: c.hint }))
          : [];

  function pick(item: SigilItem | undefined) {
    if (!sigil || !item || item.blocked) return;
    if (sigil.kind === "/") {
      const next = replaceSigil(props.draft, sigil, `/${item.id} `);
      props.onDraftChange(next);
      setCaret(item.id.length + 2);
    } else if (sigil.kind === "@") {
      props.onAddressAgent?.(item.id);
      props.onDraftChange(replaceSigil(props.draft, sigil, ""));
      setCaret(sigil.start);
    } else {
      const [kind, ...rest] = item.id.split(":");
      if (refs.length < MAX_REFS) {
        setRefs([...refs, { kind, id: rest.join(":"), label: item.label, hint: item.hint }]);
      } else {
        setError(t("You can attach up to 10 items.", "Du kannst bis zu 10 Einträge anhängen."));
      }
      props.onDraftChange(replaceSigil(props.draft, sigil, ""));
      setCaret(sigil.start);
    }
    setPickerIndex(0);
    textarea.current?.focus();
  }

  async function addFiles(files: File[]) {
    setError(null);
    for (const file of files) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        setError(
          t(
            `${file.name} is larger than 25 MB. Pick a smaller file.`,
            `${file.name} ist größer als 25 MB. Wähl eine kleinere Datei.`,
          ),
        );
        continue;
      }
      setUploading((n) => n + 1);
      try {
        const sessionId = props.sessionId ?? (await props.ensureSession());
        const uploaded = await uploadChatAttachment(sessionId, file);
        setAttachments((prev) => [...prev, uploaded]);
      } catch {
        setError(
          t(
            `${file.name} could not be uploaded. Try again.`,
            `${file.name} konnte nicht hochgeladen werden. Versuch es noch einmal.`,
          ),
        );
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }

  function submit() {
    const text = props.draft.trim();
    if (!text || props.disabled || props.busy || uploading > 0) return;
    const local = detectCommand(text, commands);
    if (local?.command.local) {
      props.onLocalCommand?.(local.command.key);
      props.onDraftChange("");
      return;
    }
    props.onSend({ text, attachmentIds: attachments.map((a) => a.id), contextRefs: refs });
    setAttachments([]);
    setRefs([]);
    setError(null);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing) return;
    if (sigil && sigilItems.length > 0) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setPickerIndex((i) => cycleIndex(i, e.key === "ArrowDown" ? 1 : -1, sigilItems.length));
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        pick(sigilItems[Math.min(pickerIndex, sigilItems.length - 1)]);
        return;
      }
    }
    if (e.key === "Escape" && props.busy) {
      e.preventDefault();
      props.onStop();
      return;
    }
    if (e.key === "ArrowUp" && props.draft === "" && props.onEditLast) {
      e.preventDefault();
      props.onEditLast();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.clipboardData?.files ?? []);
    if (files.length > 0) {
      e.preventDefault();
      void addFiles(files);
    }
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    setDragging(false);
    if (props.disabled) return;
    const files = Array.from(e.dataTransfer?.files ?? []);
    if (files.length > 0) {
      e.preventDefault();
      void addFiles(files);
    }
  }

  const plusItems: PlusMenuItem[] = [
    {
      id: "upload",
      label: t("Upload file", "Datei hochladen"),
      icon: <Upload className="size-4" aria-hidden />,
      onSelect: () => fileInput.current?.click(),
    },
    ...(props.extraPlusItems ?? []),
  ];
  const canSend = !!props.draft.trim() && !props.disabled && uploading === 0;
  const chips = attachments.length > 0 || refs.length > 0 || uploading > 0;

  return (
    <div
      className="space-y-2"
      onDragOver={(e) => {
        if (props.disabled) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      {props.above}
      {sigil && (
        <SigilPopover
          title={
            sigil.kind === "/"
              ? t("Commands", "Befehle")
              : sigil.kind === "@"
                ? t("Agents", "Agenten")
                : t("Attach context", "Kontext anhängen")
          }
          items={sigilItems}
          activeIndex={pickerIndex}
          onPick={pick}
          onHoverIndex={setPickerIndex}
          emptyText={t("Nothing matches that.", "Dazu passt nichts.")}
        />
      )}
      <div
        className={cn(
          "rounded-lg border border-border bg-card p-2 focus-within:ring-2 focus-within:ring-ring",
          dragging && "ring-2 ring-ring",
        )}
      >
        {props.target && (
          <div className="px-1 pb-1 text-xs text-muted-foreground">{props.target}</div>
        )}
        {chips && (
          <ul className="flex flex-wrap gap-1 px-1 pb-1" aria-live="polite">
            {attachments.map((a) => (
              <li
                key={a.id}
                className="inline-flex h-9 items-center gap-1 rounded-full border border-border pl-3 text-xs"
              >
                <Paperclip className="size-3.5" aria-hidden />
                {a.filename}
                <button
                  type="button"
                  aria-label={t(`Remove ${a.filename}`, `${a.filename} entfernen`)}
                  onClick={() => setAttachments((p) => p.filter((x) => x.id !== a.id))}
                  className="grid size-9 place-items-center rounded-full text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </li>
            ))}
            {refs.map((r) => (
              <li
                key={`${r.kind}:${r.id}`}
                className="inline-flex h-9 items-center gap-1 rounded-full border border-border pl-3 text-xs text-muted-foreground"
              >
                <BookOpen className="size-3.5" aria-hidden />
                {r.label}
                <button
                  type="button"
                  aria-label={t(`Remove ${r.label}`, `${r.label} entfernen`)}
                  onClick={() => setRefs(refs.filter((x) => !(x.kind === r.kind && x.id === r.id)))}
                  className="grid size-9 place-items-center rounded-full hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </li>
            ))}
            {uploading > 0 && (
              <li className="inline-flex h-9 items-center px-3 text-xs text-muted-foreground">
                {t("Uploading …", "Wird hochgeladen …")}
              </li>
            )}
          </ul>
        )}
        <textarea
          ref={textarea}
          aria-label={t("Message", "Nachricht")}
          value={props.draft}
          disabled={props.disabled}
          rows={1}
          placeholder={props.placeholder ?? t("Ask anything …", "Frag, was du willst …")}
          onChange={(e) => {
            props.onDraftChange(e.target.value);
            setCaret(e.target.selectionStart ?? e.target.value.length);
            setPickerIndex(0);
          }}
          onKeyUp={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onClick={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          className="max-h-48 min-h-10 w-full resize-none bg-transparent px-1 py-1 text-sm outline-none placeholder:text-muted-foreground"
        />
        <div className="flex items-center gap-1">
          <PlusMenu items={plusItems} disabled={props.disabled} />
          {props.showModelPicker && (
            <ModelPicker
              sessionId={props.sessionId}
              value={props.modelValue}
              disabled={props.disabled}
            />
          )}
          <div className="flex-1" />
          {props.trailing}
          {props.busy ? (
            <button
              type="button"
              onClick={props.onStop}
              aria-label={t("Stop", "Stopp")}
              className={cn(ICON_BUTTON, "bg-foreground text-background")}
            >
              <Square className="size-3.5" aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!canSend}
              aria-label={props.sendLabel ?? t("Send", "Senden")}
              className={cn(
                ICON_BUTTON,
                canSend ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
              )}
            >
              <ArrowUp className="size-4" aria-hidden />
            </button>
          )}
        </div>
      </div>
      {error && (
        <p className="px-1 text-xs text-status-error" role="alert">
          {error}
        </p>
      )}
      <input
        ref={fileInput}
        type="file"
        multiple
        hidden
        accept=".pdf,.docx,.xlsx,.csv,.txt,.md,image/*"
        onChange={(e) => {
          void addFiles(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />
    </div>
  );
}
