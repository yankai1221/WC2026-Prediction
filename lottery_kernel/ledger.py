"""Ledger —— 财务账本。

从 Kompany state/ledger.py 剥离：
- ``db: Database`` → ``storage: LedgerStorage`` (Protocol)
- ``current_run_id()`` 全局 contextvar → ``run_id_provider`` 注入回调
- 新增 ``record_stake`` / ``record_payout`` 体彩专用 helper
- ``balance_after`` 计算保持原子（依赖 storage.commit 的事务语义）
"""
from __future__ import annotations

from typing import Optional

from lottery_kernel.errors import StorageContractError
from lottery_kernel.models.ledger_entry import LedgerCategory, LedgerEntry
from lottery_kernel.ports.ledger_port import LedgerStorage, RunIdProvider


def _noop_run_id() -> str | None:
    return None


class Ledger:
    """SQL 风格账本。

    宿主只要提供 ``execute(sql, params) → Cursor`` + ``commit()`` 的对象即可。
    """

    def __init__(
        self,
        storage: LedgerStorage,
        *,
        run_id_provider: RunIdProvider | None = None,
    ):
        if not hasattr(storage, "execute") or not hasattr(storage, "commit"):
            raise StorageContractError(
                "LedgerStorage requires execute() and commit(); "
                f"got {type(storage).__name__}"
            )
        self.storage = storage
        self._run_id = run_id_provider or _noop_run_id

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        row = self.storage.execute(
            "SELECT balance_after FROM ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return float(row["balance_after"]) if row else 0.0

    def spent_for_project(self, project_id: str) -> float:
        row = self.storage.execute(
            "SELECT COALESCE(SUM(amount), 0.0) AS total "
            "FROM ledger WHERE project_id = ? AND category != 'income'",
            (project_id,),
        ).fetchone()
        net = float(row["total"] or 0.0) if row else 0.0
        return max(0.0, -net)

    def get_totals(self) -> dict[str, float]:
        rows = self.storage.execute(
            "SELECT category, SUM(amount) as total FROM ledger GROUP BY category"
        ).fetchall()
        return {row["category"]: float(row["total"]) for row in rows}

    def get_recent(self, limit: int = 10) -> list[dict]:
        rows = self.storage.execute(
            "SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_burn_rate(self, window_hours: int = 24) -> float:
        if window_hours <= 0:
            raise ValueError("window_hours must be > 0")
        row = self.storage.execute(
            "SELECT COALESCE(SUM(amount), 0.0) AS total "
            "FROM ledger WHERE amount < 0 AND timestamp >= datetime('now', ?)",
            (f"-{int(window_hours)} hours",),
        ).fetchone()
        total_expense = float(row["total"] or 0.0) if row else 0.0
        return abs(total_expense) / float(window_hours)

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def record(
        self,
        amount: float,
        description: str,
        category: LedgerCategory,
        directive_id: str | None = None,
        project_id: str | None = None,
        approved_by: str | None = None,
        run_id: str | None = None,
    ) -> LedgerEntry:
        balance = self.get_balance() + amount
        rid = run_id if run_id is not None else self._run_id()
        self.storage.execute(
            "INSERT INTO ledger "
            "(amount, balance_after, description, category, "
            "directive_id, project_id, approved_by, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (amount, balance, description, category.value,
             directive_id, project_id, approved_by, rid),
        )
        self.storage.commit()
        return LedgerEntry(
            amount=amount,
            balance_after=balance,
            description=description,
            category=category,
            directive_id=directive_id,
            project_id=project_id,
            approved_by=approved_by,
        )

    def record_ai_cost(
        self,
        amount_usd: float,
        description: str,
        directive_id: str | None = None,
        run_id: str | None = None,
        project_id: str | None = None,
    ) -> LedgerEntry:
        return self.record(
            amount=-abs(amount_usd),
            description=f"AI: {description}",
            category=LedgerCategory.AI_COST,
            directive_id=directive_id,
            project_id=project_id,
            approved_by="auto",
            run_id=run_id,
        )

    # ----- 体彩专用：CFO 财务勘界（100~500 元本金）-----

    def record_stake(
        self,
        amount_cny: float,
        ticket_id: str,
        match_label: str,
        approved_by: str = "cfo",
    ) -> LedgerEntry:
        """记一笔体彩注金（负数）。"""
        if amount_cny <= 0:
            raise ValueError("stake amount must be > 0 CNY")
        return self.record(
            amount=-abs(amount_cny),
            description=f"STAKE {ticket_id}: {match_label}",
            category=LedgerCategory.STAKE,
            project_id=ticket_id,
            approved_by=approved_by,
        )

    def record_payout(
        self,
        amount_cny: float,
        ticket_id: str,
        match_label: str,
    ) -> LedgerEntry:
        return self.record(
            amount=abs(amount_cny),
            description=f"PAYOUT {ticket_id}: {match_label}",
            category=LedgerCategory.PAYOUT,
            project_id=ticket_id,
            approved_by="auto",
        )


__all__ = ["Ledger"]
