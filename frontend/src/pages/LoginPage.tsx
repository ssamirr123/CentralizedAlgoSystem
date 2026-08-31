import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { TradingModeBadge } from "@/components/TradingModeBadge";
import { ApiError } from "@/api/client";

export function LoginPage() {
  const { signIn, isAuthenticated, baseUrl, setBaseUrl } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [baseUrlInput, setBaseUrlInput] = useState(baseUrl);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (isAuthenticated) return <Navigate to="/" replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    if (showAdvanced) setBaseUrl(baseUrlInput.trim());
    try {
      const user = await signIn(username.trim(), password);
      navigate(user.must_change_password ? "/change-password" : "/", { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 401
            ? "Invalid username or password."
            : err.status === 429
              ? "Too many attempts. Wait a few minutes and try again."
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
          Sign in with your account.
        </p>

        <div className="field">
          <label htmlFor="username">Username</label>
          <input
            id="username"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {showAdvanced && (
          <div className="field">
            <label htmlFor="baseUrl">API base URL (optional)</label>
            <input
              id="baseUrl"
              type="text"
              value={baseUrlInput}
              onChange={(e) => setBaseUrlInput(e.target.value)}
              placeholder="blank = same-origin /api"
            />
          </div>
        )}

        <button type="submit" className="primary" style={{ width: "100%" }} disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
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
