// How oc8 signs in to a plain HTTP API: nothing, an API key (header or query
// string), Basic, or OAuth2 client credentials. The secret is stored sealed
// by the setup form; only the other parts reach the connection's config.
import { Field } from "@/components/agent-identity-fields";
import type { HttpLoginDraft } from "@/lib/http-tools";
import { useT } from "@/lib/i18n";

const INPUT =
  "w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50";

export function HttpLoginFields({
  value,
  onChange,
}: {
  value: HttpLoginDraft;
  onChange: (next: HttpLoginDraft) => void;
}) {
  const t = useT();

  function setKind(kind: HttpLoginDraft["kind"]) {
    if (kind === "none") onChange({ kind });
    else if (kind === "api_key") onChange({ kind, in: "header", name: "X-API-Key", secret: "" });
    else if (kind === "basic") onChange({ kind, username: "", secret: "" });
    else onChange({ kind, tokenUrl: "", clientId: "", scope: "", secret: "" });
  }

  return (
    <div className="space-y-3">
      <Field label={t("Login", "Anmeldung")}>
        <select
          aria-label={t("Login", "Anmeldung")}
          value={value.kind}
          onChange={(e) => setKind(e.target.value as HttpLoginDraft["kind"])}
          className={INPUT}
        >
          <option value="none">{t("No login", "Keine Anmeldung")}</option>
          <option value="api_key">{t("API key", "API-Schlüssel")}</option>
          <option value="basic">
            {t("Username and password (Basic)", "Benutzername und Passwort (Basic)")}
          </option>
          <option value="oauth2_client_credentials">
            {t("OAuth2 client credentials", "OAuth2 Client Credentials")}
          </option>
        </select>
      </Field>

      {value.kind === "api_key" && (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label={t("Send the key in", "Schlüssel senden im")}>
            <select
              aria-label={t("Send the key in", "Schlüssel senden im")}
              value={value.in}
              onChange={(e) => onChange({ ...value, in: e.target.value as "header" | "query" })}
              className={INPUT}
            >
              <option value="header">{t("Header", "Header")}</option>
              <option value="query">{t("Query string", "Query-String")}</option>
            </select>
          </Field>
          <Field label={t("Key name", "Name des Schlüssels")}>
            <input
              aria-label={t("Key name", "Name des Schlüssels")}
              value={value.name}
              onChange={(e) => onChange({ ...value, name: e.target.value })}
              className={`${INPUT} font-mono`}
            />
          </Field>
          <Field label={t("API key", "API-Schlüssel")}>
            <input
              aria-label={t("API key", "API-Schlüssel")}
              type="password"
              value={value.secret}
              onChange={(e) => onChange({ ...value, secret: e.target.value })}
              className={`${INPUT} font-mono`}
            />
          </Field>
        </div>
      )}

      {value.kind === "basic" && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("Username", "Benutzername")}>
            <input
              aria-label={t("Username", "Benutzername")}
              value={value.username}
              onChange={(e) => onChange({ ...value, username: e.target.value })}
              className={INPUT}
            />
          </Field>
          <Field label={t("Password", "Passwort")}>
            <input
              aria-label={t("Password", "Passwort")}
              type="password"
              value={value.secret}
              onChange={(e) => onChange({ ...value, secret: e.target.value })}
              className={INPUT}
            />
          </Field>
        </div>
      )}

      {value.kind === "oauth2_client_credentials" && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("Token URL", "Token-URL")}>
            <input
              aria-label={t("Token URL", "Token-URL")}
              value={value.tokenUrl}
              placeholder="https://auth.example.com/oauth/token"
              onChange={(e) => onChange({ ...value, tokenUrl: e.target.value })}
              className={`${INPUT} font-mono`}
            />
          </Field>
          <Field label={t("Scope (optional)", "Scope (optional)")}>
            <input
              aria-label={t("Scope (optional)", "Scope (optional)")}
              value={value.scope}
              onChange={(e) => onChange({ ...value, scope: e.target.value })}
              className={`${INPUT} font-mono`}
            />
          </Field>
          <Field label={t("Client ID", "Client-ID")}>
            <input
              aria-label={t("Client ID", "Client-ID")}
              value={value.clientId}
              onChange={(e) => onChange({ ...value, clientId: e.target.value })}
              className={`${INPUT} font-mono`}
            />
          </Field>
          <Field label={t("Client secret", "Client-Secret")}>
            <input
              aria-label={t("Client secret", "Client-Secret")}
              type="password"
              value={value.secret}
              onChange={(e) => onChange({ ...value, secret: e.target.value })}
              className={`${INPUT} font-mono`}
            />
          </Field>
        </div>
      )}
    </div>
  );
}
