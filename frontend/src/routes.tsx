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
import { TccOverviewPage } from "@/pages/TccOverviewPage";
import { TccStrategiesPage } from "@/pages/TccStrategiesPage";
import { TccAccountsPage } from "@/pages/TccAccountsPage";
import { TccBrokersPage } from "@/pages/TccBrokersPage";
import { TccAssignmentsPage } from "@/pages/TccAssignmentsPage";
import { TccOrdersPage } from "@/pages/TccOrdersPage";
import { TccPositionsPage } from "@/pages/TccPositionsPage";
import { TccPnlPage } from "@/pages/TccPnlPage";
import { TccRiskPage } from "@/pages/TccRiskPage";
import { TccLogsPage } from "@/pages/TccLogsPage";
import { TccSystemHealthPage } from "@/pages/TccSystemHealthPage";

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
  { path: "/market", label: "Market", element: MarketPage, permission: "VIEW" },
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

  // --- Trading Control Center: broker-agnostic execution framework (Phase 12) ---
  // Distinct nav section from the legacy telemetry pages above (see
  // docs/phase-12-react-control-center-report.md's naming-collision note).
  { path: "/execution", label: "Execution Overview", element: TccOverviewPage, permission: "VIEW" },
  { path: "/execution/strategies", label: "Execution Strategies", element: TccStrategiesPage, permission: "VIEW" },
  { path: "/execution/accounts", label: "Trading Accounts", element: TccAccountsPage, permission: "VIEW" },
  { path: "/execution/brokers", label: "Brokers", element: TccBrokersPage, permission: "VIEW" },
  { path: "/execution/assignments", label: "Strategy Assignments", element: TccAssignmentsPage, permission: "VIEW" },
  { path: "/execution/orders", label: "Orders", element: TccOrdersPage, permission: "VIEW" },
  { path: "/execution/positions", label: "Execution Positions", element: TccPositionsPage, permission: "VIEW" },
  { path: "/execution/pnl", label: "Execution P&L", element: TccPnlPage, permission: "VIEW" },
  { path: "/execution/risk", label: "Execution Risk", element: TccRiskPage, permission: "VIEW" },
  { path: "/execution/logs", label: "Execution Logs", element: TccLogsPage, permission: "ADMIN" },
  { path: "/execution/system-health", label: "Execution Health", element: TccSystemHealthPage, permission: "VIEW" },
];
