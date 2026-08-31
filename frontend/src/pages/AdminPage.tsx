import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createAdminUser,
  deactivateAdminUser,
  getAudit,
  listAdminUsers,
  resetAdminUserPassword,
  updateAdminUser,
} from "@/api/endpoints";
import { ApiError } from "@/api/client";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { formatIST, relativeAge } from "@/lib/format";
import { useAuth } from "@/auth/AuthContext";

const ROLES = ["viewer", "trader", "operator", "admin"];

function UsersTab() {
  const { user: me } = useAuth();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["admin-users"], queryFn: listAdminUsers });
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [nu, setNu] = useState({ username: "", password: "", role: "viewer", email: "" });
  const [resetFor, setResetFor] = useState<{ id: number; username: string } | null>(null);
  const [resetPw, setResetPw] = useState("");

  const refresh = () => qc.invalidateQueries({ queryKey: ["admin-users"] });
  const wrap = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? `${e.status}: ${e.detail}` : e instanceof Error ? e.message : "Failed");
    }
  };

  return (
    <>
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Add user</h2>
        <div className="toolbar">
          <div className="field">
            <label>Username</label>
            <input value={nu.username} onChange={(e) => setNu({ ...nu, username: e.target.value })} />
          </div>
          <div className="field">
            <label>Temp password</label>
            <input type="text" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} />
          </div>
          <div className="field" style={{ minWidth: 140 }}>
            <label>Role</label>
            <select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}>
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
          <button
            className="primary"
            disabled={creating || !nu.username || !nu.password}
            onClick={() =>
              wrap(async () => {
                setCreating(true);
                try {
                  await createAdminUser({
                    username: nu.username.trim(),
                    password: nu.password,
                    role: nu.role,
                    email: nu.email || null,
                  });
                  setNu({ username: "", password: "", role: "viewer", email: "" });
                } finally {
                  setCreating(false);
                }
              })
            }
          >
            Create
          </button>
        </div>
        <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 8 }}>
          New users start as <code>viewer</code> unless set otherwise, and must change the password on first sign-in.
        </div>
        {err && <div className="form-error">{err}</div>}
      </div>

      <QueryBoundary query={users} empty={(d) => d.length === 0}>
        {(list) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th>Effective permissions</th>
                  <th>Active</th>
                  <th>Last login</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {list.map((u) => {
                  const self = u.id === me?.id;
                  return (
                    <tr key={u.id}>
                      <td>
                        {u.username}
                        {self && <span style={{ color: "var(--text-faint)" }}> (you)</span>}
                        {u.must_change_password && <span className="badge stopped" style={{ marginLeft: 6 }}>must change pw</span>}
                      </td>
                      <td>
                        <select
                          value={u.role}
                          disabled={self}
                          onChange={(e) => wrap(() => updateAdminUser(u.id, { role: e.target.value }))}
                        >
                          {ROLES.map((r) => (
                            <option key={r} value={r}>{r}</option>
                          ))}
                        </select>
                      </td>
                      <td className="mono" style={{ whiteSpace: "normal" }}>{u.effective_permissions.join(", ")}</td>
                      <td>
                        {u.is_active ? (
                          <span className="badge running"><span className="dot" />yes</span>
                        ) : (
                          <span className="badge error"><span className="dot" />no</span>
                        )}
                      </td>
                      <td>{u.last_login_at ? relativeAge(u.last_login_at) : "never"}</td>
                      <td className="row-actions">
                        <button className="sm ghost" onClick={() => { setResetFor({ id: u.id, username: u.username }); setResetPw(""); }}>
                          Reset pw
                        </button>
                        {!self && u.is_active && (
                          <button className="sm danger" onClick={() => wrap(() => deactivateAdminUser(u.id))}>
                            Deactivate
                          </button>
                        )}
                        {!self && !u.is_active && (
                          <button className="sm" onClick={() => wrap(() => updateAdminUser(u.id, { is_active: true }))}>
                            Reactivate
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>

      <ConfirmDialog
        open={resetFor !== null}
        title={`Reset password for ${resetFor?.username ?? ""}`}
        confirmLabel="Reset"
        danger
        body={
          <>
            <p style={{ marginTop: 0 }}>The user's sessions are revoked and they must set a new password on next sign-in.</p>
            <input
              type="text"
              placeholder="temporary password"
              value={resetPw}
              onChange={(e) => setResetPw(e.target.value)}
            />
          </>
        }
        onConfirm={() =>
          wrap(async () => {
            if (resetFor) await resetAdminUserPassword(resetFor.id, resetPw);
            setResetFor(null);
          })
        }
        onCancel={() => setResetFor(null)}
      />
    </>
  );
}

function AuditTab() {
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const audit = useQuery({
    queryKey: ["admin-audit", action, outcome],
    queryFn: () => getAudit({ action: action || undefined, outcome: outcome || undefined, limit: 300 }),
  });

  return (
    <>
      <div className="toolbar">
        <div className="field" style={{ minWidth: 180 }}>
          <label>Action</label>
          <input value={action} onChange={(e) => setAction(e.target.value)} placeholder="e.g. ALGO_START" />
        </div>
        <div className="field" style={{ minWidth: 140 }}>
          <label>Outcome</label>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
            <option value="">any</option>
            <option value="success">success</option>
            <option value="denied">denied</option>
            <option value="error">error</option>
          </select>
        </div>
        <button className="sm" onClick={() => audit.refetch()}>Refresh</button>
      </div>
      <QueryBoundary query={audit} empty={(d) => d.length === 0}>
        {(rows) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Time (IST)</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Outcome</th>
                  <th>IP</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>{formatIST(r.timestamp)}</td>
                    <td>{r.actor_label || r.actor}</td>
                    <td>{r.action}</td>
                    <td className="mono">{r.target ?? "—"}</td>
                    <td className={r.outcome === "denied" ? "neg" : r.outcome === "error" ? "neg" : "pos"}>
                      {r.outcome}
                    </td>
                    <td className="mono">{r.ip ?? "—"}</td>
                    <td style={{ whiteSpace: "normal" }}>
                      <code>{r.detail ? JSON.stringify(r.detail) : "—"}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>
    </>
  );
}

export function AdminPage() {
  const [tab, setTab] = useState<"users" | "audit">("users");
  return (
    <>
      <PageHeader
        title="Administration"
        description="User accounts, roles and the security audit trail. Requires the ADMIN permission."
        actions={
          <div className="row-actions">
            <button className={tab === "users" ? "primary sm" : "sm ghost"} onClick={() => setTab("users")}>
              Users
            </button>
            <button className={tab === "audit" ? "primary sm" : "sm ghost"} onClick={() => setTab("audit")}>
              Audit log
            </button>
          </div>
        }
      />
      {tab === "users" ? <UsersTab /> : <AuditTab />}
    </>
  );
}
