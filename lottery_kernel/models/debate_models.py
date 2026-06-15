"""Debate 数据模型 —— 从 Kompany core/debate_models.py 提炼。

仅保留 kernel 内 DebateEngine 真正消费的字段，
所有字段都用 pydantic（智能体 LLM 结构化输出必需）；
若无 pydantic，则回退 dataclass（kernel 不强制 pydantic 依赖）。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

try:
    from pydantic import BaseModel, Field
    _HAS_PYDANTIC = True
except ImportError:  # 测试/极简部署可不带 pydantic
    _HAS_PYDANTIC = False
    from dataclasses import dataclass, field

    def Field(*, default=None, default_factory=None, **_):  # type: ignore
        if default_factory is not None:
            return field(default_factory=default_factory)
        return field(default=default)

    class BaseModel:  # type: ignore
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            dataclass(cls)


class SourceType(str, Enum):
    USER_INPUT = "user_input"
    TEMPLATE_DEFAULT = "template_default"
    LEDGER_ENTRY = "ledger_entry"
    AGENT_MEMORY = "agent_memory"
    AUDIT_EVENT = "audit_event"
    ODDS_SNAPSHOT = "odds_snapshot"     # 新增：来自 odds_tools 的实时盘口
    SPORTTERY_FEED = "sporttery_feed"   # 新增：来自体彩中心
    INFERRED = "inferred"


class DebateRound(str, Enum):
    POSITION = "position"
    REBUTTAL = "rebuttal"
    CONVERGENCE = "convergence"


class Source(BaseModel):
    source_type: SourceType = SourceType.INFERRED
    source_ref: str = ""
    claim_supported: str = ""


class Claim(BaseModel):
    text: str = ""
    evidence: list[Source] = Field(default_factory=list)


class AgentPosition(BaseModel):
    agent_role: str = ""
    agent_name: str = ""
    squad: str = ""
    round: DebateRound = DebateRound.POSITION
    claims: list[Claim] = Field(default_factory=list)
    recommendation: str = ""
    confidence: str = "中"
    # 兼容字段：原 Kompany analysis 字符串
    analysis: str = ""

    def effective_claims(self) -> list[Claim]:
        if self.claims:
            return self.claims
        # 退化：把 analysis 视为一条 inferred claim
        if self.analysis:
            return [Claim(text=self.analysis, evidence=[Source()])]
        return []


class DebateSynthesis(BaseModel):
    consensus_claims: list[Claim] = Field(default_factory=list)
    key_tensions: list[str] = Field(default_factory=list)
    recommended_option: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    consensus_position: str = ""

    def effective_consensus_claims(self) -> list[Claim]:
        if self.consensus_claims:
            return self.consensus_claims
        if self.consensus_position:
            return [Claim(text=self.consensus_position, evidence=[Source()])]
        return []


class CEODecision(BaseModel):
    decision: str = ""
    rationale_claims: list[Claim] = Field(default_factory=list)
    rationale: str = ""
    issue_ticket: bool = False          # 出单指令
    stake_allocation: dict[str, float] = Field(default_factory=dict)
    scorelines: list[str] = Field(default_factory=list)


class DebateResult(BaseModel):
    question: str = ""
    rounds: list[list[AgentPosition]] = Field(default_factory=list)
    synthesis: DebateSynthesis = Field(default_factory=DebateSynthesis)
    decision: CEODecision = Field(default_factory=CEODecision)
    agents_participated: list[str] = Field(default_factory=list)
