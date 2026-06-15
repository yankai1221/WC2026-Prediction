"""Ledger 数据模型 —— Kompany 原 state.models 的子集复刻。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LedgerCategory(str, Enum):
    AI_COST = "ai_cost"          # LLM/工具的算力开销
    STAKE = "stake"              # 体彩本金支出（CFO 约束）
    PAYOUT = "payout"            # 中奖回款
    REFUND = "refund"            # 撤单/平局退款
    SUBSCRIPTION = "subscription"
    OTHER = "other"


@dataclass
class LedgerEntry:
    amount: float
    balance_after: float
    description: str
    category: LedgerCategory
    directive_id: str | None = None
    project_id: str | None = None
    approved_by: str | None = None
