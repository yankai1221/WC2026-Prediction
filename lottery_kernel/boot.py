"""kernel 启动器 —— 把宿主组件装配成可运行的实例。

核心职责：**fail-loud 地校验 vault_key**（修复 S1）。
任何凭据/秘钥相关的初始化必须经过 ``boot_kernel(...)`` 这一道关。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lottery_kernel.autonomy import AutonomyGate
from lottery_kernel.debate import DebateEngine
from lottery_kernel.errors import VaultKeyMissing
from lottery_kernel.ledger import Ledger
from lottery_kernel.ports.agent_registry import AgentRegistryLike
from lottery_kernel.ports.ledger_port import LedgerStorage, RunIdProvider
from lottery_kernel.ports.tick_host import TickHost
from lottery_kernel.ports.vault import VaultKeyProvider
from lottery_kernel.ticker import Ticker


@dataclass
class Kernel:
    """装配好的 kernel 单元。

    所有四个子系统在这里持有；宿主拿到 Kernel 实例之后再调
    ``ticker.start()`` 或 ``debate.run(...)`` 即可。
    """

    autonomy: AutonomyGate
    ledger: Ledger
    debate: DebateEngine
    ticker: Optional[Ticker]
    vault_key: str  # 永远非空（fail-loud 后才到这里）


def _enforce_vault_key(provider: VaultKeyProvider | None, raw_value: str | None) -> str:
    """对应 Kompany engine.py:177-191 的修复路径。

    旧逻辑：``except Exception: pass`` → CredentialVaultStore("") → 空串解密。
    新逻辑：任何缺失都抛 VaultKeyMissing，调用方必须处理。
    """
    candidates: list[tuple[str, str | None]] = []
    if raw_value is not None:
        candidates.append(("raw_value", raw_value))
    if provider is not None:
        try:
            resolved = provider.resolve()
        except VaultKeyMissing:
            raise
        except Exception as exc:  # 把 provider 内部任何崩溃也抬到 fail-loud
            raise VaultKeyMissing(
                f"vault_key provider failed: {type(exc).__name__}: {exc}"
            ) from exc
        candidates.append(("provider", resolved))

    for source, value in candidates:
        if value and isinstance(value, str) and value.strip():
            return value

    raise VaultKeyMissing(
        "vault_key is required and must be a non-empty string. "
        "Provide either ``vault_key=...`` directly, or a VaultKeyProvider "
        "whose ``resolve()`` returns a non-empty key. "
        "Kompany's silent fallback (engine.py:177-191) is intentionally "
        "removed in lottery_kernel (fixes audit finding S1)."
    )


def boot_kernel(
    *,
    storage: LedgerStorage,
    host: TickHost,
    registry: AgentRegistryLike,
    vault_key: str | None = None,
    vault_provider: VaultKeyProvider | None = None,
    run_id_provider: RunIdProvider | None = None,
    tick_interval_seconds: int = 300,
    debate_rounds: int = 2,
    autonomy: AutonomyGate | None = None,
    enable_ticker: bool = True,
) -> Kernel:
    """组装 kernel；vault_key 缺失/为空时**直接抛错**。

    Args:
        storage: 满足 LedgerStorage 协议的 DB 连接
        host: 满足 TickHost 协议的引擎宿主
        registry: 满足 AgentRegistryLike 协议的智能体仓
        vault_key: 直接提供的 master key（优先级最高）
        vault_provider: keychain/env/file 来源的 provider
        run_id_provider: 当前 run_id 回调（contextvar 抽象）
        tick_interval_seconds: 心跳周期，默认 300s
        debate_rounds: 辩论轮数 1..3，默认 2
        autonomy: 可注入自定义 AutonomyGate（含 founder rules evaluator）
        enable_ticker: 静态分析/测试场景可设 False
    """
    key = _enforce_vault_key(vault_provider, vault_key)

    autonomy_gate = autonomy if autonomy is not None else AutonomyGate()
    ledger = Ledger(storage, run_id_provider=run_id_provider)
    debate = DebateEngine(registry, num_rounds=debate_rounds)
    ticker = (
        Ticker(host, tick_interval_seconds=tick_interval_seconds)
        if enable_ticker
        else None
    )

    return Kernel(
        autonomy=autonomy_gate,
        ledger=ledger,
        debate=debate,
        ticker=ticker,
        vault_key=key,
    )


__all__ = ["Kernel", "boot_kernel"]
