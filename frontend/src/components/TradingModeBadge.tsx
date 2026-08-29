import { TRADING_MODE } from "@/lib/config";

/** The always-visible PAPER / LIVE indicator. Never hidden. */
export function TradingModeBadge() {
  return (
    <span className={`mode-banner ${TRADING_MODE}`} title={`This build targets ${TRADING_MODE.toUpperCase()} trading`}>
      <span className="pulse" />
      {TRADING_MODE === "live" ? "Live Trading" : "Paper Trading"}
    </span>
  );
}

export function TradingModeStripe() {
  return <div className={`mode-stripe ${TRADING_MODE}`} aria-hidden />;
}
