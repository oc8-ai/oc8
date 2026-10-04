export type CopilotStatus = "paused" | "waiting" | "working" | "ready";

export function statusLabel(
  t: (en: string, de?: string) => string,
  status: CopilotStatus,
  count: number,
): string {
  switch (status) {
    case "ready":
      return t("Ready", "Bereit");
    case "working":
      return count === 1
        ? t("Working on 1 thing", "Arbeitet an 1 Sache")
        : t(`Working on ${count} things`, `Arbeitet an ${count} Dingen`);
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
