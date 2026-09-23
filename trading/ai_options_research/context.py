"""
Phase 10 (Section 4/5/6) -- the immutable OptionsResearchContext.

Every agent in one research job reads the SAME frozen object -- a plain,
frozen dataclass built once from a single Phase 9
``MarketStructureSummary`` call, tagged with a ``research_snapshot_id``.
No agent (and no later stage of the same job) can accidentally observe a
different, more-recent option-chain snapshot mid-run.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from trading.market_data.options_intelligence import (
    DataQuality,
    MarketStructureSummary,
    OptionType,
)


@dataclass(frozen=True)
class ChainRowSnapshot:
    """A minimal, LLM-referenceable per-strike fact used by the
    hallucination guard (Section 61) to validate anything an agent claims
    about a specific strike/price."""

    strike: float
    option_type: OptionType
    ltp: float | None
    oi: float | None
    iv: float | None
    delta: float | None


@dataclass(frozen=True)
class OptionsResearchContext:
    research_snapshot_id: str
    underlying: str
    research_timestamp: datetime
    as_of: datetime | None  # None == live/current research

    spot: float | None
    spot_timestamp: datetime | None

    expiry: date | None
    dte_calendar: int | None
    dte_trading: int | None
    atm_strike: float | None

    # Phase 9 passthrough only -- never recomputed by this package or by an LLM.
    total_call_oi: int | None
    total_put_oi: int | None
    oi_pcr: float | None
    volume_pcr: float | None
    max_pain_strike: float | None
    oi_support: tuple[float, ...]
    oi_resistance: tuple[float, ...]
    atm_iv: float | None
    atm_iv_source: str
    iv_rank: float | None
    iv_percentile: float | None
    iv_rank_status: str
    expected_move: float | None
    expected_move_method: str

    chain: tuple[ChainRowSnapshot, ...]
    valid_strikes: frozenset[float]

    data_quality: DataQuality
    data_quality_reasons: tuple[str, ...]
    provenance: dict

    # Reserved extension points (Section 4) -- not populated by Phase 10;
    # left None/empty so a later phase can add them without a schema
    # change to this dataclass's shape.
    technical_indicators: dict | None = None
    news_sentiment: dict | None = None
    existing_trading_agents_research: str | None = None


def freeze_snapshot(summary: MarketStructureSummary) -> OptionsResearchContext:
    """The ONE place a live/historical Phase 9 summary becomes an
    immutable research context. Called exactly once per research job
    (Section 5) -- every agent downstream receives this same object."""
    chain = tuple(
        ChainRowSnapshot(
            strike=r.strike, option_type=r.option_type,
            ltp=r.quote.ltp if r.quote else None, oi=r.quote.oi if r.quote else None,
            iv=r.iv, delta=r.greeks.delta if r.greeks else None,
        )
        for r in summary.rows
    )
    return OptionsResearchContext(
        research_snapshot_id=str(uuid.uuid4()),
        underlying=summary.underlying,
        research_timestamp=summary.provenance.chain_timestamp or summary.as_of or summary.provenance.spot_timestamp,
        as_of=summary.as_of,
        spot=summary.spot, spot_timestamp=summary.provenance.spot_timestamp,
        expiry=summary.expiry,
        dte_calendar=summary.expiry_info.days_to_expiry_calendar if summary.expiry_info else None,
        dte_trading=summary.expiry_info.days_to_expiry_trading if summary.expiry_info else None,
        atm_strike=summary.atm_strike,
        total_call_oi=summary.oi.total_call_oi if summary.oi else None,
        total_put_oi=summary.oi.total_put_oi if summary.oi else None,
        oi_pcr=summary.pcr.oi_pcr, volume_pcr=summary.pcr.volume_pcr,
        max_pain_strike=summary.max_pain.max_pain_strike,
        oi_support=summary.support_resistance.oi_support, oi_resistance=summary.support_resistance.oi_resistance,
        atm_iv=summary.atm_iv, atm_iv_source=summary.atm_iv_source.value,
        iv_rank=summary.iv_rank.iv_rank, iv_percentile=summary.iv_rank.iv_percentile,
        iv_rank_status=summary.iv_rank.status,
        expected_move=summary.expected_move.expected_move, expected_move_method=summary.expected_move.method,
        chain=chain, valid_strikes=frozenset(r.strike for r in chain),
        data_quality=summary.quality.status, data_quality_reasons=summary.quality.reasons,
        provenance={
            "underlying_source": summary.provenance.underlying_source,
            "option_chain_source": summary.provenance.option_chain_source,
            "calculation_version": summary.provenance.calculation_version,
            "risk_free_rate": summary.provenance.risk_free_rate,
            "provider_call_count": summary.provenance.provider_call_count,
        },
    )


class EvidenceQuality:
    OK = "OK"
    DEGRADED = "DEGRADED"
    INSUFFICIENT = "INSUFFICIENT"


def evidence_quality_gate(context: OptionsResearchContext) -> tuple[str, str]:
    """Section 6: inspect Phase 9's own data-quality verdict BEFORE any
    AI reasoning runs. Returns (level, reason). INSUFFICIENT means the
    job must short-circuit to an explicit degraded result rather than
    let an LLM present high-confidence research on missing data."""
    if context.data_quality == DataQuality.MISSING:
        return EvidenceQuality.INSUFFICIENT, "option-chain data is MISSING for this underlying/expiry/as_of"
    if context.atm_strike is None or context.spot is None:
        return EvidenceQuality.INSUFFICIENT, "spot/ATM could not be determined"
    if context.data_quality in (DataQuality.PARTIAL, DataQuality.STALE):
        reasons = "; ".join(context.data_quality_reasons) or context.data_quality.value
        return EvidenceQuality.DEGRADED, f"Phase 9 data quality is {context.data_quality.value}: {reasons}"
    return EvidenceQuality.OK, "Phase 9 data quality is COMPLETE"
