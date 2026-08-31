import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { changePassword } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import { PageHeader } from "@/components/PageHeader";

export function ChangePasswordPage() {
  const { user, refreshMe } = useAuth();
  const navigate = useNavigate();
  const forced = !!user?.must_change_password;
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (next !== confirm) {
      setError("New passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await changePassword(current, next);
      await refreshMe();
      setDone(true);
      setTimeout(() => navigate("/", { replace: true }), 800);
    } catch (err) {
      setError(
        err instanceof ApiError ? `${err.status}: ${err.detail}` : err instanceof Error ? err.message : "Failed.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Change password"
        description={forced ? "Your account requires a new password before you can continue." : undefined}
      />
      <form className="card" style={{ maxWidth: 420 }} onSubmit={submit}>
        <div className="field">
          <label htmlFor="cur">Current password</label>
          <input id="cur" type="password" autoComplete="current-password" value={current}
                 onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="new">New password</label>
          <input id="new" type="password" autoComplete="new-password" value={next}
                 onChange={(e) => setNext(e.target.value)} />
          <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 4 }}>
            ≥ 10 chars, and at least three of: lowercase, uppercase, digit, symbol.
          </div>
        </div>
        <div className="field">
          <label htmlFor="cf">Confirm new password</label>
          <input id="cf" type="password" autoComplete="new-password" value={confirm}
                 onChange={(e) => setConfirm(e.target.value)} />
        </div>
        <button type="submit" className="primary" disabled={busy || !current || !next || done}>
          {done ? "Updated" : busy ? "Saving…" : "Update password"}
        </button>
        {error && <div className="form-error">{error}</div>}
      </form>
    </>
  );
}
