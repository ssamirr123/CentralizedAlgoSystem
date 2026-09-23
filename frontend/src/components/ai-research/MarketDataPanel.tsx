import type { MarketDataProvenance } from "@/api/aiResearchTypes";

const STATUS_LABEL: Record<string, string> = {
  AVAILABLE: "Available",
  PARTIAL: "Partial coverage",
  MISSING: "Missing",
  STALE: "Stale",
  PROVIDER_ERROR: "Provider error",
  RATE_LIMITED: "Rate limited",
  UNSUPPORTED: "Unsupported",
};

const SOURCE_LABEL: Record<string, string> = {
  TIMESCALEDB: "Local database",
  ICICI_BREEZE: "ICICI Breeze (live fetch)",
  MIXED: "Local database + ICICI Breeze",
  NONE: "None",
};

const PROVIDER_LABEL: Record<string, string> = {
  icici_breeze: "ICICI Breeze",
};

/** Section 28/29: compact market-data provenance section for Indian
 * research results -- never claims Breeze was called when data actually
 * came from the local database. */
export function MarketDataPanel({ provenance }: { provenance: MarketDataProvenance }) {
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3>Market Data</h3>
      <dl className="kv">
        <dt>Source</dt>
        <dd>{SOURCE_LABEL[provenance.source] ?? provenance.source}</dd>
        <dt>Provider</dt>
        <dd>{PROVIDER_LABEL[provenance.provider] ?? provenance.provider}</dd>
        <dt>Interval</dt>
        <dd>{provenance.interval}</dd>
        <dt>Coverage</dt>
        <dd>{provenance.from} → {provenance.to}</dd>
        {provenance.cutoff && (
          <>
            <dt>Data Cutoff</dt>
            <dd>{new Date(provenance.cutoff).toLocaleString()}</dd>
          </>
        )}
        <dt>Status</dt>
        <dd>{STATUS_LABEL[provenance.status] ?? provenance.status}</dd>
        <dt>Candles Used</dt>
        <dd>{provenance.candle_count}</dd>
      </dl>
    </div>
  );
}
