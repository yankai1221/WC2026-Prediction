"""Ticker —— 引擎心跳循环。

从 Kompany core/ticker.py 剥离：
- engine 反向引用 → ``host: TickHost`` Protocol
- 不再 lazy-import ProjectRunner，改成 host 自己提供 ``run_one_pending_task``
- 不再依赖 harness_execution 的 ACTION_* 常量，改成构造时注入 ``blocking_action_types``
- ``_publish`` 改成构造时注入的 ``on_event`` callback，不再耦合 EventHub
- 严格按 host fail-loud：TickHost 缺关键属性时直接报错
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from lottery_kernel.errors import HostContractError
from lottery_kernel.ports.tick_host import TickHost

log = logging.getLogger(__name__)

TICK_HISTORY_KEEP = 500

# 🚨 防爆风控锁（用户授权 ACK，2026-06-15）
MIN_TICK_INTERVAL_SECONDS = 1
"""单次 tick 最短间隔。即便宿主传 0，也强制不少于 1 秒，杜绝 0 延迟空转。"""

MAX_TICKS_PER_SESSION = 10_000
"""单个 Ticker 实例从启动到停止的最大 tick 计数。到顶自动停车。"""


class _NullTickStore:
    """fallback：宿主可不提供 daemon_ticks 时使用。"""

    def record(self, **kwargs) -> dict[str, Any]:
        return dict(kwargs)

    def prune(self, keep: int) -> int:
        return 0


EventCallback = Callable[[str, dict[str, Any]], None]
"""``on_event(channel, payload)`` —— 替代 Kompany 的 EventHub。"""

TickAction = Callable[[], list[str]]


def _status_value(status: Any) -> str:
    """模仿 Kompany 原 _status_value，但不绑死 TaskStatus enum。"""
    if hasattr(status, "value"):
        return status.value
    return str(status)


PENDING_STATUS = "pending"


class Ticker:
    """4 角色体彩系统的 24/7 心跳。"""

    def __init__(
        self,
        host: TickHost,
        *,
        tick_store: Any | None = None,
        tick_interval_seconds: int = 300,
        auto_execute: bool = True,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        on_event: EventCallback | None = None,
        blocking_action_types: Iterable[str] | None = None,
    ):
        self._assert_host(host)
        self._host = host
        self._ticks = tick_store if tick_store is not None else _NullTickStore()
        # 🚨 强制最小间隔 1 秒，即使宿主传 0
        self.tick_interval_seconds = max(
            MIN_TICK_INTERVAL_SECONDS, int(tick_interval_seconds)
        )
        self.auto_execute = bool(auto_execute)
        self._sleeper = sleeper if sleeper is not None else asyncio.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._on_event = on_event
        # 默认 block 的两类预算审批；宿主可改写。
        self._blocking_action_types: set[str] = set(
            blocking_action_types
            or {"project_envelope_topup", "harness_budget_increase"}
        )

        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self.last_tick_at: str | None = None
        self.tick_count: int = 0
        # 心跳动作链表；persona 模块可 append 自己的 step。
        self.actions: list[tuple[str, TickAction]] = [
            ("heartbeat", self._action_heartbeat),
            ("advance", self._action_advance),
            ("housekeeping", self._action_housekeeping),
        ]

    @staticmethod
    def _assert_host(host: TickHost) -> None:
        """fail-loud：缺哪个属性早点炸。"""
        required = ("runtime", "projects", "approvals",
                    "heartbeat_once", "get_int_config", "run_one_pending_task")
        missing = [name for name in required if not hasattr(host, name)]
        if missing:
            raise HostContractError(
                "TickHost missing required attributes: "
                f"{', '.join(missing)} (got {type(host).__name__})"
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("ticker.start called outside a running loop; deferring")
            return
        self._stopped.clear()
        self._task = loop.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._stopped.set()
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # One tick
    # ------------------------------------------------------------------

    def tick_once(self) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        t0 = self._clock()
        actions: list[str] = []
        errors: dict[str, str] = {}
        runtime = self._host.runtime.get() or {}
        if runtime.get("state") == "suspended":
            outcome = "idle_suspended"
        else:
            for name, action in self.actions:
                try:
                    actions.extend(action() or [])
                except Exception as exc:  # noqa: BLE001 — 一步失败不许杀整个 tick
                    log.exception("ticker action %r failed", name)
                    actions.append(f"{name}:error")
                    errors[name] = str(exc)
            outcome = "error" if errors else "ok"
        duration_ms = int(max(0.0, self._clock() - t0) * 1000)
        row = self._ticks.record(
            started_at=started_at,
            duration_ms=duration_ms,
            actions=actions,
            outcome=outcome,
            detail={"errors": errors} if errors else None,
        )
        self.last_tick_at = started_at
        self.tick_count += 1
        self._publish(outcome, actions, duration_ms)
        return row

    # ------------------------------------------------------------------
    # 心跳动作
    # ------------------------------------------------------------------

    def _action_heartbeat(self) -> list[str]:
        self._host.heartbeat_once()
        return ["heartbeat"]

    def _action_advance(self) -> list[str]:
        if not self.auto_execute:
            return []
        actions: list[str] = []
        blocked = self._projects_blocked_on_approval()
        candidates = sorted(
            self._host.projects.list_active(),
            key=lambda p: getattr(p, "created_at", ""),
        )
        for project in candidates:
            tasks = self._host.projects.list_tasks(project.id)
            if tasks and not any(
                _status_value(getattr(t, "status", "")) == PENDING_STATUS
                for t in tasks
            ):
                continue
            if project.id in blocked:
                actions.append(f"skipped_pending_approval:{project.id}")
                continue
            task_id = self._host.run_one_pending_task(project.id)
            if task_id:
                actions.append(f"advanced_task:{task_id}")
                return actions
        if not actions:
            actions.append("no_work")
        return actions

    def _action_housekeeping(self) -> list[str]:
        actions: list[str] = []
        removed = self._ticks.prune(keep=TICK_HISTORY_KEEP)
        if removed:
            actions.append(f"pruned_ticks:{removed}")
        episodes = self._host.episodes
        trim = getattr(episodes, "trim_to_retention_window", None) if episodes else None
        if callable(trim):
            max_full = self._host.get_int_config(
                "episode_retention_full_count", default=50
            )
            trimmed = trim(max_full)
            if trimmed:
                actions.append(f"episodes_trimmed:{len(trimmed)}")
        return actions

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _projects_blocked_on_approval(self) -> set[str]:
        return {
            req.project_id
            for req in self._host.approvals.list_pending()
            if req.action_type in self._blocking_action_types and req.project_id
        }

    def _publish(self, outcome: str, actions: list[str], duration_ms: int) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(
                "daemon.tick",
                {
                    "activity_kind": "tick",
                    "outcome": outcome,
                    "actions": actions,
                    "duration_ms": duration_ms,
                },
            )
        except Exception:  # noqa: BLE001 — 观测层 best-effort
            pass

    async def _loop(self) -> None:
        log.debug(
            "ticker loop started (interval=%ds, auto_execute=%s, max_ticks=%d)",
            self.tick_interval_seconds,
            self.auto_execute,
            MAX_TICKS_PER_SESSION,
        )
        try:
            while not self._stopped.is_set():
                # 🚨 max-ticks 熔断：跑到上限自动停车，杜绝长程异常
                if self.tick_count >= MAX_TICKS_PER_SESSION:
                    log.error(
                        "ticker: hit MAX_TICKS_PER_SESSION=%d, auto-stopping "
                        "to prevent runaway loop",
                        MAX_TICKS_PER_SESSION,
                    )
                    self._stopped.set()
                    break
                await self._sleeper(self.tick_interval_seconds)
                if self._stopped.is_set():
                    break
                try:
                    self.tick_once()
                except Exception as exc:  # noqa: BLE001
                    log.exception("ticker tick_once failed")
                    with contextlib.suppress(Exception):
                        self._ticks.record(
                            started_at=datetime.now(timezone.utc).isoformat(),
                            duration_ms=0,
                            actions=[],
                            outcome="error",
                            detail={"error": str(exc)},
                        )
        except asyncio.CancelledError:
            log.debug("ticker loop cancelled")
            raise


__all__ = [
    "TICK_HISTORY_KEEP",
    "MIN_TICK_INTERVAL_SECONDS",
    "MAX_TICKS_PER_SESSION",
    "Ticker",
    "EventCallback",
    "TickAction",
]
