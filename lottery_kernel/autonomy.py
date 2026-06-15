"""AutonomyGate —— 决定行动是否需要"Master"（玩家）审批。

剥离自 Kompany core/autonomy.py，唯一差别：
- 把对 ``founder_config`` 的函数内导入改成宿主注入的回调
  （``rules_evaluator`` 参数），避免 kernel 反依赖 Kompany 的 founder 层。
- 阈值改成可配置，体彩场景默认更严格：100 元自动放行天花板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


# CFO 死守 100~500 元本金，单笔自动放行天花板设为 50 元（一半的低阶）。
DEFAULT_THRESHOLDS: Mapping[str, float] = {
    "auto": 5.0,
    "ceo": 50.0,
}


RulesEvaluator = Callable[..., Optional[str]]
"""签名: (rules, *, tool_name, side_effect, estimated_cost_usd, description) -> 拒绝理由 | None。"""


def _no_extra_rules(*_args: Any, **_kwargs: Any) -> str | None:
    """默认 evaluator：放行（kernel 自身不带 founder 规则）。"""
    return None


@dataclass
class AutonomyGate:
    thresholds: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    rules_evaluator: RulesEvaluator = _no_extra_rules

    def check(self, approval_tier: str, estimated_cost: float | None) -> bool:
        """True → 无需 Master 审批可执行。"""
        if approval_tier == "master":
            return False
        if approval_tier == "auto":
            return True
        if approval_tier == "ceo":
            cost = estimated_cost or 0
            return cost <= self.thresholds["ceo"]
        return False

    def check_tool(
        self,
        side_effect: str,
        autonomy_tier: str,
        estimated_cost_usd: float = 0.0,
        configured_auto: bool = False,
    ) -> bool:
        """Tool inline 执行权限。

        PAID 硬关：只要带 spend / 真实成本，任何 configured_auto 都救不了。
        """
        if side_effect == "spend" or (estimated_cost_usd or 0.0) > 0:
            return False
        if side_effect != "read":
            return False
        return autonomy_tier == "auto"

    def check_rules(
        self,
        rules: dict | None,
        *,
        tool_name: str,
        side_effect: str = "read",
        estimated_cost_usd: float = 0.0,
        description: str = "",
    ) -> str | None:
        """委托宿主 evaluator 做规则裁定。

        kernel 自身不知道 founder rules 长什么样；
        宿主在装配时注入 ``rules_evaluator=founder_config.check_tool_rules``
        即可还原 Kompany 行为。
        """
        return self.rules_evaluator(
            rules,
            tool_name=tool_name,
            side_effect=side_effect,
            estimated_cost_usd=estimated_cost_usd,
            description=description,
        )


__all__ = ["AutonomyGate", "DEFAULT_THRESHOLDS", "RulesEvaluator"]
