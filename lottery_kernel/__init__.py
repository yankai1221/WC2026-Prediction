"""lottery_kernel — 体彩预测系统的独立自主代理内核。

从 Kompany 主干剥离四个核心基建（autonomy / ledger / debate / ticker），
通过 PEP 544 Protocol 解耦，斩断对 Mixin/WebUI 的依赖。

设计原则：
- Fail-loud：vault_key 未提供 → 直接抛 VaultKeyMissing，绝不静默回退。
- Protocol 注入：宿主提供 TickHost/LedgerStorage/AgentRegistryLike。
- 工具线程化：odds_tools 通过 ThreadPoolExecutor 包装阻塞 urllib。
"""
from __future__ import annotations

__version__ = "0.1.0"

from lottery_kernel.errors import VaultKeyMissing, KernelConfigError

__all__ = ["VaultKeyMissing", "KernelConfigError", "__version__"]
