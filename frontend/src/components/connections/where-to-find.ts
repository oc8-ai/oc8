// "Wo finde ich das?": the vendor's click path for each login field. Keyed by
// credential type and field key; a field without an entry shows no hint.
export interface Hint {
  en: string;
  de: string;
}

const HINTS: Record<string, Record<string, Hint>> = {
  odoo_login: {
    url: {
      en: "The address you open Odoo with, e.g. https://firma.odoo.com.",
      de: "Die Adresse, mit der Sie Odoo öffnen, z. B. https://firma.odoo.com.",
    },
    database: {
      en: "Odoo: Settings → (at the bottom) About → database name. On odoo.com it is usually your subdomain.",
      de: "Odoo: Einstellungen → (ganz unten) Über → Datenbankname. Bei odoo.com meist Ihre Subdomain.",
    },
    username: {
      en: "The e-mail address you sign in to Odoo with.",
      de: "Die E-Mail-Adresse, mit der Sie sich bei Odoo anmelden.",
    },
    password: {
      en: "Odoo: your avatar (top right) → My Profile → Account Security → New API Key.",
      de: "Odoo: Ihr Avatar (oben rechts) → Mein Profil → Kontosicherheit → Neuer API-Schlüssel.",
    },
  },
  hubspot_login: {
    private_app_access_token: {
      en: "HubSpot: Settings (gear) → Integrations → Private Apps → Create a private app → Scopes (CRM) → Create app → Access token → Show token.",
      de: "HubSpot: Einstellungen (Zahnrad) → Integrationen → Private Apps → Private App erstellen → Bereiche (CRM) → App erstellen → Zugriffstoken → Token anzeigen.",
    },
  },
  jira_login: {
    url: {
      en: "The address of your Jira site, e.g. https://firma.atlassian.net.",
      de: "Die Adresse Ihrer Jira-Seite, z. B. https://firma.atlassian.net.",
    },
    username: {
      en: "The e-mail address of your Atlassian account.",
      de: "Die E-Mail-Adresse Ihres Atlassian-Kontos.",
    },
    api_token: {
      en: "id.atlassian.com → Security → API tokens → Create API token → copy it.",
      de: "id.atlassian.com → Sicherheit → API-Token → API-Token erstellen → kopieren.",
    },
  },
};

export function whereToFind(credentialType: string | null, fieldKey: string): Hint | null {
  if (!credentialType) return null;
  return HINTS[credentialType]?.[fieldKey] ?? null;
}
