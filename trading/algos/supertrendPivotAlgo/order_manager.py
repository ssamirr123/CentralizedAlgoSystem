"""
order_manager.py
Place and exit option orders, track current open position, and enforce risk
controls such as max trades per day and single-position rule.
"""

import config
from logger import get_logger

log = get_logger()


class Position:
    """Represents a single open SELL option position."""

    def __init__(self, contract, entry_price, qty, side="SELL"):
        self.contract = contract
        self.option_type = contract["option_type"]  # "CE" or "PE"
        self.symbol = contract["symbol"]
        self.token = contract["token"]
        self.strike = contract["strike"]
        self.qty = qty
        self.entry_price = entry_price
        self.exit_price = None
        self.side = side

    def pnl(self):
        """Realized P&L for a short option (sell high, buy low)."""
        if self.exit_price is None:
            return 0.0
        # Sold at entry_price, bought back at exit_price.
        return (self.entry_price - self.exit_price) * self.qty


class OrderManager:
    def __init__(self, smart):
        self.smart = smart
        self.cfg = config.get_index_config()
        self.current_position = None
        self.trades_today = 0
        self.realized_pnl = 0.0
        self.trade_log = []
        # Optional heartbeat agent for the Central Strategy Monitoring System.
        self.agent = None

    # ------------------------------------------------------- monitoring hooks
    def attach_agent(self, agent):
        """Attach the dashboard monitor (monitor.py) to report metrics after trades."""
        self.agent = agent

    def mtm(self):
        """Current mark-to-market: realized P&L + unrealized on any open short."""
        unrealized = 0.0
        pos = self.current_position
        if pos is not None:
            ltp = self._get_ltp(pos.contract)
            if ltp:
                # Short option: profit when the buy-back price drops below entry.
                unrealized = (pos.entry_price - ltp) * pos.qty
        return self.realized_pnl + unrealized

    def report_metrics(self, status="RUNNING"):
        """Push the latest metrics to the monitor. Never blocks/raises."""
        if self.agent is None:
            return
        try:
            self.agent.update_metrics(
                self.mtm(), self.realized_pnl, self.trades_today, status
            )
        except Exception as exc:  # noqa: BLE001 - monitoring must never break trading
            log.debug("Metric report failed: %s", exc)

    # ---------------------------------------------------------------- helpers
    def can_open_new_position(self):
        """Risk gate: respect single-position rule and daily trade cap."""
        if self.current_position is not None and not config.ALLOW_SIMULTANEOUS_POSITIONS:
            return False, "A position is already open"
        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, (
                f"Max trades per day reached "
                f"({self.trades_today}/{config.MAX_TRADES_PER_DAY})"
            )
        return True, "OK"

    def _get_ltp(self, contract):
        try:
            resp = self.smart.ltpData(
                self.cfg["exchange"], contract["symbol"], contract["token"]
            )
            return float(resp["data"]["ltp"])
        except Exception as exc:  # noqa: BLE001
            log.error("LTP fetch failed for %s: %s", contract["symbol"], exc)
            return 0.0

    def _place_order(self, contract, transaction_type):
        qty = config.QUANTITY_LOTS * contract.get("lotsize", self.cfg["lot_size"])
        params = {
            "variety": config.ORDER_VARIETY,
            "tradingsymbol": contract["symbol"],
            "symboltoken": contract["token"],
            "transactiontype": transaction_type,   # BUY / SELL
            "exchange": self.cfg["exchange"],
            "ordertype": config.ORDER_TYPE,
            "producttype": config.PRODUCT_TYPE,
            "duration": "DAY",
            "quantity": qty,
        }

        if config.DRY_RUN:
            ltp = self._get_ltp(contract)
            log.info("[DRY_RUN] %s %s qty=%s @ ~%.2f",
                     transaction_type, contract["symbol"], qty, ltp)
            return {"order_id": "DRYRUN", "price": ltp, "qty": qty}

        try:
            order_id = self.smart.placeOrder(params)
            ltp = self._get_ltp(contract)
            log.info("Order placed: %s %s qty=%s id=%s",
                     transaction_type, contract["symbol"], qty, order_id)
            return {"order_id": order_id, "price": ltp, "qty": qty}
        except Exception as exc:  # noqa: BLE001
            log.error("Order placement failed: %s", exc)
            raise

    # ------------------------------------------------------------------ entry
    def sell_option(self, contract):
        """Open a short option position (sell to open)."""
        ok, reason = self.can_open_new_position()
        if not ok:
            log.warning("Entry blocked: %s", reason)
            return None

        result = self._place_order(contract, "SELL")
        pos = Position(contract, result["price"], result["qty"], side="SELL")
        self.current_position = pos
        self.trades_today += 1
        log.info(
            "ENTRY  Sold %s @ %.2f  (trade %d/%d)",
            pos.symbol, pos.entry_price, self.trades_today,
            config.MAX_TRADES_PER_DAY,
        )
        self.trade_log.append(
            {"event": "ENTRY", "symbol": pos.symbol, "price": pos.entry_price}
        )
        # Report metrics immediately after the trade executes.
        self.report_metrics(status="RUNNING")
        return pos

    # ------------------------------------------------------------------- exit
    def exit_position(self, reason="EXIT"):
        """Buy back the open short option position (buy to close)."""
        if self.current_position is None:
            return None

        pos = self.current_position
        result = self._place_order(pos.contract, "BUY")
        pos.exit_price = result["price"]
        pnl = pos.pnl()
        self.realized_pnl += pnl

        log.info(
            "EXIT   Bought back %s @ %.2f  | %s | P&L=%.2f | Day P&L=%.2f",
            pos.symbol, pos.exit_price, reason, pnl, self.realized_pnl,
        )
        self.trade_log.append(
            {"event": "EXIT", "symbol": pos.symbol,
             "price": pos.exit_price, "pnl": pnl, "reason": reason}
        )
        self.current_position = None
        # Report metrics immediately after the trade executes.
        self.report_metrics(status="RUNNING")
        return pnl

    def square_off_all(self):
        """Force-close any open position (used at end of day)."""
        if self.current_position is not None:
            log.info("Square-off triggered.")
            return self.exit_position(reason="SQUARE_OFF_EOD")
        log.info("Square-off: no open positions.")
        return None

