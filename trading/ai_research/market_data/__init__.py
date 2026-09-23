"""
Phase 8 -- bridges the AI Research Engine (Phase 7's canonical Instrument)
to the EXISTING Stage 19 market-data platform (trading/market_data/):
ICICIBreezeProvider, the MarketCandle table, and provider-agnostic
symbols/error taxonomy. Nothing here reimplements the Breeze client,
normalization, or persistence -- see service.py's own docstring for
exactly what is reused vs. new.

    ICICI Breeze
         |
    ICICIBreezeProvider (trading.market_data.providers.icici_breeze, REUSED)
         |
    Normalization (Candle, REUSED)
         |
    market_candles table (trading.database.models.MarketCandle, REUSED)
         |
    HistoricalMarketDataService (THIS package -- NEW: DB-first read,
                                  bounded backfill, point-in-time cutoff,
                                  provenance)
         |
    AI Research Engine / AI Backtesting

TradingAgents never calls Breeze directly and is never handed Breeze
credentials -- see trading_agents_adapter.py's own EXECUTION ISOLATION
docstring section, unchanged by this phase.
"""
