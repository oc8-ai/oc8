import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { PublicAuthLayout } from "@/components/public-auth-layout";
import { useT } from "@/lib/i18n";
import { publicPost } from "@/lib/api";

export const Route = createFileRoute("/forgot-password")({
  component: ForgotPasswordPage,
});

export function ForgotPasswordPage() {
  const t = useT();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await publicPost("/auth/password/forgot", { email });
    } catch {
      // Deliberately swallowed, not surfaced: the backend already never
      // leaks whether the email exists (202, identical body either way), so
      // the one case left that could still throw here is a network failure
      // -- and even that must not produce a visibly different outcome from
      // "sent", or the two become distinguishable by watching for an error.
    } finally {
      setBusy(false);
      // Always show the same success state, on every exit from the try.
      setSent(true);
    }
  }

  return (
    <PublicAuthLayout>
      {sent ? (
        <p className="text-sm text-muted-foreground">
          {t(
            "If that email exists and a mail server is configured, a reset link was sent.",
            "Falls diese E-Mail existiert und ein Mail-Server konfiguriert ist, wurde ein Link verschickt.",
          )}
        </p>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <label htmlFor="forgot-email" className="block text-sm font-medium">
            {t("Your email", "Deine E-Mail")}
          </label>
          <input
            id="forgot-email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="h-9 w-full rounded-md border border-border bg-background/60 px-2 text-sm"
          />
          <button
            type="submit"
            disabled={busy || !email}
            className="h-9 w-full rounded-md bg-brand-gradient text-sm font-medium text-primary-foreground hover:brightness-110 disabled:opacity-50"
          >
            {t("Send reset link", "Reset-Link senden")}
          </button>
        </form>
      )}
    </PublicAuthLayout>
  );
}
