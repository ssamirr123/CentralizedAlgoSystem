import { useEffect, useMemo, useState } from "react";
import { useCycleSessions, useSessionChart, useSessionOI, useStraddleCycles } from "@/api/hooks";
import type { StraddleCycle, StraddleSession } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { CycleHeader } from "@/components/straddle-pulse/CycleHeader";
import { CycleSelector } from "@/components/straddle-pulse/CycleSelector";
import { parseApiTimestampMs } from "@/components/straddle-pulse/format";
import { DailyStraddleChart } from "@/components/straddle-pulse/chart/DailyStraddleChart";
import { OIBuildupChart } from "@/components/straddle-pulse/chart/OIBuildupChart";
import { WholeCycleChart } from "@/components/straddle-pulse/chart/WholeCycleChart";
import { SessionTabs, type SessionSelection } from "@/components/straddle-pulse/SessionTabs";
import { StatTiles } from "@/components/straddle-pulse/StatTiles";
import { WholeCycleTable } from "@/components/straddle-pulse/WholeCycleTable";

function DailyView({ sessionId }: { sessionId: number }) {
  const chart = useSessionChart(sessionId);
  const oi = useSessionOI(sessionId);
  const [xDomain, setXDomain] = useState<[number, number] | null>(null);
  const [hoverT, setHoverT] = useState<number | null>(null);

  const fallbackDomain = useMemo<[number, number]>(() => {
    const spot = chart.data?.spot ?? [];
    if (spot.length === 0) return [Date.now() - 1, Date.now()];
    return [parseApiTimestampMs(spot[0].timestamp), parseApiTimestampMs(spot[spot.length - 1].timestamp)];
  }, [chart.data]);

  useEffect(() => {
    setXDomain(fallbackDomain);
  }, [fallbackDomain[0], fallbackDomain[1]]);

  if (!chart.data || !oi.data || !xDomain) {
    return <div className="state">{chart.isLoading ? "Loading…" : "No data yet for this session."}</div>;
  }

  const shared = { xDomain, onXDomainChange: setXDomain, hoverT, onHoverChange: setHoverT };

  return (
    <>
      <DailyStraddleChart chart={chart.data} shared={shared} />
      <div style={{ marginTop: 12 }}>
        <OIBuildupChart oi={oi.data} shared={shared} />
      </div>
    </>
  );
}

export function StraddlePulsePage() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [selection, setSelection] = useState<SessionSelection>("whole-cycle");
  const [selectedCycleId, setSelectedCycleId] = useState<number | null>(null);

  const cycles = useStraddleCycles(underlying);
  const cycleList = cycles.data ?? [];
  const activeCycle = cycleList.find((c) => c.status === "ACTIVE") ?? cycleList[0];
  const selectedCycle = cycleList.find((c) => c.id === selectedCycleId) ?? activeCycle;
  const sessions = useCycleSessions(selectedCycle?.id ?? null);

  useEffect(() => {
    // switching underlying (or first load) resets to that underlying's own
    // active cycle -- never carries a stale cycle id across underlyings.
    setSelection("whole-cycle");
    setSelectedCycleId(null);
  }, [underlying]);

  const sessionList = sessions.data ?? [];
  const selectedSession = selection !== "whole-cycle" ? sessionList.find((s) => s.id === selection) : undefined;

  return (
    <>
      <PageHeader title="Straddle Pulse" description="NIFTY + SENSEX expiry-cycle ATM straddle, spot overlay, and intraday OI/PCR." />

      <QueryBoundary query={cycles} empty={(d) => d.length === 0}>
        {() => (
          <>
            <CycleHeader
              underlying={underlying}
              onUnderlyingChange={setUnderlying}
              cycle={selectedCycle}
              sessionCount={sessionList.length}
            />

            {cycleList.length > 0 && (
              <div className="toolbar" style={{ marginTop: -6 }}>
                <CycleSelector
                  cycles={cycleList}
                  selectedId={selectedCycle?.id ?? null}
                  onSelect={(id) => {
                    setSelectedCycleId(id);
                    setSelection("whole-cycle");
                  }}
                />
              </div>
            )}

            {selectedCycle && (
              <QueryBoundary query={sessions} empty={(d) => d.length === 0}>
                {(rows) => (
                  <>
                    <SessionTabs sessions={rows} selected={selection} onSelect={setSelection} />

                    {selection === "whole-cycle" ? (
                      <>
                        <WholeCycleChart sessions={rows} />
                        <div style={{ marginTop: 12 }}>
                          <WholeCycleTable sessions={rows} />
                        </div>
                      </>
                    ) : selectedSession ? (
                      <StatTilesSection sessionId={selectedSession.id} cycle={selectedCycle} session={selectedSession} />
                    ) : null}
                  </>
                )}
              </QueryBoundary>
            )}
          </>
        )}
      </QueryBoundary>
    </>
  );
}

function StatTilesSection({
  sessionId, cycle, session,
}: { sessionId: number; cycle: StraddleCycle; session: StraddleSession }) {
  const chart = useSessionChart(sessionId);
  const oi = useSessionOI(sessionId);
  return (
    <>
      <StatTiles session={session} cycle={cycle} chart={chart.data} oi={oi.data} />
      <DailyView sessionId={sessionId} />
    </>
  );
}
