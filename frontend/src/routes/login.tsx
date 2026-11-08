import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState, useEffect } from "react";
import { toast } from "sonner";
import { useT } from "@/lib/i18n";
import { PublicAuthLayout } from "@/components/public-auth-layout";
import { TotpChallenge } from "@/components/totp-challenge";
import { TotpEnroll } from "@/components/totp-enroll";
import type { OctopusMood } from "@/components/octopus-companion";

export const Route = createFileRoute("/login")({
  component: LoginPage,
});

interface SetupFormData {
  email: string;
  password: string;
  confirmPassword: string;
  displayName: string;
}

interface LoginFormData {
  email: string;
  password: string;
}

type PageMode = "setup" | "login" | null;

export function LoginPage() {
  const t = useT();
  const navigate = useNavigate();
  const [mode, setMode] = useState<PageMode>(null);
  const [loading, setLoading] = useState(false);
  const [celebrating, setCelebrating] = useState(false);
  const [alreadyInitializedNotice, setAlreadyInitializedNotice] = useState<string | null>(null);
  // Narrow, single-purpose tokens handed back by POST /auth/login when the
  // password alone is not enough. Deliberately NOT stored in
  // "oc8-community-token" -- that key is the real session, and anything
  // holding it is treated as logged in.
  const [totpChallengeToken, setTotpChallengeToken] = useState<string | null>(null);
  const [totpEnrollToken, setTotpEnrollToken] = useState<string | null>(null);

  const mood: OctopusMood = loading ? "thinking" : celebrating ? "celebrating" : "idle";

  // Flashes the companion to "celebrating" and navigates immediately -- no
  // artificial delay, so the form/button never re-enable while a completed
  // submission is still settling (that window previously let a second
  // request race in and 422 as "already initialized" right after success).
  const celebrateAndNavigate = (to: string) => {
    setCelebrating(true);
    navigate({ to });
  };

  // Check if instance is initialized by attempting to get the singleton org
  useEffect(() => {
    async function checkInitialization() {
      try {
        // Try to fetch /me without a token to see if instance is initialized
        // If we get a 404, the instance is not initialized
        const response = await fetch("/api/v1/auth/config");
        if (!response.ok) {
          setMode("setup");
          return;
        }
        const config = await response.json();
        // If mode is 'dev', we should not be here; redirect back
        if (config.mode === "dev") {
          navigate({ to: "/" });
          return;
        }
        // `initialized` says whether an administrator already exists, using
        // the same member-count predicate POST /auth/setup checks. Assuming
        // "uninitialized" here (which is what this did before) meant anyone
        // whose session expired was handed a Create-Admin form for an account
        // that already existed, and only learned otherwise from the 422 after
        // filling it in.
        setMode(config.initialized ? "login" : "setup");
      } catch (error) {
        console.error("Failed to check initialization:", error);
        setMode("setup");
      }
    }
    checkInitialization();
  }, [navigate]);

  const handleSetup = async (data: SetupFormData) => {
    setLoading(true);
    setAlreadyInitializedNotice(null);
    try {
      if (data.password !== data.confirmPassword) {
        toast.error(t("Passwords do not match", "Passwörter stimmen nicht überein"));
        setLoading(false);
        return;
      }

      const response = await fetch("/api/v1/auth/setup", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          email: data.email,
          password: data.password,
          display_name: data.displayName,
        }),
      });

      if (!response.ok) {
        const error = await response.json();
        // If 422 with "already initialized", show login instead
        if (response.status === 422 && error.detail?.includes("already initialized")) {
          setMode("login");
          const notice = t(
            "Instance already initialized. Please log in.",
            "Instanz bereits initialisiert. Bitte melde dich an.",
          );
          setAlreadyInitializedNotice(notice);
          toast.info(notice);
        } else {
          toast.error(error.detail || t("Setup failed", "Setup fehlgeschlagen"));
        }
        setLoading(false);
        return;
      }

      const result = await response.json();
      // Store token and redirect to welcome
      if (typeof window !== "undefined") {
        window.localStorage.setItem("oc8-community-token", result.token);
      }
      toast.success(t("Account created successfully", "Konto erfolgreich erstellt"));
      setLoading(false);
      celebrateAndNavigate("/welcome");
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : t("Setup failed", "Setup fehlgeschlagen"),
      );
      setLoading(false);
    }
  };

  const handleLogin = async (data: LoginFormData) => {
    setLoading(true);
    try {
      const response = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          email: data.email,
          password: data.password,
        }),
      });

      if (!response.ok) {
        const error = await response.json();
        if (response.status === 404) {
          // Instance not initialized
          setMode("setup");
          setAlreadyInitializedNotice(null);
          toast.info(
            t(
              "Instance not yet initialized. Please complete setup.",
              "Instanz noch nicht initialisiert. Bitte führe das Setup durch.",
            ),
          );
        } else {
          toast.error(error.detail || t("Login failed", "Anmeldung fehlgeschlagen"));
        }
        setLoading(false);
        return;
      }

      const result = await response.json();

      // Second factor still outstanding: `result.token` is a narrow
      // challenge/enrollment token, not a session. Stop before the tail below
      // so it is neither stored nor treated as a completed login.
      if (result.requiresTotpCode) {
        setTotpChallengeToken(result.token);
        setLoading(false);
        return;
      }
      if (result.requiresTotpEnrollment) {
        setTotpEnrollToken(result.token);
        setLoading(false);
        return;
      }

      // Login succeeded, but 2FA is still only pending within a grace window.
      // A banner on this page would be useless -- we navigate away in the same
      // breath -- so nag with a toast that survives the transition.
      if (result.totpGraceExpiresAt) {
        // Floor at 1: the backend only ever hands back a deadline in the
        // future, so a client clock running ahead is the one way this could
        // round to 0 (or below) -- and "within 0 day(s)" reads as a bug, not
        // as urgency.
        const days = Math.max(
          1,
          Math.ceil(
            (new Date(result.totpGraceExpiresAt).getTime() - Date.now()) / (1000 * 60 * 60 * 24),
          ),
        );
        toast.warning(
          t(
            `Set up two-factor authentication within ${days} day(s), or you will be locked out.`,
            `Richte die Zwei-Faktor-Authentifizierung innerhalb von ${days} Tag(en) ein, sonst wirst du ausgesperrt.`,
          ),
          { duration: 10000 },
        );
      }

      // Store token and redirect to dashboard
      if (typeof window !== "undefined") {
        window.localStorage.setItem("oc8-community-token", result.token);
      }
      toast.success(t("Logged in successfully", "Erfolgreich angemeldet"));
      setLoading(false);
      celebrateAndNavigate("/");
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : t("Login failed", "Anmeldung fehlgeschlagen"),
      );
      setLoading(false);
    }
  };

  if (totpChallengeToken) {
    return (
      <PublicAuthLayout mood={mood}>
        <TotpChallenge
          token={totpChallengeToken}
          onVerified={(sessionToken) => {
            if (typeof window !== "undefined") {
              window.localStorage.setItem("oc8-community-token", sessionToken);
            }
            celebrateAndNavigate("/");
          }}
        />
      </PublicAuthLayout>
    );
  }

  if (totpEnrollToken) {
    return (
      <PublicAuthLayout mood={mood}>
        <TotpEnroll
          token={totpEnrollToken}
          onDone={() => {
            // Confirming enrollment does not mint a session (Task 11's own
            // design) -- send the operator back to the form. `mode` is
            // already "login" to have got here at all, but set it explicitly
            // so this branch does not depend on that.
            setTotpEnrollToken(null);
            setMode("login");
            toast.success(
              t("2FA enabled. Please sign in again.", "2FA aktiviert. Bitte erneut anmelden."),
            );
          }}
        />
      </PublicAuthLayout>
    );
  }

  if (mode === null) {
    return (
      <PublicAuthLayout mood={mood}>
        <div className="text-center text-sm text-muted-foreground">
          {t("Loading…", "Wird geladen…")}
        </div>
      </PublicAuthLayout>
    );
  }

  return (
    <PublicAuthLayout mood={mood}>
      <div className="space-y-6">
        <div className="flex items-center justify-center">
          <img src="/oc8_Logo_white.svg" alt="oc8" className="h-10 w-auto" />
        </div>

        {/*
          Always mounted, even while empty: screen readers announce content that
          CHANGES inside an existing live region, not a region that appears with
          its text already in place.
        */}
        <p
          aria-live="polite"
          data-testid="auth-notice"
          className={
            alreadyInitializedNotice
              ? "rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-muted-foreground"
              : "sr-only"
          }
        >
          {alreadyInitializedNotice ?? ""}
        </p>

        {mode === "login" ? (
          <LoginForm onSubmit={handleLogin} loading={loading} t={t} />
        ) : (
          <SetupForm onSubmit={handleSetup} loading={loading} t={t} />
        )}
      </div>
    </PublicAuthLayout>
  );
}

