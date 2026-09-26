import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { NAV_ROUTES, type NavRoute } from "@/routes";
import { useAuth } from "@/auth/AuthContext";
import { useHealth } from "@/api/hooks";
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

function NavGroup({ route }: { route: NavRoute }) {
  const location = useLocation();
  const { hasPermission } = useAuth();
  const children = route.children!.filter((c) => hasPermission(c.permission ?? route.permission));
  const inGroup =
    location.pathname === route.path ||
    location.pathname.startsWith(`${route.path}/`) ||
    children.some((c) => location.pathname === c.path || location.pathname.startsWith(`${c.path}/`));
  const [open, setOpen] = useState(inGroup);
  // Navigating into the group (e.g. via a link on the page) expands it.
  useEffect(() => {
    if (inGroup) setOpen(true);
  }, [inGroup]);

  return (
    <>
      <div className="nav-group">
        <NavLink to={route.path} end className={({ isActive }) => (isActive ? "active" : "")}>
          <span className="dot" />
          {route.label}
        </NavLink>
        <button
          type="button"
          className={`nav-toggle ${open ? "open" : ""}`}
          aria-expanded={open}
          aria-label={`${open ? "Collapse" : "Expand"} ${route.label}`}
          onClick={() => setOpen((o) => !o)}
        >
          ▸
        </button>
      </div>
      {open &&
        children.map((c) => (
          <NavLink key={c.path} to={c.path} className={({ isActive }) => `nav-child ${isActive ? "active" : ""}`}>
            <span className="dot" />
            {c.label}
          </NavLink>
        ))}
    </>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { user, signOut, hasPermission } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const visible = NAV_ROUTES.filter((r) => hasPermission(r.permission));
  const active =
    NAV_ROUTES.find((r) => r.path === location.pathname) ??
    NAV_ROUTES.flatMap((r) => r.children ?? []).find((c) => c.path === location.pathname);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">Trading Control Center</div>
        <nav>
          {visible.map((r) =>
            r.children?.length ? (
              <NavGroup key={r.path} route={r} />
            ) : (
              <NavLink key={r.path} to={r.path} end={r.path === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
                <span className="dot" />
                {r.label}
              </NavLink>
            ),
          )}
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
        <header className="topbar">
          <strong style={{ fontSize: 15 }}>{active?.label ?? "Trading Control Center"}</strong>
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
