"""
The ResearchJob value type -- the shape both the (now removed, Phase 5)
in-memory store and repository.py's DB-backed functions return, so
service.py/router.py didn't need to change their calling shape when
persistence replaced the in-memory store as the source of truth.

Phase 5: the database (trading/ai_research/repository.py) is now the sole
source of truth for job/stage state (Section 10 -- "Do not maintain two
independent sources of truth"). There is no in-memory JobStore anymore.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from trading.ai_research.schemas import (
    NodeStageOut,
    ResearchDepth,
    ResearchJobStatus,
    ResearchReportOut,
    ResearchRequestIn,
    ResearchStage,
)


@dataclass(frozen=True)
class ResearchJob:
    research_id: str
    symbol: str
    research_date: date
    research_depth: ResearchDepth
    status: ResearchJobStatus
    created_at: datetime
    stage: ResearchStage = ResearchStage.QUEUED
    # Phase 7 market metadata (Section 34/35) -- always populated for a
    # new job; a pre-Phase-7 row's NULL columns are interpreted by
    # repository._row_to_job() as these exact defaults (market=US /
    # exchange=GENERIC / currency=USD), so legacy history reads unchanged.
    market: str = "US"
    exchange: str = "GENERIC"
    canonical_id: str | None = None
    currency: str = "USD"
    # Phase 8 (Section 19/29) -- small provenance summary about the
    # market-price data used for this run's technical context, or None
    # for US research / a pre-Phase-8 row.
    market_data_provenance: dict | None = None
    current_stage: str | None = None  # real node name, or None before RUNNING starts
    node_stages: tuple[NodeStageOut, ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None
    report: ResearchReportOut | None = None
    error: str | None = None
    provider_error: str | None = None
    # Populated only by repository._row_to_request() when explicitly asked
    # for (resume) -- not part of every normal read, to avoid rebuilding it
    # on every single status poll.
    request: ResearchRequestIn | None = None
