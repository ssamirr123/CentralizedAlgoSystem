import type { ComponentType } from "react";
import { DashboardPage } from "@/pages/DashboardPage";
import { ServersPage } from "@/pages/ServersPage";
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

export interface NavRoute {
  path: string;
  label: string;
  element: ComponentType;
}

export const NAV_ROUTES: NavRoute[] = [
  { path: "/", label: "Dashboard", element: DashboardPage },
  { path: "/servers", label: "Servers", element: ServersPage },
  { path: "/strategies", label: "Strategies", element: StrategiesPage },
  { path: "/algo-status", label: "Algo Status", element: AlgoStatusPage },
  { path: "/heartbeats", label: "Heartbeats", element: HeartbeatsPage },
  { path: "/pnl", label: "P&L", element: PnlPage },
  { path: "/positions", label: "Positions", element: PositionsPage },
  { path: "/trades", label: "Trades", element: TradesPage },
  { path: "/commands", label: "Commands", element: CommandsPage },
  { path: "/logs", label: "Logs", element: LogsPage },
  { path: "/risk", label: "Risk", element: RiskPage },
  { path: "/system-health", label: "System Health", element: SystemHealthPage },
];
