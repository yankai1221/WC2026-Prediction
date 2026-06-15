"""PEP 544 Protocols — kernel 期望宿主实现的契约。

把 Kompany 14-Mixin 的隐式依赖收敛到 4 个显式接口：
- LedgerStorage：Ledger 需要的 DB 行为
- TickHost：Ticker 需要的引擎反向引用
- AgentRegistryLike / AgentLike：DebateEngine 需要的智能体仓
- RuntimeGate / VaultKeyProvider：跨模块共享的运行时门控

宿主可用任意实现（SQLite / Postgres / 内存 mock）来满足这些 Protocol。
"""
from __future__ import annotations

from lottery_kernel.ports.ledger_port import (
    Cursor,
    LedgerStorage,
    RunIdProvider,
)
from lottery_kernel.ports.tick_host import (
    ApprovalsLike,
    EpisodesLike,
    PendingApproval,
    ProjectLike,
    ProjectsLike,
    RuntimeGate,
    TickHost,
)
from lottery_kernel.ports.agent_registry import (
    AgentLike,
    AgentRegistryLike,
    StructuredResponse,
)
from lottery_kernel.ports.vault import VaultKeyProvider

__all__ = [
    "Cursor",
    "LedgerStorage",
    "RunIdProvider",
    "TickHost",
    "RuntimeGate",
    "ProjectsLike",
    "ProjectLike",
    "EpisodesLike",
    "ApprovalsLike",
    "PendingApproval",
    "AgentRegistryLike",
    "AgentLike",
    "StructuredResponse",
    "VaultKeyProvider",
]
