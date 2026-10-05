export type CopilotStatus = "paused" | "waiting" | "working" | "ready";

export function statusLabel(
  t: (en: string, de?: string) => string,
  status: CopilotStatus,
  count: number,
): string {
  switch (status) {
    case "ready":
      return t("Ready", "Bereit");
    case "working": {
      if (count === 1) {
        return t("Working on 1 thing", "Arbeitet an 1 Sache");
      }
      const msg = t("Working on {n} things", "Arbeitet an {n} Dingen");
      return msg.replace("{n}", String(count));
    }
    case "waiting":
      return t("Waiting for you", "Wartet auf dich");
    case "paused":
      return t("Paused", "Pausiert");
  }
}

export function mascotState(status: CopilotStatus): "idle" | "working" | "waiting" | "paused" {
  switch (status) {
    case "ready":
      return "idle";
    case "working":
      return "working";
    case "waiting":
      return "waiting";
    case "paused":
      return "paused";
  }
}
