import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import { PublicAuthLayout } from "@/components/public-auth-layout";
import { useT } from "@/lib/i18n";
import { publicPost } from "@/lib/api";

export const Route = createFileRoute("/reset-password")({
  component: ResetPasswordPage,
  // Read the one query param this route cares about and drop anything
  // else, so a stale or tampered link cannot smuggle extra state in
  // through the URL.
  validateSearch: (search: Record<string, unknown>): { token: string } => ({
    token: typeof search.token === "string" ? search.token : "",
  }),
});

export function ResetPasswordPage() {
  const t = useT();
  const navigate = useNavigate();
  const { token } = Route.useSearch();
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await publicPost("/auth/password/reset", { token, newPassword });
      toast.success(
        t("Password reset. Please sign in.", "Passwort zurückgesetzt. Bitte anmelden."),
      );
      navigate({ to: "/login" });
    } catch {
      // Generic on purpose, regardless of what the backend actually said --
      // an invalid, expired, and already-used token all answer the same 400,
      // and this must not surface anything more specific than that.
      toast.error(
        t("This link is invalid or has expired.", "Dieser Link ist ungültig oder abgelaufen."),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <PublicAuthLayout>
      <form onSubmit={submit} className="space-y-3">
        <label htmlFor="reset-password" className="block text-sm font-medium">
          {t("New password", "Neues Passwort")}
        </label>
        <input
          id="reset-password"
          type="password"
          required
          minLength={8}
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          className="h-9 w-full rounded-md border border-border bg-background/60 px-2 text-sm"
        />
        <button
          type="submit"
          disabled={busy || newPassword.length < 8 || !token}
          className="h-9 w-full rounded-md bg-brand-gradient text-sm font-medium text-primary-foreground hover:brightness-110 disabled:opacity-50"
        >
          {t("Set new password", "Neues Passwort setzen")}
        </button>
      </form>
    </PublicAuthLayout>
  );
}
