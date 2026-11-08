import { createFileRoute } from "@tanstack/react-router";
import { KeyRound, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  useCreateCredential,
  useCredentials,
  useCredentialTypes,
  useDeleteCredential,
  useTestCredential,
  useUpdateCredential,
  type CredentialDTO,
  type CredentialTypeFieldDTO,
} from "@/lib/hooks";
import { useMay } from "@/lib/governance-hooks";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import { useConfirm } from "@/hooks/use-confirm";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export const Route = createFileRoute("/credentials")({
  component: CredentialsPage,
});

export function CredentialsPage() {
  const t = useT();
  const { lang } = useLang();
  const may = useMay();
  // `secret:view`, not `secret:manage`, is the real hard gate: it is what the
  // nav entry itself checks, so reaching this page already implies it -- this
  // explicit check stays only for defense-in-depth against direct navigation.
  // `secret:manage` separately toggles the mutating affordances below (New /
  // Test / Delete), exactly like /models splits `model:view` (the nav gate)
  // from `model:manage` (which only hides its declare/delete buttons) rather
  // than blocking the whole page for view-only callers.
  const mayView = may("secret:view");
  const mayManage = may("secret:manage");
  const [createOpen, setCreateOpen] = useState(false);
  const [editingCredential, setEditingCredential] = useState<CredentialDTO | null>(null);
  const { data: credentials = [] } = useCredentials();
  const { data: types = [] } = useCredentialTypes();
  const deleteCredential = useDeleteCredential();
  const testCredential = useTestCredential();
  const { confirm, ConfirmDialog } = useConfirm();
  const typeLabel = (name: string) => {
    const ty = types.find((entry) => entry.name === name);
    if (!ty) return name;
    return resolveTranslation(ty.displayName, ty.displayNameTranslations, lang);
  };

  if (!mayView) {
    return (
      <Panel className="p-6">
        <p className="text-sm text-muted-foreground">
          {t("Your role does not include secret:view.", "Deine Rolle enthält secret:view nicht.")}
        </p>
      </Panel>
    );
  }

  return (
    <Panel className="p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
            <KeyRound className="size-4" />
          </div>
          <div>
            <h3 className="font-serif text-lg">{t("Credentials", "Zugangsdaten")}</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {t(
                "Reusable connections to other services -- pick one wherever a Capa or data source needs one.",
                "Wiederverwendbare Verbindungen zu anderen Diensten -- auswählbar überall, wo eine Capa oder Datenquelle eine braucht.",
              )}
            </p>
          </div>
        </div>
        {mayManage && (
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90"
          >
            <Plus className="size-3.5" />
            {t("New credential", "Neue Zugangsdaten")}
          </button>
        )}
      </div>

      {credentials.length === 0 ? (
        <p className="mt-6 text-sm text-muted-foreground">
          {t("No credentials yet.", "Noch keine Zugangsdaten.")}
        </p>
      ) : (
        <Table className="mt-6 text-sm">
          <TableHeader>
            <TableRow className="border-b border-border hover:bg-transparent">
              <TableHead className="py-3 micro-label">{t("Name", "Name")}</TableHead>
              <TableHead className="py-3 micro-label">{t("Type", "Typ")}</TableHead>
              <TableHead className="py-3 micro-label">
                {t("Last tested", "Zuletzt getestet")}
              </TableHead>
              <TableHead className="py-3 text-right micro-label">{t("Action", "Aktion")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {credentials.map((cred) => (
              <TableRow key={cred.id} className="border-b border-border/50">
                <TableCell className="py-3 font-medium">{cred.name}</TableCell>
                <TableCell className="py-3 text-muted-foreground">
                  {typeLabel(cred.credentialType)}
                </TableCell>
                <TableCell className="py-3 text-muted-foreground">
                  {cred.lastTestedAt == null
                    ? t("Never tested", "Nie getestet")
                    : cred.lastTestOk
                      ? t("OK", "OK")
                      : t("Failed", "Fehlgeschlagen")}
                </TableCell>
                <TableCell className="py-3 text-right">
                  {mayManage && (
                    <>
                      <button
                        type="button"
                        disabled={testCredential.isPending}
                        onClick={() =>
                          testCredential.mutate(cred.id, {
                            onSuccess: () =>
                              toast.success(t("Credential works", "Zugangsdaten funktionieren")),
                            onError: (e: unknown) =>
                              toast.error(
                                e instanceof Error
                                  ? e.message
                                  : t("Test failed", "Test fehlgeschlagen"),
                              ),
                          })
                        }
                        className="mr-2 rounded-md border border-border px-2 py-1 text-xs"
                      >
                        {t("Test", "Testen")}
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingCredential(cred)}
                        className="mr-2 rounded-md border border-border px-2 py-1 text-xs"
                      >
                        {t("Edit", "Bearbeiten")}
                      </button>
                      <button
                        type="button"
                        disabled={deleteCredential.isPending}
                        onClick={async () => {
                          const ok = await confirm({
                            title: t("Delete credential?", "Zugangsdaten löschen?"),
                            description: t(
                              `Delete "${cred.name}"? Anything still using it will break.`,
                              `"${cred.name}" löschen? Alles, was sie noch nutzt, hört auf zu funktionieren.`,
                            ),
                            confirmLabel: t("Delete", "Löschen"),
                            cancelLabel: t("Cancel", "Abbrechen"),
                          });
                          if (!ok) return;
                          deleteCredential.mutate(cred.id, {
                            onSuccess: () => toast.success(t("Deleted", "Gelöscht")),
                            onError: (e: unknown) =>
                              toast.error(
                                e instanceof Error
                                  ? e.message
                                  : t("Could not delete.", "Konnte nicht gelöscht werden."),
                              ),
                          });
                        }}
                        className="rounded-md border border-destructive/40 px-2 py-1 text-xs text-destructive"
                      >
                        {t("Delete", "Löschen")}
                      </button>
                    </>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <CreateCredentialDialog open={createOpen} onOpenChange={setCreateOpen} types={types} />
      <EditCredentialDialog
        credential={editingCredential}
        onOpenChange={(open) => {
          if (!open) setEditingCredential(null);
        }}
        types={types}
      />
      {ConfirmDialog}
    </Panel>
  );
}

function CreateCredentialDialog({
  open,
  onOpenChange,
  types,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  types: {
    name: string;
    displayName: string;
    displayNameTranslations: Record<string, string>;
    fields: CredentialTypeFieldDTO[];
  }[];
}) {
  const t = useT();
  const { lang } = useLang();
  const createCredential = useCreateCredential();
  const [name, setName] = useState("");
  const [credentialType, setCredentialType] = useState(types[0]?.name ?? "");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const selectedType = types.find((ty) => ty.name === credentialType);

  const reset = () => {
    setName("");
    setFieldValues({});
  };

  const submit = () => {
    if (!name.trim() || !credentialType) return;
    createCredential.mutate(
      { name: name.trim(), credentialType, fieldValues },
      {
        onSuccess: () => {
          toast.success(t("Credential created", "Zugangsdaten angelegt"));
          reset();
          onOpenChange(false);
        },
        onError: (e: unknown) =>
          toast.error(
            e instanceof Error
              ? e.message
              : t("Could not create.", "Konnte nicht angelegt werden."),
          ),
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("New credential", "Neue Zugangsdaten")}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-3 text-sm">
          <label className="grid gap-1.5">
            <span>{t("Type", "Typ")}</span>
            <select
              value={credentialType}
              onChange={(e) => setCredentialType(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
            >
              {types.map((ty) => (
                <option key={ty.name} value={ty.name}>
                  {resolveTranslation(ty.displayName, ty.displayNameTranslations, lang)}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1.5">
            <span>{t("Name", "Name")}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
              autoFocus
            />
          </label>
          {(selectedType?.fields ?? []).map((field) => (
            <label key={field.key} className="grid gap-1.5">
              <span>{resolveTranslation(field.label, field.label_translations, lang)}</span>
              <input
                type={field.kind === "password" ? "password" : "text"}
                value={fieldValues[field.key] ?? field.default ?? ""}
                onChange={(e) => setFieldValues((cur) => ({ ...cur, [field.key]: e.target.value }))}
                className="rounded-md border border-input bg-background px-3 py-2"
              />
            </label>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            onClick={submit}
            disabled={createCredential.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {createCredential.isPending ? t("Creating…", "Wird angelegt…") : t("Create", "Anlegen")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function EditCredentialDialog({
  credential,
  onOpenChange,
  types,
}: {
  credential: CredentialDTO | null;
  onOpenChange: (open: boolean) => void;
  types: {
    name: string;
    displayName: string;
    displayNameTranslations: Record<string, string>;
    fields: CredentialTypeFieldDTO[];
  }[];
}) {
  const t = useT();
  const { lang } = useLang();
  const updateCredential = useUpdateCredential();
  const [name, setName] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const selectedType = types.find((ty) => ty.name === credential?.credentialType);

  // Re-seed local state whenever a different credential is opened for
  // editing -- non-secret fields prefill from what's already stored,
  // secret-kind fields always start blank (the API never returns their
  // values), matching PATCH's own "omitted/blank means unchanged" contract.
  useEffect(() => {
    if (!credential) return;
    setName(credential.name);
    setFieldValues(
      Object.fromEntries(
        Object.entries(credential.fieldValues ?? {}).map(([k, v]) => [k, String(v ?? "")]),
      ),
    );
  }, [credential]);

  if (!credential) return null;

  const submit = () => {
    if (!name.trim()) return;
    const trimmedName = name.trim();
    updateCredential.mutate(
      {
        credentialId: credential.id,
        ...(trimmedName !== credential.name ? { name: trimmedName } : {}),
        fieldValues,
      },
      {
        onSuccess: () => {
          toast.success(t("Credential updated", "Zugangsdaten aktualisiert"));
          onOpenChange(false);
        },
        onError: (e: unknown) =>
          toast.error(
            e instanceof Error
              ? e.message
              : t("Could not update.", "Konnte nicht aktualisiert werden."),
          ),
      },
    );
  };

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("Edit credential", "Zugangsdaten bearbeiten")}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-3 text-sm">
          <label className="grid gap-1.5">
            <span>{t("Type", "Typ")}</span>
            <input
              disabled
              value={
                selectedType
                  ? resolveTranslation(
                      selectedType.displayName,
                      selectedType.displayNameTranslations,
                      lang,
                    )
                  : credential.credentialType
              }
              className="rounded-md border border-input bg-muted px-3 py-2 text-muted-foreground"
            />
          </label>
          <label className="grid gap-1.5">
            <span>{t("Name", "Name")}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
              autoFocus
            />
          </label>
          {(selectedType?.fields ?? []).map((field) => (
            <label key={field.key} className="grid gap-1.5">
              <span>{resolveTranslation(field.label, field.label_translations, lang)}</span>
              <input
                type={field.kind === "password" ? "password" : "text"}
                value={fieldValues[field.key] ?? ""}
                onChange={(e) => setFieldValues((cur) => ({ ...cur, [field.key]: e.target.value }))}
                placeholder={
                  field.kind === "password"
                    ? t("Leave blank to keep unchanged", "Leer lassen, um es unverändert zu lassen")
                    : resolveTranslation(field.placeholder, field.placeholder_translations, lang)
                }
                className="rounded-md border border-input bg-background px-3 py-2"
              />
            </label>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            onClick={submit}
            disabled={updateCredential.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {updateCredential.isPending
              ? t("Saving…", "Wird gespeichert…")
              : t("Save", "Speichern")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
