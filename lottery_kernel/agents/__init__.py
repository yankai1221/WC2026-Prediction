"""灵魂加载器 —— 把 4 个 YAML Soul 编译成 BaseAgent 的 prompt 上下文。

设计契约（Prompt Assembly Contract）：

1. 每个 .yaml 必须含有顶层 keys:
     role, display_name, squad, tone, mandate, tools (list[str]),
     constitution (list[str]), debate_focus (list[str])
   可选 keys:
     allowed_tool_args (dict), forbidden (list[str]),
     position_template, rebuttal_template, convergence_template

2. ``compose_system_prompt(soul)`` 输出一个稳定 system 段：

     [IDENTITY]
       You are {display_name}, the {role.upper()} of the lottery board.
       Squad: {squad}.

     [TONE]
       {tone}

     [MANDATE]
       {mandate}

     [CONSTITUTION] (hard rules, never violate)
       1. ...
       2. ...

     [TOOLS] (callable via NativeRunner; never invent tools)
       - odds_snapshot: ...
       - match_odds: ...

     [DEBATE_FOCUS]
       During debate rounds, anchor every claim on:
       - ...

     [FORBIDDEN] (auto-reject these in your own output)
       - ...

3. DebateEngine 调用 ``agent.call_structured(prompt=...)`` 时，
   prompt 里只有 round-specific 任务（在 debate.py 内已组装）；
   system 段由 BaseAgent.__init__ 时一次性塞入，不重复发送。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # 极简部署不带 PyYAML —— 显式报错
    raise ImportError(
        "lottery_kernel.agents requires PyYAML to load souls; "
        "install with `pip install pyyaml`."
    ) from e


SOULS_DIR = Path(__file__).parent / "souls"

# Soul YAML 必备 keys
REQUIRED_KEYS = (
    "role", "display_name", "squad", "tone", "mandate",
    "tools", "constitution", "debate_focus",
)


@dataclass
class Soul:
    role: str
    display_name: str
    squad: str
    tone: str
    mandate: str
    tools: list[str]
    constitution: list[str]
    debate_focus: list[str]
    forbidden: list[str] = field(default_factory=list)
    allowed_tool_args: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


def load_soul(role: str, *, souls_dir: Path | None = None) -> Soul:
    """从 ``<souls_dir>/<role>.yaml`` 加载并校验。"""
    base = Path(souls_dir) if souls_dir else SOULS_DIR
    path = base / f"{role}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Soul not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise ValueError(
            f"Soul {role!r} missing required keys: {', '.join(missing)}"
        )
    return Soul(
        role=str(raw["role"]),
        display_name=str(raw["display_name"]),
        squad=str(raw["squad"]),
        tone=str(raw["tone"]),
        mandate=str(raw["mandate"]),
        tools=list(raw["tools"] or []),
        constitution=list(raw["constitution"] or []),
        debate_focus=list(raw["debate_focus"] or []),
        forbidden=list(raw.get("forbidden") or []),
        allowed_tool_args=dict(raw.get("allowed_tool_args") or {}),
        extra={
            k: v
            for k, v in raw.items()
            if k not in REQUIRED_KEYS and k not in {"forbidden", "allowed_tool_args"}
        },
    )


def load_all_souls(souls_dir: Path | None = None) -> dict[str, Soul]:
    base = Path(souls_dir) if souls_dir else SOULS_DIR
    souls: dict[str, Soul] = {}
    for yaml_path in sorted(base.glob("*.yaml")):
        role = yaml_path.stem
        souls[role] = load_soul(role, souls_dir=base)
    return souls


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  - {it}" for it in items) if items else "  (none)"


def _numbered(items: list[str]) -> str:
    if not items:
        return "  (none)"
    return "\n".join(f"  {i+1}. {it}" for i, it in enumerate(items))


def compose_system_prompt(soul: Soul, *, minimal: bool = False) -> str:
    """把 Soul 编译成 BaseAgent 的 system prompt 段。

    Args:
        soul: 已加载的 Soul（来自 ``load_soul(role)``）。
        minimal: True → 脱水模式（dry_run 用）。
            只保留 IDENTITY + TONE + CONSTITUTION 三段，约 600 tokens；
            为对抗中转网络的 prefill 慢速瓶颈而设。
            False → 完整模式（生产用，约 3K tokens）。
    """
    if minimal:
        # 🚨 脱水模式：物理压缩到 ~600 tokens
        # 只保留 IDENTITY + TONE + CONSTITUTION（其余下沉到 user prompt）
        return "\n".join([
            f"[IDENTITY] You are {soul.display_name}, {soul.role.upper()} of the lottery board.",
            f"[TONE] {soul.tone.strip()}",
            "[CONSTITUTION] (hard rules — violating these is automatic rejection)",
            _numbered(soul.constitution),
        ])

    blocks = [
        "[IDENTITY]",
        f"  You are {soul.display_name}, the {soul.role.upper()} of the lottery board.",
        f"  Squad: {soul.squad}.",
        "",
        "[TONE]",
        f"  {soul.tone}",
        "",
        "[MANDATE]",
        f"  {soul.mandate}",
        "",
        "[CONSTITUTION] (hard rules — violating these is automatic rejection)",
        _numbered(soul.constitution),
        "",
        "[TOOLS] (callable via NativeRunner — never invent tools not listed here)",
        _bullets(soul.tools),
        "",
        "[DEBATE_FOCUS] (every factual claim you make MUST cite one of these axes)",
        _bullets(soul.debate_focus),
    ]
    if soul.forbidden:
        blocks += [
            "",
            "[FORBIDDEN] (auto-reject these patterns in your own output)",
            _bullets(soul.forbidden),
        ]
    return "\n".join(blocks)


__all__ = [
    "Soul",
    "SOULS_DIR",
    "REQUIRED_KEYS",
    "load_soul",
    "load_all_souls",
    "compose_system_prompt",
]
