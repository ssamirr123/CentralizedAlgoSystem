// Section 17 (mandatory): shown near every Trader/Risk/Portfolio Manager
// output. This component intentionally contains NO action button of any
// kind -- see tests/AiResearch.test.tsx's "no execution controls" test and
// the Section 32 codebase-wide search this satisfies.
export function ResearchOnlyWarning() {
  return (
    <div className="card" style={{ borderColor: "var(--warn, #b58900)", background: "var(--warn-bg, rgba(181,137,0,0.08))" }}>
      <strong>AI Research Only</strong>
      <p style={{ margin: "4px 0 0" }}>
        This analysis does not place, modify, or cancel orders and is not connected to broker execution.
      </p>
    </div>
  );
}
