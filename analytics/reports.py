from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "tracker.db"


def build_price_report(limit: int = 100) -> dict[str, dict[str, float | int]]:
    """Aggregate latest prices by symbol from tracker.db."""
    if not DB_PATH.exists():
        return {}

    stats: dict[str, list[float]] = defaultdict(list)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, price
            FROM market_ticks
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    for symbol, price in rows:
        stats[symbol].append(float(price))

    report: dict[str, dict[str, float | int]] = {}
    for symbol, prices in stats.items():
        report[symbol] = {
            "samples": len(prices),
            "latest": prices[0],
            "average": round(sum(prices) / len(prices), 4),
            "high": max(prices),
            "low": min(prices),
        }
    return report


if __name__ == "__main__":
    print(build_price_report())

