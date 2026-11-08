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
  useEffect,
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
import {
  ATTACHMENT_ACCEPT,
  MAX_ATTACHMENT_BYTES,
  isAllowedAttachment,
  uploadChatAttachment,
  type FileAttachmentDTO,
} from "@/lib/api";
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
  /** Focus the message box on mount (the dock, when it opens). */
  autoFocus?: boolean;
  sendLabel?: string;
  /** C3: lets the + menu and # add a ref from outside (data source browser). */
  contextRefs?: ContextItem[];
  onContextRefsChange?: (refs: ContextItem[]) => void;
  /** C2: what is sent answers an open question, as text only. The hint says
   *  so; the + menu, the drop zone and # are off, and a send that still
   *  carries files or context is refused (they are never dropped). */
  textOnlyHint?: string;
}

const MAX_REFS = 10;
// Read when needed, not at import: every page imports this through the dock,
// and tests that stub `@/lib/api` should not have to stub this constant too.
function maxMb(): number {
  return Math.round(MAX_ATTACHMENT_BYTES / (1024 * 1024));
}

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
  // The token Esc closed; it stays closed until the caret moves or the
  // token changes, so Esc is not swallowed by a popover that reopens at once.
  const [dismissed, setDismissed] = useState<SigilToken | null>(null);
  const textOnly = !!props.textOnlyHint;
  const typedSigil = props.disabled ? null : activeSigil(props.draft, caret);
  // # pins context, which an answer cannot carry.
  const rawSigil = textOnly && typedSigil?.kind === "#" ? null : typedSigil;
  const sigil: SigilToken | null =
    rawSigil &&
    !(
      dismissed &&
      dismissed.kind === rawSigil.kind &&
      dismissed.start === rawSigil.start &&
      dismissed.end === rawSigil.end &&
      dismissed.query === rawSigil.query
    )
      ? rawSigil
      : null;

  // Attachments and context belong to one chat. Switching chats drops them
  // (and ignores uploads still running for the old one); the first session
  // being created under a draft (null -> id) keeps them.
  const generation = useRef(0);
  const [seenSession, setSeenSession] = useState(props.sessionId);
  if (seenSession !== props.sessionId) {
    setSeenSession(props.sessionId);
    if (seenSession !== null) {
      generation.current += 1;
      setAttachments([]);
      setOwnRefs([]);
      setError(null);
    }
  }
  const { onContextRefsChange } = props;
  const previousSession = useRef(props.sessionId);
  useEffect(() => {
    const before = previousSession.current;
    previousSession.current = props.sessionId;
    if (before !== null && before !== props.sessionId) onContextRefsChange?.([]);
  }, [props.sessionId, onContextRefsChange]);
  const autoFocus = !!props.autoFocus;
  useEffect(() => {
    if (autoFocus) textarea.current?.focus();
    // Only on mount: the dock mounts a fresh chat whenever it opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
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
    if (textOnly) {
      setError(
        t(
          "Files can be attached once the question is answered.",
          "Dateien kannst du anhängen, sobald die Frage beantwortet ist.",
        ),
      );
      return;
    }
    const refused: string[] = [];
    const accepted: File[] = [];
    for (const file of files) {
      if (!isAllowedAttachment(file)) {
        refused.push(
          t(
            `${file.name}: file type not supported`,
            `${file.name}: Dateityp wird nicht unterstützt`,
          ),
        );
      } else if (file.size > MAX_ATTACHMENT_BYTES) {
        refused.push(
          t(
            `${file.name} is larger than ${maxMb()} MB. Pick a smaller file.`,
            `${file.name} ist größer als ${maxMb()} MB. Wähl eine kleinere Datei.`,
          ),
        );
      } else {
        accepted.push(file);
      }
    }
    setError(refused.length > 0 ? refused.join(". ") : null);
    if (accepted.length === 0) return;
    const mine = generation.current;
    // Every file waits for the one session: resolved once, before the loop,
    // so a multi-file drop on a new chat never creates two.
    setUploading((n) => n + accepted.length);
    let sessionId: string;
    try {
      sessionId = props.sessionId ?? (await props.ensureSession());
    } catch {
      setUploading((n) => n - accepted.length);
      if (mine === generation.current) {
        setError(
          t(
            "Your files could not be uploaded. Try again.",
            "Deine Dateien konnten nicht hochgeladen werden. Versuch es noch einmal.",
          ),
        );
      }
      return;
    }
    for (const file of accepted) {
      try {
        const uploaded = await uploadChatAttachment(sessionId, file);
        if (mine === generation.current) setAttachments((prev) => [...prev, uploaded]);
      } catch {
        if (mine !== generation.current) continue;
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
    if (textOnly && (attachments.length > 0 || refs.length > 0)) {
      // Never dropped silently: the draft and the chips stay as they are.
      setError(
        t(
          "An answer is sent as text only. Remove the attachments, or send them after answering.",
          "Eine Antwort geht nur als Text. Entfern die Anhänge oder schick sie nach der Antwort.",
        ),
      );
      return;
    }
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
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    // Ctrl/Cmd/Alt chords belong to the browser and the OS, not to us.
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape" && sigil) {
      e.preventDefault();
      setDismissed(sigil);
      return;
    }
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

  function carriesFiles(e: DragEvent<HTMLDivElement>): boolean {
    const dt = e.dataTransfer;
    return Array.from(dt?.types ?? []).includes("Files") || (dt?.files?.length ?? 0) > 0;
  }

  function onDragOver(e: DragEvent<HTMLDivElement>) {
    if (!carriesFiles(e)) return;
    // Always claim a file drag, read-only too: otherwise the browser
    // navigates away to open the dropped file.
    e.preventDefault();
    if (!props.disabled && !textOnly) setDragging(true);
  }

  function onDragLeave(e: DragEvent<HTMLDivElement>) {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    setDragging(false);
    const files = Array.from(e.dataTransfer?.files ?? []);
    if (files.length === 0) return;
    e.preventDefault();
    if (!props.disabled) void addFiles(files);
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
    <div className="space-y-2" onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
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
          <PlusMenu items={plusItems} disabled={props.disabled || textOnly} />
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
      {props.textOnlyHint && (
        <p className="px-1 text-xs text-muted-foreground">{props.textOnlyHint}</p>
      )}
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
        accept={ATTACHMENT_ACCEPT}
        onChange={(e) => {
          void addFiles(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />
    </div>
  );
}
