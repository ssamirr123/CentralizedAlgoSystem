const UNDERLYINGS = ["NIFTY", "SENSEX"] as const;

export function UnderlyingToggle({ value, onChange }: { value: string; onChange: (u: string) => void }) {
  return (
    <div className="toolbar" style={{ display: "inline-flex", gap: 4, padding: 2 }}>
      {UNDERLYINGS.map((u) => (
        <button
          key={u}
          className={`sm ${value === u ? "active" : ""}`}
          onClick={() => onChange(u)}
        >
          {u}
        </button>
      ))}
    </div>
  );
}
