import { useRealtime } from "@/realtime/RealtimeProvider";
import { REALTIME_ENABLED } from "@/lib/config";

const LABEL: Record<string, { text: string; color: string; pulse?: boolean }> = {
  open: { text: "live", color: "var(--pos)", pulse: true },
  connecting: { text: "connecting…", color: "var(--warn)" },
  reconnecting: { text: "reconnecting…", color: "var(--warn)" },
  degraded: { text: "polling (fallback)", color: "var(--text-faint)" },
  idle: { text: "polling", color: "var(--text-faint)" },
  closed: { text: "offline", color: "var(--text-faint)" },
};

export function RealtimeIndicator() {
  const { status, lastEventAt } = useRealtime();
  if (!REALTIME_ENABLED) {
    return <span className="conn" title="Realtime disabled by build config — polling only">polling</span>;
  }
  const l = LABEL[status] ?? LABEL.idle;
  return (
    <span
      className="conn"
      title={
        status === "open"
          ? `realtime stream connected${lastEventAt ? ` · last event ${new Date(lastEventAt).toLocaleTimeString()}` : ""}`
          : "realtime unavailable — falling back to polling every 15s"
      }
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: l.color,
          display: "inline-block",
          animation: l.pulse ? "pulse 1.6s ease-in-out infinite" : undefined,
        }}
      />
      {l.text}
    </span>
  );
}
