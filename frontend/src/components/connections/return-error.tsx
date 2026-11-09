import { useT } from "@/lib/i18n";
import { returnErrorText } from "./texts";

/** Explains a failed sign-in in plain words. For a missing administrator
 *  approval it also shows the link the IT administrator opens, copyable. */
export function ReturnError({
  code,
  adminConsentUrl,
}: {
  code: string;
  adminConsentUrl?: string | null;
}) {
  const t = useT();
  return (
    <div role="alert" className="space-y-2 text-sm">
      <p>{returnErrorText(code, t)}</p>
      {code === "admin_consent_required" && adminConsentUrl && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            readOnly
            aria-label={t(
              "Approval link for your IT administrator",
              "Freigabe-Link für Ihren IT-Administrator",
            )}
            value={adminConsentUrl}
            onFocus={(event) => event.currentTarget.select()}
            className="min-h-11 min-w-0 flex-1 rounded-md border border-border bg-background px-3 text-xs"
          />
          <button
            type="button"
            onClick={() => void navigator.clipboard?.writeText(adminConsentUrl)}
            className="inline-flex min-h-11 items-center rounded-md border border-border px-4 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            {t("Copy link", "Link kopieren")}
          </button>
        </div>
      )}
    </div>
  );
}
