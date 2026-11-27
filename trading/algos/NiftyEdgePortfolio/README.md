# NiftyEdgePortfolio

One process that runs the four NIFTY option-selling strategies picked from the
Nov 2026 backtests (Trading Brain → *Algo Portfolio* / *Best Algo Cases*), each as an
independent plug-in with its own on/off switch, lots, order tag, state and P&L.

**Paper by default.** Real orders only with `NEP_LIVE=true` (env or `trading/.env`). `BOT_DRY_RUN` is ignored on purpose, so another algo going live can't switch this one live.

| Plug-in | Rules (exactly as backtested) | Lots |
|---|---|---|
| `strangle60` | Expiry day. 09:19 close: CE ≥ ATM and PE ≤ ATM nearest ₹60 (skip if > ₹18 away). 09:20 close: sell both. Exit a leg when a 1-min close ≥ 2× entry. Exit 15:15. | 2 |
| `otm4_1day` | DTE1. 09:20 close: sell CE ATM+200 and PE ATM−200. SL: 1-min close ≥ 1.5× entry (overnight too). Exit expiry day 15:15. | 1 |
| `btst9` | Daily. 09:20 close: per side, strike nearest ₹100 (next weekly on expiry day). Sell on the candle after a close ≤ 50% of that. SL 1.5× on closes. Exit next session 09:20. | 2 |
| `st10` | 10-min Supertrend(10,3) on spot. On a flip, close the short and sell the ATM option against the trend (PE in an up-trend, CE in a down-trend), next weekly on expiry day; roll at 09:20 on expiry day. Naked, held overnight. | 1 |
| `stbt` | Daily "Sell Today Buy Tomorrow". Range = spot 09:15–11:14. First 1-min close beyond it (11:15–15:15): UP → sell PE ATM+200, DOWN → sell CE ATM−200 (next weekly on expiry day). SL: spot 0.5% against, on 1-min high/low, overnight included. Exit next session 09:35. Added 26 Nov 2026 — paper first. | 1 |

## Layout
```
main.py            startup: log tee, PID file, trading-day check, scrip master, login, websocket, engine
config.py          DRY_RUN, per-strategy enabled/lots/tags, strategy parameters, risk limits
engine.py          0.5 s loop: on_minute() per closed candle per strategy (isolated), intrabar checks, reconcile
context.py         the interface strategies use (prices, open_leg/close_leg, state); Leg dataclass; charges model
live_context.py    Angel One implementation of context.py (paper fills in DRY_RUN)
instruments.py     scrip master -> (expiry, strike, CE/PE) -> symbol/token; expiries; trading days; DTE
feed.py            websocket LTP cache with resubscribe-on-reconnect; batched REST quotes
broker/orders.py   LIMIT orders that walk toward the market, MARKET remainder for exits, ordertag,
                   CARRYFORWARD, resting STOPLOSS_LIMIT, rate-limit throttle (from DoubleStraddelAlgo)
risk/guard.py      daily loss limit, portfolio drawdown stop + HALT file, margin check,
                   disaster stops, broker-SL fill detection, manual-trade guard
state/store.py     multi-day JSON state (overnight legs + indicator state survive restarts)
state/journal.py   closed trades with charges -> runtime/journal.csv; equity peak for the drawdown stop
strategies/        strangle60.py  otm4_1day.py  btst9.py  st10.py  stbt.py  (+ base.py)
replay/            replay.py (runs the live strategy code on your 1-min CSVs) + compare_backtest.py
```

## Timing convention
Minute `m` = the candle that has just closed (0 = 09:15 … 374 = 15:29). Every rule
acts on candle **closes**, about 2 s after the candle closes - the same prices the
backtests used. Stop checks are on 1-min closes (not ticks). Ticks are used only for
the optional *disaster* stop (3× for the strangle, 2.25× for OTM4/BTST 9) and the
resting broker SL at the same level, which protects a short if this process dies.
An exit signalled on the 15:29 close is executed at the next session's open.

