import { useEffect, useRef, useState } from "react";
import { useRealtime } from "@/realtime/RealtimeProvider";
import { formatIST } from "@/lib/format";

const SEV_COLOR: Record<string, string> = {
  critical: "var(--neg)",
  warning: "var(--warn)",
  info: "var(--info)",
};

export function AlertsBell() {
  const { alerts, unreadAlerts, markAlertsRead } = useRealtime();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        className="sm ghost"
        style={{ border: "none", position: "relative" }}
        onClick={() => {
          setOpen((v) => !v);
          if (!open) markAlertsRead();
        }}
        title="Realtime alerts"
      >
        Alerts
        {unreadAlerts > 0 && (
          <span
            style={{
              position: "absolute",
              top: -2,
              right: -4,
              background: "var(--neg)",
              color: "#fff",
              borderRadius: 999,
              fontSize: 10,
              lineHeight: "14px",
              minWidth: 14,
              height: 14,
              padding: "0 3px",
              textAlign: "center",
            }}
          >
            {unreadAlerts > 99 ? "99+" : unreadAlerts}
          </span>
        )}
      </button>
      {open && (
        <div
          className="card"
          style={{
            position: "absolute",
            right: 0,
            top: "calc(100% + 6px)",
            width: 360,
            maxHeight: 420,
            overflowY: "auto",
            zIndex: 50,
            padding: 8,
          }}
        >
          {alerts.length === 0 ? (
            <div className="state" style={{ padding: 20 }}>
              No alerts this session.
            </div>
          ) : (
            alerts.map((a) => (
              <div
                key={a.seq}
                style={{
                  padding: "8px 10px",
                  borderBottom: "1px solid var(--border)",
                  display: "flex",
                  gap: 8,
                }}
              >
                <span
                  style={{
                    width: 6,
                    borderRadius: 3,
                    background: SEV_COLOR[a.severity] ?? "var(--text-faint)",
                    flexShrink: 0,
                  }}
                />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13 }}>{a.message}</div>
                  <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
                    {a.kind}
                    {a.algo_id ? ` · ${a.algo_id}` : ""}
                    {a.server_id ? ` · ${a.server_id}` : ""} · {formatIST(a.ts)}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
