import { Link } from "react-router-dom";
import { useMarketHealth, useMarketIndices, useMarketSessionStatus } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { IndexCard } from "@/components/IndexCard";
import { MarketFeedStatus } from "@/components/MarketFeedStatus";
import { MarketChart } from "@/components/MarketChart";

const ORDER = ["NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX"];

export function MarketPage() {
  const indices = useMarketIndices();
  const health = useMarketHealth();
  const session = useMarketSessionStatus();
  const provider = (health.data?.provider ?? session.data?.provider ?? "zerodha").toLowerCase();
  const providerLabel = provider.includes("zerodha") || provider.includes("kite") ? "Zerodha Kite" : "ICICI Breeze";

  return (
    <>
      <PageHeader
        title="Market"
        description={`Live index quotes from ${providerLabel} (paper — market data only, no orders).`}
        actions={
          <>
            <Link className="sm" to="/market/straddle-pulse">
              Straddle Pulse
            </Link>
            <button className="sm" onClick={() => indices.refetch()}>
              Refresh
            </button>
          </>
        }
      />

      <QueryBoundary query={indices} empty={(d) => d.length === 0}>
        {(list) => {
          const byId = Object.fromEntries(list.map((q) => [q.symbol, q]));
          return (
            <div className="entity-grid" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(240px,1fr))" }}>
              {ORDER.filter((s) => byId[s]).map((s) => (
                <IndexCard key={s} q={byId[s]} />
              ))}
            </div>
          );
        }}
      </QueryBoundary>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <MarketFeedStatus />
        <MarketChart />
      </div>
    </>
  );
}
