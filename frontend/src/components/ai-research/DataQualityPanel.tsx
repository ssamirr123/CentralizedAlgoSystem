import { useDataQuality } from "@/api/aiResearchBacktestHooks";
import { Loading } from "@/components/States";

const SAFE_LABEL: Record<string, string> = {
  YES: "Point-in-time safe",
  NO: "Not point-in-time safe",
  PARTIAL: "Partially safe (documented gap)",
  WITHHELD: "Withheld for historical dates (safe by refusal)",
};

const SAFE_CLASS: Record<string, string> = {
  YES: "running",
  NO: "error",
  PARTIAL: "stale",
  WITHHELD: "stopped",
};

/** Section 28: bias/data-quality disclosure. Always shown alongside
 * backtest results -- never hidden behind aggregate metrics. */
export function DataQualityPanel() {
  const quality = useDataQuality();

  if (quality.isLoading) return <Loading label="Loading data-quality matrix…" />;
  if (quality.isError || !quality.data) return null;

  return (
    <div className="card" style={{ borderColor: "var(--warn, #b8860b)" }}>
      <h3>Historical Research Quality</h3>
      <p className="sub">{quality.data.summary}</p>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Source</th>
              <th>Point-in-time safety</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {quality.data.sources.map((s) => (
              <tr key={s.source}>
                <td>{s.source.replace(/_/g, " ")}</td>
                <td>
                  <span className={`badge ${SAFE_CLASS[s.safe] ?? "unknown"}`}>
                    <span className="dot" />
                    {SAFE_LABEL[s.safe] ?? s.safe}
                  </span>
                </td>
                <td style={{ maxWidth: 480 }}>{s.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
