import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { TradingModeBadge } from "@/components/TradingModeBadge";
import { CONFIGURED_API_BASE_URL } from "@/lib/config";
import { ApiError } from "@/api/client";

export function LoginPage() {
  const { signIn, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(CONFIGURED_API_BASE_URL);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(apiKey, showAdvanced ? baseUrl : undefined);
      navigate("/", { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 401
            ? "That API key was rejected by the backend."
            : err.status === 0
              ? err.detail
              : `${err.status}: ${err.detail}`,
        );
      } else {
        setError(err instanceof Error ? err.message : "Sign-in failed.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <TradingModeBadge />
        <h1 style={{ textAlign: "center", marginBottom: 4 }}>Trading Control Center</h1>
        <p style={{ textAlign: "center", color: "var(--text-dim)", marginTop: 0, marginBottom: 20, fontSize: 13 }}>
          Sign in with the control API key.
        </p>

        <div className="field">
          <label htmlFor="apiKey">API key (X-API-Key)</label>
          <input
            id="apiKey"
            type="password"
            autoComplete="current-password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="paste CONTROL_API_KEY"
            autoFocus
          />
        </div>

        {showAdvanced && (
          <div className="field">
            <label htmlFor="baseUrl">API base URL (optional)</label>
            <input
              id="baseUrl"
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="blank = same-origin /api (dev proxy / nginx)"
            />
          </div>
        )}

        <button type="submit" className="primary" style={{ width: "100%" }} disabled={busy || !apiKey.trim()}>
          {busy ? "Verifying…" : "Sign in"}
        </button>

        <button
          type="button"
          className="ghost sm"
          style={{ width: "100%", marginTop: 8, border: "none" }}
          onClick={() => setShowAdvanced((v) => !v)}
        >
          {showAdvanced ? "Hide" : "Advanced"} connection settings
        </button>

        {error && <div className="form-error">{error}</div>}
      </form>
    </div>
  );
}