## Validation: replay vs backtest (all of 2021 → 6 Oct 2026)
`python -m replay.replay --data "<data folder>" --from 2021-01-01 --to 2026-10-06 --full-history --strategies strangle60,otm4_1day,btst9,st10`
then `python -m replay.compare_backtest --replay replay/replay_trades.csv --research "<…>/NIFTY_DTE_EDGE_RESEARCH/results"`.

| Strategy | Exact trade matches |
|---|---|
| strangle60 | 346 / 346 (100%) |
| otm4_1day | 581 / 591 (98.3%) |
| btst9 | 742 / 767 (96.7%) |
| st10 | 1,713 / 1,736 (98.7%) |
| stbt | 69 / 69 trades Jan–Apr 2024 (same strikes and entry times; SL exits 1 min later in replay) |

Every difference traced so far is a limitation of the research panels, where the live
code follows the rule correctly: strikes outside the panel's ±500 window, the next
weekly missing around special Saturday sessions (e.g. 2024-02-29, 2025-01-30), days
missing from the data (e.g. 2021-12-01), and float32 rounding when a close equals the
stop exactly.

## Holiday guard
On top of the NSE calendar check at startup (`trading.common.market_calendar`, verified for 2025-2026 only),
the engine sends NO order - entry or exit - until the NIFTY index price has changed at least 3 times that day
(websocket ticks, or two different REST quotes if the websocket is down). On a holiday the index never moves,
so nothing can become an AMO even if the calendar is wrong (e.g. 2027 before NSE's list is added).

## Run
```bash
pip install -r requirements.txt
cd trading/algos/NiftyEdgePortfolio
python main.py                      # paper (DRY_RUN) - start before 09:15 each trading day
NEP_LIVE=true python main.py        # LIVE - only after the paper checklist below
```
Credentials come from the environment / `trading/.env` (`ANGELONE_CLIENT_ID`, `ANGELONE_API_KEY`,
`ANGELONE_MPIN`, `ANGELONE_TOTP_SECRET`); Telegram from `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

## Rollout
1. Paper: `strangle60` only (default) for 4-6 expiries. Compare paper fills with the 1-min closes.
2. Live `strangle60` 1 lot. Then enable `otm4_1day` + `btst9` (paper 2 weeks, then live).
3. Enable `st10` last (largest drawdown; flatten it before Budget / RBI / election results).
4. Turn `MANUAL_GUARD["auto_exit"]` on only when no other algo trades NIFTY options on this
   Angel One account (it would treat their positions as manual trades).
5. Switch off `Vwap_Algo_Nifty_hedge` / `DoubleStraddelAlgo` on the same capital first.

## Before live - verify
- `LOT_SIZE` (65) and freeze quantity against the current NSE circular.
- Margin per lot in `STRATEGIES[...]["margin_per_lot"]` with Angel One's margin calculator.
- One DRY_RUN day end to end: entries at 09:20:0x, exits at 15:15:0x, `runtime/journal.csv` rows,
  Telegram alerts, restart mid-day resumes open legs without duplicate orders.
- Risk: `DAILY_LOSS_LIMIT` ₹25k (blocks new entries), `PORTFOLIO_DD_STOP` ₹1.4L (flatten + `runtime/HALTED`;
  delete the file by hand to resume).

**Sizing (P30 optimisation, 26 Nov 2026):** strangle60 ×2, otm4_1day ×1, btst9 ×2, st10 ×1, stbt ×1 — backtest ₹23.1L vs ₹19.2L at 1 lot each, max DD −₹86.1k, peak margin ~₹10L on Tuesday (verify with the broker).

## Market data (WebSocket first)
- **NIFTY 1-min bars** are built from WebSocket index ticks (`feed.index_bars`); N-min bars are aggregated from them. Saved every minute to `runtime/bars/YYYY-MM-DD.json` and reloaded after a restart.
- **ST10 history** (20 days) is read from those saved bar files; `getCandleData` is used only for days not saved yet (first weeks after deployment) and to backfill minutes missed while the algo was down (at most every 20 s).
- **Option prices**: CE/PE strikes ATM ±2000 of the nearest 2 expiries are pre-subscribed at start-up and at the open (re-centred if NIFTY moves 500+), so chain reads come from the WebSocket; REST quotes only fill gaps.
- Orders, order book, positions and margin are REST by nature.
