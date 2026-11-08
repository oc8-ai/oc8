// oc8 objects an answer mentions ("oc8://agent/<id>") and where they live.
export const OBJECT_KINDS = [
  "agent",
  "department",
  "run",
  "approval",
  "responsibility",
  "chat_session",
  "knowledge_base",
  "data_source",
  "file",
] as const;

export function objectHref(kind: string, id: string): string | null {
  const safe = encodeURIComponent(id);
  switch (kind) {
    case "agent":
      return `/agents/${safe}`;
    case "department":
      return `/departments/${safe}`;
    case "knowledge_base":
      return `/knowledge/bases/${safe}/content`;
    case "approval":
    case "responsibility":
      return "/workspace";
    case "run":
      return `/activity?run=${safe}`;
    case "chat_session":
      return `/copilot?session=${safe}`;
    default:
      return null;
  }
}

export function parseObjectUrl(href: string): { kind: string; id: string } | null {
  const match = /^oc8:\/\/([a-z_]+)\/([A-Za-z0-9-]+)$/.exec(href);
  return match ? { kind: match[1], id: match[2] } : null;
}
