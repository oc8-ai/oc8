import { Copy, ExternalLink } from "lucide-react";
import { toast } from "sonner";
import { useT } from "@/lib/i18n";

/** The identity provider's stable id for a person (the token's `sub`).
 *  Shown small and muted, never labelled as an email: with an external OIDC
 *  provider it is often a uuid. Copyable, because support needs it verbatim. */
export function UserIdLine({ subject }: { subject: string }) {
  const t = useT();
  return (
    <CopyableIdLine
      label={t("User ID", "Benutzer-ID")}
      copyLabel={t("Copy user ID", "Benutzer-ID kopieren")}
      value={subject}
      testId="user-id"
    />
  );
}

/** The organisation (tenant) the person signed in to. Every member sees it on
 *  their profile -- only administrators see the organisation's settings -- so
 *  anyone can tell support which organisation they write from. */
export function OrganisationIdLine({ tenantId }: { tenantId: string }) {
  const t = useT();
  return (
    <div className="grid gap-0.5">
      <CopyableIdLine
        label={t("Organisation ID", "Organisations-ID")}
        copyLabel={t("Copy organisation ID", "Organisations-ID kopieren")}
        value={tenantId}
        testId="organisation-id"
      />
      <span className="text-xs text-muted-foreground">
        {t(
          "Please include it when you contact support.",
          "Bitte geben Sie sie bei Support-Anfragen an.",
        )}
      </span>
    </div>
  );
}

function CopyableIdLine({
  label,
  copyLabel,
  value,
  testId,
}: {
  label: string;
  copyLabel: string;
  value: string;
  testId: string;
}) {
  const t = useT();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(t("Copied", "Kopiert"));
    } catch {
      toast.error(t("Could not copy.", "Konnte nicht kopiert werden."));
    }
  };
  return (
    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <span>{label}:</span>
      <code className="truncate font-mono" data-testid={testId}>
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={copyLabel}
        className="rounded p-0.5 transition hover:bg-muted hover:text-foreground"
      >
        <Copy className="size-3" />
      </button>
    </div>
  );
}

/** Opens the identity provider's own account page, where a person changes
 *  their sign-in email and password. Only ever for the person themselves. */
export function AccountManagementLink({ url }: { url: string }) {
  const t = useT();
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener"
      className="inline-flex w-fit items-center gap-1.5 rounded-md border border-border bg-panel px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-accent"
    >
      <ExternalLink className="size-3.5" />
      {t("Change email or password", "E-Mail oder Passwort ändern")}
    </a>
  );
}

/** What the sign-in field says under it when an external identity provider
 *  owns sign-in: a link for the person themselves, a pointer for everybody
 *  else, and the plain hint when the deployment names no account page. */
export function ExternalSignInHint({
  accountUrl,
  isSelf,
}: {
  accountUrl: string | null | undefined;
  isSelf: boolean;
}) {
  const t = useT();
  if (!accountUrl) {
    return (
      <span className="text-xs text-muted-foreground">
        {t(
          "Managed by your identity provider; not editable here.",
          "Wird von Ihrem Identity-Provider verwaltet; hier nicht bearbeitbar.",
        )}
      </span>
    );
  }
  if (isSelf) return <AccountManagementLink url={accountUrl} />;
  return (
    <span className="text-xs text-muted-foreground">
      {t(
        "Each person changes their email and password in their own account.",
        "Jede Person ändert ihre E-Mail-Adresse und ihr Passwort selbst in ihrem eigenen Konto.",
      )}
    </span>
  );
}
