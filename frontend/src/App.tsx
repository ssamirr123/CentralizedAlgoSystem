import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "@/auth/AuthContext";
import { Layout } from "@/components/Layout";
import { LoginPage } from "@/pages/LoginPage";
import { ChangePasswordPage } from "@/pages/ChangePasswordPage";
import { NAV_ROUTES } from "@/routes";
import type { Permission } from "@/lib/config";

function FullScreen({ children }: { children: ReactNode }) {
  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", color: "var(--text-dim)" }}>
      {children}
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, initializing, user } = useAuth();
  const location = useLocation();
  if (initializing) return <FullScreen><span className="spinner" /></FullScreen>;
  if (!isAuthenticated) return <Navigate to="/login" state={{ from: location }} replace />;
  if (user?.must_change_password && location.pathname !== "/change-password") {
    return <Navigate to="/change-password" replace />;
  }
  return <>{children}</>;
}

function PermissionRoute({ permission, children }: { permission: Permission; children: ReactNode }) {
  const { hasPermission } = useAuth();
  if (!hasPermission(permission)) {
    return (
      <div className="state error">
        You don’t have permission to view this page ({permission} required).
      </div>
    );
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/*"
        element={
          <RequireAuth>
            <Layout>
              <Routes>
                <Route path="/change-password" element={<ChangePasswordPage />} />
                {NAV_ROUTES.map(({ path, element: El, permission }) => (
                  <Route
                    key={path}
                    path={path === "/" ? "/" : path.slice(1)}
                    element={
                      <PermissionRoute permission={permission}>
                        <El />
                      </PermissionRoute>
                    }
                  />
                ))}
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Layout>
          </RequireAuth>
        }
      />
    </Routes>
  );
}
