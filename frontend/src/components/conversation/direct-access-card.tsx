// The Copilot's direct access in the chat (copilot direct access §3, §8): the
// offer and every held write as a card in the turn, and a banner while a grant
// is active. Decided through the chat's own door -- the person needs no seat.
import { ExternalLink, KeyRound, X } from "lucide-react";
import { useState } from "react";

import { isHttpUrl, RecordLink } from "@/components/dashboard/widgets/approvals-widget";
import { Button } from "@/components/ui/button";
import {
  useDecideDirectAccess,
  useDirectAccessGrants,
  useRevokeDirectAccess,
} from "@/lib/direct-access-api";
import type { Approval } from "@/lib/hooks";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";

export function DirectAccessCard({ approval }: { approval: Approval }) {
  const t = useT();
  const { lang } = useLang();
  const info = approval.directAccess;
  const decide = useDecideDirectAccess();
  const revoke = useRevokeDirectAccess(info?.sessionId ?? "");
  const { data: grants } = useDirectAccessGrants(info?.sessionId ?? null);
  const [decided, setDecided] = useState<"approved" | "rejected" | null>(null);
  if (!info) return null;
  const isOffer = approval.actionType === "direct_access";
  const status = decided ?? approval.status;
  const grant = isOffer ? grants?.find((g) => g.approvalId === approval.id) : undefined;
  const title = resolveTranslation(approval.title, approval.titleTranslations, lang);
  const account =
    info.account === "own"
      ? t("Using your own login", "Mit deinem eigenen Login")
      : info.account === "organisation"
        ? t("Using the organisation account", "Mit dem Organisationskonto")
        : t("The account could not be determined.", "Das Konto konnte nicht ermittelt werden.");
  const answer = (decision: "approve" | "reject") =>
    decide.mutate(
      { approvalId: approval.id, decision },
      { onSuccess: () => setDecided(decision === "approve" ? "approved" : "rejected") },
    );

  return (
    <div className="rounded-lg border border-border bg-card p-4 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <KeyRound className="size-4 text-primary" aria-hidden />
        <span>{title}</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{account}</p>
      {approval.detail && <p className="mt-2">{approval.detail}</p>}
      {isOffer && (
        <p className="mt-2 text-xs text-muted-foreground">
          {t(
            "Reading is allowed for the rest of this chat. Every change asks you again.",
            "Lesen ist für den Rest dieses Chats erlaubt. Jede Änderung fragt dich erneut.",
          )}
        </p>
      )}
      {isHttpUrl(approval.recordUrl) && (
        <RecordLink href={approval.recordUrl}>
          <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {t("Open the record", "Datensatz öffnen")}
        </RecordLink>
      )}
      {approval.relatedLinks
        ?.filter((link) => isHttpUrl(link.url))
        .map((link) => (
          <RecordLink key={link.url} href={link.url}>
            ↗ {link.label}
          </RecordLink>
        ))}
      {isHttpUrl(approval.resultUrl) && (
        <RecordLink href={approval.resultUrl}>
          <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {t("Created record", "Angelegter Datensatz")}
        </RecordLink>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {status === "pending" && info.decider === "member" && (
          <>
            <Button size="sm" disabled={decide.isPending} onClick={() => answer("approve")}>
              {isOffer ? t("Allow", "Erlauben") : t("Approve", "Zustimmen")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={decide.isPending}
              onClick={() => answer("reject")}
            >
              {t("Reject", "Ablehnen")}
            </Button>
          </>
        )}
        {status === "pending" && info.decider === "department" && (
          <span className="text-xs text-muted-foreground">
            {t("Waiting for the department's approval.", "Wartet auf die Freigabe der Abteilung.")}
          </span>
        )}
        {status === "approved" && (
          <span className="text-xs text-muted-foreground">{t("Approved", "Zugestimmt")}</span>
        )}
        {status === "rejected" && (
          <span className="text-xs text-muted-foreground">{t("Rejected", "Abgelehnt")}</span>
        )}
        {grant && (
          <Button
            size="sm"
            variant="ghost"
            disabled={revoke.isPending}
            onClick={() => revoke.mutate(grant.id)}
          >
            {t("Revoke access", "Zugriff entziehen")}
          </Button>
        )}
      </div>
    </div>
  );
}

/** "Direct access: Odoo (read) · Revoke" while a grant is active (§8). */
export function DirectAccessBanner({ sessionId }: { sessionId: string }) {
  const t = useT();
  const { data: grants } = useDirectAccessGrants(sessionId);
  const revoke = useRevokeDirectAccess(sessionId);
  if (!grants || grants.length === 0) return null;
  return (
    <div
      role="status"
      className="inline-flex min-h-9 flex-wrap items-center gap-2 rounded-full border border-border bg-card pl-3 pr-1 text-sm"
    >
      <KeyRound className="size-4 text-primary" aria-hidden />
      {grants.map((g) => (
        <span key={g.id} className="inline-flex items-center gap-1">
          {t("Direct access", "Direktzugriff")}: {g.label} ({t("read", "lesen")}) ·
          <button
            type="button"
            onClick={() => revoke.mutate(g.id)}
            disabled={revoke.isPending}
            aria-label={`${t("Revoke", "Entfernen")} ${g.label}`}
            className="inline-flex items-center gap-1 rounded-full px-2 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-60"
          >
            {t("Revoke", "Entfernen")}
            <X className="size-3.5" aria-hidden />
          </button>
        </span>
      ))}
    </div>
  );
}
