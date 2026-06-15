"""TickHost —— Ticker 需要的"宿主"接口。

这是把 ``ticker.py`` 从 KompanyEngine 拆出的关键。
Kompany 原版 ``Ticker(engine=...)`` 反向引用了 9 处 engine 属性
（runtime / projects / episodes / approvals / heartbeat_once / _get_int_config 等），
现在统一收敛到这个 Protocol，kernel 永远只看 ``host: TickHost``。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PendingApproval(Protocol):
    """ticker 关心的 approval 字段子集。"""

    action_type: str
    project_id: str | None


@runtime_checkable
class ApprovalsLike(Protocol):
    def list_pending(self) -> list[PendingApproval]: ...


@runtime_checkable
class ProjectLike(Protocol):
    id: str
    created_at: Any


@runtime_checkable
class ProjectsLike(Protocol):
    def list_active(self) -> list[ProjectLike]: ...
    def list_tasks(self, project_id: str) -> list[Any]: ...


@runtime_checkable
class EpisodesLike(Protocol):
    def trim_to_retention_window(self, keep: int) -> list[Any]: ...


@runtime_checkable
class RuntimeGate(Protocol):
    def get(self) -> dict | None: ...


@runtime_checkable
class TickHost(Protocol):
    """宿主必须暴露的最小接口集合。

    缺任何一个属性都应在 ``Ticker.__init__`` 时尽早爆掉（fail-loud），
    而不是延迟到 tick 触发那一刻。
    """

    runtime: RuntimeGate
    projects: ProjectsLike
    approvals: ApprovalsLike
    episodes: EpisodesLike | None

    def heartbeat_once(self) -> None: ...
    def get_int_config(self, key: str, *, default: int) -> int: ...

    # advance 阶段执行 single pending task；宿主自行决定如何 run。
    # 返回 task_id 表示推进成功，None 表示 "no work / blocked"。
    def run_one_pending_task(self, project_id: str) -> str | None: ...
