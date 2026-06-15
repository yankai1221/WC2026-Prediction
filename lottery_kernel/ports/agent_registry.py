"""AgentRegistryLike —— DebateEngine 需要的智能体仓库契约。

Kompany 原版 debate.py:39-40 把 ``AgentRegistry`` 锁死在
``TYPE_CHECKING`` 里。本协议把这层 typing-only 依赖改成
"任何能 ``get(role) → AgentLike`` 的对象都行" 的鸭子类型。
"""
from __future__ import annotations

from typing import Any, Protocol, Type, runtime_checkable


@runtime_checkable
class StructuredResponse(Protocol):
    """LLM 结构化返回；至少要有 ``parsed`` 字段。"""

    parsed: Any


@runtime_checkable
class AgentLike(Protocol):
    """专家智能体最小接口。

    四个核心 Soul（ceo / cfo / cro / analyst）都将实现这个接口。
    """

    display_name: str
    squad: str
    role: str

    def call_structured(
        self,
        *,
        prompt: str,
        output_schema: Type[Any],
        directive_id: str | None = None,
        max_tokens: int = 2048,
        action_type: str = "",
    ) -> StructuredResponse: ...


@runtime_checkable
class AgentRegistryLike(Protocol):
    def get(self, role: str) -> AgentLike: ...
