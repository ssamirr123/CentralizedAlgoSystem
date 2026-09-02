import { useMarketIndices } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { IndexCard } from "@/components/IndexCard";
import { MarketFeedStatus } from "@/components/MarketFeedStatus";
import { MarketChart } from "@/components/MarketChart";
import { NiftyOptionChain } from "@/components/NiftyOptionChain";

const ORDER = ["NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX"];

export function MarketPage() {
  const indices = useMarketIndices();

  return (
    <>
      <PageHeader
        title="Market"
        description="Live index quotes + NIFTY option chain from ICICI Breeze (paper — market data only, no orders)."
        actions={
          <button className="sm" onClick={() => indices.refetch()}>
            Refresh
          </button>
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

      <div style={{ marginTop: 16 }}>
        <NiftyOptionChain />
      </div>
    </>
  );
}
