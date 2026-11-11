import { useCapaIcon } from "@/lib/hooks";
import type { Integration } from "@/lib/connections";

/** The integration's own icon (its Capa's declared icon); nothing when it
 *  has none, so callers keep a text-only layout. */
export function IntegrationIcon({
  integration,
  size = 32,
}: {
  integration: Pick<Integration, "capaId">;
  size?: number;
}) {
  const { data } = useCapaIcon(integration.capaId);
  if (!data) return null;
  return (
    <img
      src={data}
      alt=""
      width={size}
      height={size}
      style={{ width: `${size / 16}rem`, height: `${size / 16}rem` }}
      className="shrink-0 rounded-md object-contain"
    />
  );
}
