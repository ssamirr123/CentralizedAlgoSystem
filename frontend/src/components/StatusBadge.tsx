export function StatusBadge({ status }: { status: string | null | undefined }) {
  const s = (status ?? "UNKNOWN").toUpperCase();
  const cls =
    s === "RUNNING"
      ? "running"
      : s === "STOPPED"
        ? "stopped"
        : s === "ERROR"
          ? "error"
          : "unknown";
  return (
    <span className={`badge ${cls}`}>
      <span className="dot" />
      {s}
    </span>
  );
}

export function StaleBadge({ stale }: { stale: boolean }) {
  return stale ? (
    <span className="badge stale">
      <span className="dot" />
      STALE
    </span>
  ) : (
    <span className="badge running">
      <span className="dot" />
      FRESH
    </span>
  );
}