function SetupForm({
  onSubmit,
  loading,
  t,
}: {
  onSubmit: (data: SetupFormData) => void;
  loading: boolean;
  t: (en: string, de: string) => string;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [displayName, setDisplayName] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit({ email, password, confirmPassword, displayName });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <h2 className="font-serif text-xl">{t("Create Admin Account", "Admin-Konto erstellen")}</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {t(
            "Set up your first administrator account to get started.",
            "Richte dein erstes Administrator-Konto ein.",
          )}
        </p>
      </div>

      <div className="space-y-3">
        <div>
          <label htmlFor="setup-email" className="block text-sm font-medium">
            {t("Email", "E-Mail")}
          </label>
          <input
            id="setup-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="admin@example.com"
            required
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
          />
        </div>

        <div>
          <label htmlFor="setup-display-name" className="block text-sm font-medium">
            {t("Display Name", "Anzeigename")}
          </label>
          <input
            id="setup-display-name"
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={t("Your name", "Dein Name")}
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
          />
        </div>

        <div>
          <label htmlFor="setup-password" className="block text-sm font-medium">
            {t("Password", "Passwort")}
          </label>
          <input
            id="setup-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t("At least 8 characters", "Mindestens 8 Zeichen")}
            minLength={8}
            required
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
          />
        </div>

        <div>
          <label htmlFor="setup-confirm-password" className="block text-sm font-medium">
            {t("Confirm Password", "Passwort bestätigen")}
          </label>
          <input
            id="setup-confirm-password"
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder={t("Repeat your password", "Wiederhole dein Passwort")}
            minLength={8}
            required
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={loading || !email || !password || !confirmPassword}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 disabled:opacity-50"
      >
        {loading ? t("Creating…", "Erstelle…") : t("Create Account", "Konto erstellen")}
      </button>
    </form>
  );
}

