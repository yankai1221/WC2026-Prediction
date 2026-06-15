"""lottery_kernel.tools —— 注册到 NativeRunner 的工具集。

odds_tools 抽自 worldcut-2026 的 `server.py`，做了三件事：
1. 解除单文件 2796 行的耦合，独立成模块。
2. 用 ThreadPoolExecutor 包装阻塞 urllib，不卡死 Ticker。
3. SPORTTERY_HTTP_PROXY 默认空，避免容器里 :7890 connection refused。
"""
from __future__ import annotations

from lottery_kernel.tools.odds_tools import (
    LOTTERY_TOOL_DOCS,
    LOTTERY_TOOLS,
    OddsToolbox,
    odds_snapshot,
    match_odds,
    sporttery_snapshot,
)

__all__ = [
    "LOTTERY_TOOL_DOCS",
    "LOTTERY_TOOLS",
    "OddsToolbox",
    "odds_snapshot",
    "match_odds",
    "sporttery_snapshot",
]
