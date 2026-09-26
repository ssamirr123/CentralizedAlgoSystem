import type { ComponentType } from "react";
import type { Permission } from "@/lib/config";
import { DashboardPage } from "@/pages/DashboardPage";
import { ServersPage } from "@/pages/ServersPage";
import { AlgorithmsPage } from "@/pages/AlgorithmsPage";
import { MarketPage } from "@/pages/MarketPage";
import { StrategiesPage } from "@/pages/StrategiesPage";
import { AlgoStatusPage } from "@/pages/AlgoStatusPage";
import { HeartbeatsPage } from "@/pages/HeartbeatsPage";
import { PnlPage } from "@/pages/PnlPage";
import { PositionsPage } from "@/pages/PositionsPage";
import { TradesPage } from "@/pages/TradesPage";
import { CommandsPage } from "@/pages/CommandsPage";
import { LogsPage } from "@/pages/LogsPage";
import { RiskPage } from "@/pages/RiskPage";
import { SystemHealthPage } from "@/pages/SystemHealthPage";
import { AdminPage } from "@/pages/AdminPage";
import { AiResearchPage } from "@/pages/AiResearchPage";

export interface NavChild {
  path: string;
  label: string;
  /** When set, the <Route> is generated from ROUTABLE; otherwise it is
   *  declared by hand in App.tsx. */
  element?: ComponentType;
  /** Defaults to the parent's permission. */
  permission?: Permission;
}

export interface NavRoute {
  path: string;
  label: string;
  element: ComponentType;
  /** Permission needed to see the nav entry and open the route. */
  permission: Permission;
  /** Sub-pages shown under this entry in an expandable sidebar group. */
  children?: NavChild[];
}

export const NAV_ROUTES: NavRoute[] = [
  { path: "/", label: "Dashboard", element: DashboardPage, permission: "VIEW" },
  {
    path: "/servers",
    label: "Servers",
    element: ServersPage,
    permission: "VIEW",
    children: [{ path: "/system-health", label: "System Health", element: SystemHealthPage }],
  },
  {
    path: "/algorithms",
    label: "Algorithms",
    element: AlgorithmsPage,
    permission: "VIEW",
    children: [
      { path: "/strategies", label: "Strategies", element: StrategiesPage },
      { path: "/algo-status", label: "Algo Status", element: AlgoStatusPage },
      { path: "/heartbeats", label: "Heartbeats", element: HeartbeatsPage },
      { path: "/pnl", label: "P&L", element: PnlPage },
      { path: "/positions", label: "Positions", element: PositionsPage },
      { path: "/trades", label: "Trades", element: TradesPage },
      { path: "/logs", label: "Logs", element: LogsPage },
      { path: "/risk", label: "Risk", element: RiskPage },
    ],
  },
  {
    path: "/market",
    label: "Market",
    element: MarketPage,
    permission: "VIEW",
    children: [{ path: "/market/straddle-pulse", label: "Straddle Pulse" }],
  },
  { path: "/commands", label: "Commands", element: CommandsPage, permission: "VIEW" },
  { path: "/ai-research", label: "AI Research Engine", element: AiResearchPage, permission: "VIEW" },
  { path: "/admin", label: "Administration", element: AdminPage, permission: "ADMIN" },
];

/** Every page that gets a generated <Route>: top-level entries plus any
 *  children that carry their own element. */
export const ROUTABLE: { path: string; element: ComponentType; permission: Permission }[] = NAV_ROUTES.flatMap((r) => [
  r,
  ...(r.children ?? []).flatMap((c) =>
    c.element ? [{ path: c.path, element: c.element, permission: c.permission ?? r.permission }] : [],
  ),
]);
