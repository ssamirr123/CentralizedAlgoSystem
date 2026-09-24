"""Phase 10 -- in-process job shape, mirroring trading/ai_research/jobs.py."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from trading.ai_options_research.schemas import OptionsResearchRequestIn


@dataclass(frozen=True)
class NodeStageOut:
    name: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class OptionsResearchJob:
    research_id: str
    underlying: str
    expiry: date | None
    as_of: datetime | None
    status: str
    current_stage: str | None
    research_snapshot_id: str | None
    evidence_quality: str | None
    report: dict | None
    error: str | None
    provider_error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    node_stages: tuple[NodeStageOut, ...] = ()
    request: OptionsResearchRequestIn | None = None
