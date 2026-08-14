-- Database schema for storing trades (deliverable 14).
-- Compatible with SQLite and PostgreSQL (drop AUTOINCREMENT for PG -> SERIAL).

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      DATE NOT NULL,
    is_expiry_day   BOOLEAN NOT NULL,
    hedge_gap       INTEGER NOT NULL,
    day_mtm         REAL,
    day_pnl         REAL,
    kill_switch     BOOLEAN DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    trade_date      DATE NOT NULL,
    leg_type        TEXT CHECK(leg_type IN ('HEDGE','SHORT')) NOT NULL,
    strategy        TEXT CHECK(strategy IN ('HEDGE','MORNING','AFTERNOON')) NOT NULL,
    symbol          TEXT NOT NULL,
    strike          INTEGER NOT NULL,
    option_type     TEXT CHECK(option_type IN ('CE','PE')) NOT NULL,
    side            TEXT CHECK(side IN ('BUY','SELL')) NOT NULL,
    quantity        INTEGER NOT NULL,
    entry_order_id  TEXT,
    entry_time      TIMESTAMP,
    entry_price     REAL,
    exit_order_id   TEXT,
    exit_time       TIMESTAMP,
    exit_price      REAL,
    exit_reason     TEXT CHECK(exit_reason IN ('SL','Target','Time Exit','Emergency Exit','Day Close')),
    pnl_points      REAL,
    pnl_amount      REAL,
    brokerage       REAL DEFAULT 0,
    charges         REAL DEFAULT 0,
    net_pnl         REAL
);

CREATE TABLE IF NOT EXISTS orders_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      DATE NOT NULL,
    order_id        TEXT,
    symbol          TEXT,
    action          TEXT,   -- PLACE / MODIFY / CANCEL / RETRY / PARTIAL / REJECT
    order_type      TEXT,   -- LIMIT / MARKET
    price           REAL,
    quantity        INTEGER,
    filled_qty      INTEGER,
    status          TEXT,
    reason          TEXT,
    attempt         INTEGER,
    ts              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders_audit(trade_date);

