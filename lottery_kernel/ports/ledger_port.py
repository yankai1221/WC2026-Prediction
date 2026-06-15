"""Ledger 的存储契约 —— 把对 ``Database`` 的硬依赖换成 Protocol。

Kompany 原 ledger.py 把 ``db: Database`` 写死在构造器里，
迁出来后宿主可以提供任意满足 ``LedgerStorage`` 契约的对象
（实际 SQLite、Postgres 适配器、单元测试用 InMemoryStorage 都行）。
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable


@runtime_checkable
class Cursor(Protocol):
    """DB-API 2.0 cursor 子集。"""

    def fetchone(self) -> Any: ...
    def fetchall(self) -> Iterable[Any]: ...


@runtime_checkable
class LedgerStorage(Protocol):
    """Ledger 需要的最小存储接口。

    现实里 SQLite 的 ``Connection`` + ``Row`` 工厂就满足该契约；
    我们不在 kernel 内强制 sqlite3 依赖。
    """

    def execute(self, sql: str, params: tuple | list = ...) -> Cursor: ...
    def commit(self) -> None: ...


class RunIdProvider(Protocol):
    """获取当前 run_id 的可调用对象。

    Kompany 的 ``current_run_id`` 用了 contextvars——剥离到 kernel 后
    我们让宿主自己注入，不背 contextvars 的耦合。
    """

    def __call__(self) -> str | None: ...
