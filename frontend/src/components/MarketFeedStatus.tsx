import { useState } from "react";
import { useMarketHealth, useMarketSessionStatus, useUpdateMarketSession } from "@/api/hooks";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/api/client";
import { formatIST } from "@/lib/format";

const STATE_CLASS: Record<string, string> = {
  RUNNING: "running",
  CONNECTING: "stopped",
  STALE: "stale",
  STOPPING: "stopped",
  STOPPED: "stopped",
  ERROR: "error",
  SESSION_REQUIRED: "error",
  NOT_CONFIGURED: "unknown",
};

export function MarketFeedStatus() {
  const health = useMarketHealth();
  const session = useMarketSessionStatus();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("ADMIN");
  const update = useUpdateMarketSession();

  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");

  const h = health.data;
  const s = session.data;
  const feed = h?.feed ?? s?.feed_state ?? "—";
  const sess = h?.session ?? s?.session_state ?? "—";
  const needsSession = sess === "SESSION_REQUIRED" || sess === "NOT_CONFIGURED";

  return (
    <div className="card">
      <h2>Market Feed</h2>
      <dl className="kv">
        <dt>Provider</dt>
        <dd>{h?.provider ?? s?.provider ?? "icici_breeze"}</dd>
        <dt>Session</dt>
        <dd>
          <span className={`badge ${STATE_CLASS[sess] ?? "unknown"}`}>
            <span className="dot" />
            {sess}
          </span>
        </dd>
        <dt>Feed</dt>
        <dd>
          <span className={`badge ${STATE_CLASS[feed] ?? "unknown"}`}>
            <span className="dot" />
            {feed}
          </span>
        </dd>
        <dt>Window</dt>
        <dd>
          {h ? `${h.start_time}–${h.stop_time} ${h.timezone}` : "—"}
        </dd>
        <dt>Last check</dt>
        <dd>{formatIST(s?.last_session_check ?? null)}</dd>
        {s?.credentials?.session_token_fingerprint && (
          <>
            <dt>Token</dt>
            <dd className="mono">
              set · {s.credentials.session_token_fingerprint} ({s.credentials.source})
            </dd>
          </>
        )}
        {(h?.last_error || s?.last_error) && (
          <>
            <dt>Last error</dt>
            <dd className="neg">{h?.last_error ?? s?.last_error}</dd>
          </>
        )}
      </dl>

      {needsSession && (
        <div className="inline-note warn" style={{ marginTop: 10 }}>
          Breeze session is <strong>{sess}</strong>. The market feed is not running. An admin must provide
          today’s session token.
        </div>
      )}

      {canManage && (
        <div style={{ marginTop: 12 }}>
          <button className="sm primary" onClick={() => setOpen(true)}>
            Refresh Breeze Session
          </button>
        </div>
      )}

      {open && (
        <div className="modal-overlay" role="dialog" aria-modal="true" onClick={() => setOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Refresh Breeze Session</h3>
            <p style={{ color: "var(--text-dim)", fontSize: 12.5, marginTop: 0 }}>
              Generate today’s token via the ICICI Breeze login flow, then paste it here. It is stored
              server-side only and never shown again.
            </p>
            <div className="field">
              <label>Session token</label>
              <input value={token} autoFocus onChange={(e) => setToken(e.target.value)} />
            </div>
            {update.error instanceof ApiError && (
              <div className="form-error">
                {update.error.status}: {update.error.detail}
              </div>
            )}
            {update.data && (
              <div className={`inline-note ${update.data.session_state === "VALID" ? "" : "warn"}`}>
                Result: <strong>{update.data.session_state}</strong>
              </div>
            )}
            <div className="actions">
              <button className="ghost" onClick={() => setOpen(false)} disabled={update.isPending}>
                Close
              </button>
              <button
                className="primary"
                disabled={update.isPending || token.trim().length === 0}
                onClick={() =>
                  update.mutate(
                    { session_token: token.trim() },
                    { onSuccess: (r) => r.session_state === "VALID" && setOpen(false) },
                  )
                }
              >
                {update.isPending ? "Checking…" : "Submit"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
