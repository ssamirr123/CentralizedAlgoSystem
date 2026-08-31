const STATUS_CLASS: Record<string, string> = {
  RUNNING: "running",
  SUCCESS: "running",
  UPDATED: "running",
  STOPPED: "stopped",
  PENDING: "stopped",
  STARTING: "stopped",
  STOPPING: "stopped",
  RESTARTING: "stopped",
  REBOOTING: "stopped",
  ERROR: "error",
  FAILED: "error",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const s = (status ?? "UNKNOWN").toUpperCase();
  return (
    <span className={`badge ${STATUS_CLASS[s] ?? "unknown"}`}>
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
