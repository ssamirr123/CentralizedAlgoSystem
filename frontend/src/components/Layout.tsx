import type { ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { NAV_ROUTES } from "@/routes";
import { useAuth } from "@/auth/AuthContext";
import { useHealth } from "@/api/hooks";
import { TradingModeBadge, TradingModeStripe } from "./TradingModeBadge";
import { RealtimeIndicator } from "./RealtimeIndicator";
import { AlertsBell } from "./AlertsBell";
import { MarketTicker } from "./MarketTicker";

function ConnIndicator() {
  const { data, isError, isLoading } = useHealth();
  const ok = !isError && data?.status === "ok";
  const color = isLoading ? "var(--text-faint)" : ok ? "var(--pos)" : "var(--neg)";
  const text = isLoading
    ? "checking…"
    : isError
      ? "backend unreachable"
      : `backend ${data?.status ?? "?"} · db ${data?.database ?? "?"}`;
  return (
    <span className="conn">
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, display: "inline-block" }} />
      {text}
    </span>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { user, signOut, hasPermission } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const visible = NAV_ROUTES.filter((r) => hasPermission(r.permission));
  const active = NAV_ROUTES.find((r) => r.path === location.pathname);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">Trading Control Center</div>
        <nav>
          {visible.map((r) => (
            <NavLink key={r.path} to={r.path} end={r.path === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="dot" />
              {r.label}
            </NavLink>
          ))}
        </nav>
        <div style={{ flex: 1 }} />
        <div style={{ padding: 12, borderTop: "1px solid var(--border)", fontSize: 11.5, color: "var(--text-faint)" }}>
          {user ? (
            <>
              <div style={{ color: "var(--text-dim)", fontSize: 12 }}>
                {user.username} · <span style={{ textTransform: "uppercase" }}>{user.role}</span>
              </div>
              <button
                className="sm ghost"
                style={{ border: "none", padding: "4px 0", marginTop: 4 }}
                onClick={() => navigate("/change-password")}
              >
                Change password
              </button>
            </>
          ) : null}
        </div>
      </aside>

      <div className="main">
        <TradingModeStripe />
        <header className="topbar">
          <strong style={{ fontSize: 15 }}>{active?.label ?? "Trading Control Center"}</strong>
          <TradingModeBadge />
          <MarketTicker />
          <div className="spacer" />
          <RealtimeIndicator />
          <ConnIndicator />
          <AlertsBell />
          {user && (
            <span className="conn" title={`role: ${user.role}`}>
              {user.username}
            </span>
          )}
          <button className="sm ghost" onClick={signOut}>
            Sign out
          </button>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