function LoginForm({
  onSubmit,
  loading,
  t,
}: {
  onSubmit: (data: LoginFormData) => void;
  loading: boolean;
  t: (en: string, de: string) => string;
}) {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit({ email, password });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <h2 className="font-serif text-xl">{t("Sign In", "Anmelden")}</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {t("Enter your credentials to continue.", "Gib deine Zugangsdaten ein.")}
        </p>
      </div>

      <div className="space-y-3">
        <div>
          <label htmlFor="login-email" className="block text-sm font-medium">
            {t("Email", "E-Mail")}
          </label>
          <input
            id="login-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="admin@example.com"
            required
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
          />
        </div>

        <div>
          <label htmlFor="login-password" className="block text-sm font-medium">
            {t("Password", "Passwort")}
          </label>
          <input
            id="login-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t("Your password", "Dein Passwort")}
            required
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm"
          />
          <button
            type="button"
            onClick={() => navigate({ to: "/forgot-password" })}
            className="mt-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {t("Forgot password?", "Passwort vergessen?")}
          </button>
        </div>
      </div>

      <button
        type="submit"
        disabled={loading || !email || !password}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 disabled:opacity-50"
      >
        {loading ? t("Signing in…", "Melde mich an…") : t("Sign In", "Anmelden")}
      </button>
    </form>
  );
}
