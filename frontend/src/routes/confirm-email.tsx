import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { PublicAuthLayout } from "@/components/public-auth-layout";
import { useT } from "@/lib/i18n";
import { publicPost } from "@/lib/api";
import { spendOnce, urlToken, useUrlToken } from "@/lib/url-token";

export const Route = createFileRoute("/confirm-email")({
  component: ConfirmEmailPage,
  validateSearch: (search: Record<string, unknown>): { token: string } => ({
    token: urlToken(search.token),
  }),
});

export function ConfirmEmailPage() {
  const t = useT();
  const navigate = useNavigate();
  // Read once, then taken out of the address bar (it is a secret).
  const token = useUrlToken(Route.useSearch().token);
  const [status, setStatus] = useState<"pending" | "done" | "error">("pending");

  useEffect(() => {
    if (!token) {
      setStatus("error");
      return;
    }
    let cancelled = false;
    spendOnce(`confirm:${token}`, () => publicPost("/auth/email/confirm", { token }))
      .then(() => {
        if (!cancelled) setStatus("done");
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <PublicAuthLayout>
      {status === "pending" && (
        <p className="text-sm text-muted-foreground">{t("Confirming…", "Wird bestätigt…")}</p>
      )}
      {status === "done" && (
        <div className="space-y-3">
          <p className="text-sm text-foreground">
            {t("Your email has been updated.", "Deine E-Mail wurde aktualisiert.")}
          </p>
          {/* A plain button + `useNavigate`, not `<Link>`: `<Link>` reads
              `RouterProvider`'s context directly and throws when rendered
              outside one (see `login.tsx`'s "Forgot password?" trigger for
              the same reasoning) -- this route mounts standalone in its own
              tests the same way `login.tsx`'s do. */}
          <button
            type="button"
            onClick={() => navigate({ to: "/profile" })}
            className="text-sm text-primary hover:underline"
          >
            {t("Back to your profile", "Zurück zum Profil")}
          </button>
        </div>
      )}
      {status === "error" && (
        <p className="text-sm text-destructive">
          {t("This link is invalid or has expired.", "Dieser Link ist ungültig oder abgelaufen.")}
        </p>
      )}
    </PublicAuthLayout>
  );
}
