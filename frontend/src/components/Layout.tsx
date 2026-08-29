import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { NAV_ROUTES } from "@/routes";
import { useAuth } from "@/auth/AuthContext";
import { useHealth } from "@/api/hooks";
import { TradingModeBadge, TradingModeStripe } from "./TradingModeBadge";

function ConnIndicator() {
  const { data, isError, isLoading } = useHealth();
  const ok = !isError && data?.status === "ok";
  const color = isLoading ? "var(--text-faint)" : ok ? "var(--pos)" : "var(--neg)";
  const text = isLoading ? "checking…" : isError ? "backend unreachable" : `backend ${data?.status ?? "?"} · db ${data?.database ?? "?"}`;
  return (
    <span className="conn">
      <span className="badge-dot" style={{ width: 8, height: 8, borderRadius: "50%", background: color, display: "inline-block" }} />
      {text}
    </span>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { signOut, baseUrl } = useAuth();
  const location = useLocation();
  const active = NAV_ROUTES.find((r) => r.path === location.pathname);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">Trading Control Center</div>
        <nav>
          {NAV_ROUTES.map((r) => (
            <NavLink key={r.path} to={r.path} end={r.path === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="dot" />
              {r.label}
            </NavLink>
          ))}
        </nav>
        <div style={{ flex: 1 }} />
        <div style={{ padding: 12, borderTop: "1px solid var(--border)", fontSize: 11.5, color: "var(--text-faint)" }}>
          API: {baseUrl || "same-origin /api"}
        </div>
      </aside>

      <div className="main">
        <TradingModeStripe />
        <header className="topbar">
          <strong style={{ fontSize: 15 }}>{active?.label ?? "Trading Control Center"}</strong>
          <TradingModeBadge />
          <div className="spacer" />
          <ConnIndicator />
          <button className="sm ghost" onClick={signOut}>
            Sign out
          </button>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
