import type { ComponentType } from "react";
import type { Permission } from "@/lib/config";
import { DashboardPage } from "@/pages/DashboardPage";
import { ServersPage } from "@/pages/ServersPage";
import { AlgorithmsPage } from "@/pages/AlgorithmsPage";
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

export interface NavRoute {
  path: string;
  label: string;
  element: ComponentType;
  /** Permission needed to see the nav entry and open the route. */
  permission: Permission;
}

export const NAV_ROUTES: NavRoute[] = [
  { path: "/", label: "Dashboard", element: DashboardPage, permission: "VIEW" },
  { path: "/servers", label: "Servers", element: ServersPage, permission: "VIEW" },
  { path: "/algorithms", label: "Algorithms", element: AlgorithmsPage, permission: "VIEW" },
  { path: "/strategies", label: "Strategies", element: StrategiesPage, permission: "VIEW" },
  { path: "/algo-status", label: "Algo Status", element: AlgoStatusPage, permission: "VIEW" },
  { path: "/heartbeats", label: "Heartbeats", element: HeartbeatsPage, permission: "VIEW" },
  { path: "/pnl", label: "P&L", element: PnlPage, permission: "VIEW" },
  { path: "/positions", label: "Positions", element: PositionsPage, permission: "VIEW" },
  { path: "/trades", label: "Trades", element: TradesPage, permission: "VIEW" },
  { path: "/commands", label: "Commands", element: CommandsPage, permission: "VIEW" },
  { path: "/logs", label: "Logs", element: LogsPage, permission: "VIEW" },
  { path: "/risk", label: "Risk", element: RiskPage, permission: "VIEW" },
  { path: "/system-health", label: "System Health", element: SystemHealthPage, permission: "VIEW" },
  { path: "/admin", label: "Administration", element: AdminPage, permission: "ADMIN" },
];
