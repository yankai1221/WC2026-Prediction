"""dry_run_pipeline.py —— 四角辩论系统的端到端干跑（OFFLINE + 风控锁版）。

🚨 风控总开关（用户授权 ACK，2026-06-15）：
    HARD_MAX_TURNS_PER_AGENT = 2        # 单 agent 内 LLM-turn 上限
    DEADLINE_SECONDS         = 90       # 全 pipeline 总时长熔断
    DebateEngine 内置硬上限    = 8       # 单次 debate.run 全局 agent 调用上限
    odds_tools.OFFLINE_ONLY  = True     # 全程零网络，input_matches.json

执行流程：
    1. 调用 sporttery_snapshot() —— 在 OFFLINE_ONLY 模式下直接读
       项目根目录 input_matches.json，零 urllib。
    2. 选第一场比赛（荷兰 vs 日本）作为辩论议题。
    3. Mock LedgerStorage / Mock TickHost 满足 Protocol 而不真造 SQLite。
    4. MockAgentRegistry 把 4 个 Soul YAML 编译为 system prompt，
       接到真实 Anthropic 客户端（apex/primary 双档位）。
    5. DebateEngine.run() 走完 R1 + R2 + CEO 综合 + CEO 决断（共 8 次调用）。
    6. 决不启动 Ticker —— dry_run 是一次性脚本。
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

# Windows GBK 终端 → 强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from lottery_kernel.agents import compose_system_prompt, load_soul  # noqa: E402
from lottery_kernel.debate import (  # noqa: E402
    CircuitBreakerTripped,
    DebateEngine,
    HARD_TOTAL_AGENT_CALLS,
)
from lottery_kernel.models.debate_models import (  # noqa: E402
    AgentPosition,
    CEODecision,
    DebateRound,
    DebateSynthesis,
)
from lottery_kernel.tools.odds_tools import (  # noqa: E402
    LOTTERY_TOOL_DOCS,
    LOTTERY_TOOLS,
    OFFLINE_ONLY,
    sporttery_snapshot,
)


# ======================================================================
# 🚨 防爆风控锁
# ======================================================================

HARD_MAX_TURNS_PER_AGENT = 2
"""单个 AnthropicAgent.call_structured 内部 LLM turn 上限。"""

DEADLINE_SECONDS = 180
"""整个 dry_run 从启动到结束的最大总时长。超过即熔断退出。"""

ANTHROPIC_HTTP_TIMEOUT = 60.0
"""单次 messages.create() HTTP 调用硬超时（秒）—— 防中转网络抽风。"""

DEFAULT_MAX_TOKENS = 800
"""单次结构化输出 token 上限 —— 降低串行吐字时延。"""

MINIMAL_SYSTEM_PROMPT = True
"""C 方案脱水模式 —— 把 system prompt 从 3K 压到 ~600 tokens。"""


class DryRunDeadlineExceeded(RuntimeError):
    """全 pipeline 总时长熔断。"""


_pipeline_start_monotonic: float | None = None


def _enforce_deadline(stage: str) -> None:
    if _pipeline_start_monotonic is None:
        return
    elapsed = time.monotonic() - _pipeline_start_monotonic
    if elapsed > DEADLINE_SECONDS:
        raise DryRunDeadlineExceeded(
            f"deadline {DEADLINE_SECONDS}s exceeded at stage={stage} "
            f"(elapsed={elapsed:.1f}s)"
        )


# ----------------------------------------------------------------------
# Mock LedgerStorage / Mock TickHost（满足 Protocol）
# ----------------------------------------------------------------------


class InMemoryCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class MockLedgerStorage:
    """满足 LedgerStorage Protocol 的极简内存账本。

    只支持 ledger 表的几条 SQL 模式（kernel ledger.py 真实用到的）。
    """

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._next_id = 1

    def execute(self, sql: str, params: tuple | list = ()):
        sql_lc = sql.lower().strip()
        if sql_lc.startswith("select balance_after"):
            if not self.entries:
                return InMemoryCursor([])
            return InMemoryCursor([{"balance_after": self.entries[-1]["balance_after"]}])
        if sql_lc.startswith("insert into ledger"):
            (amount, balance, desc, cat, did, pid, by, rid) = params
            row = {
                "id": self._next_id,
                "amount": amount,
                "balance_after": balance,
                "description": desc,
                "category": cat,
                "directive_id": did,
                "project_id": pid,
                "approved_by": by,
                "run_id": rid,
            }
            self._next_id += 1
            self.entries.append(row)
            return InMemoryCursor([])
        return InMemoryCursor([])

    def commit(self) -> None:
        pass


class MockApprovals:
    def list_pending(self):
        return []


class MockProjects:
    def list_active(self):
        return []

    def list_tasks(self, project_id: str):
        return []


class MockRuntimeGate:
    def get(self):
        return {"state": "running"}


class MockTickHost:
    def __init__(self):
        self.runtime = MockRuntimeGate()
        self.projects = MockProjects()
        self.approvals = MockApprovals()
        self.episodes = None

    def heartbeat_once(self) -> None:
        pass

    def get_int_config(self, key: str, *, default: int) -> int:
        return default

    def run_one_pending_task(self, project_id: str) -> str | None:
        return None


# ----------------------------------------------------------------------
# Anthropic-backed Agent (满足 AgentLike Protocol)
# ----------------------------------------------------------------------


class _StructuredResp:
    def __init__(self, parsed):
        self.parsed = parsed


SCHEMA_PATCH = {
    "AgentPosition": AgentPosition,
    "DebateSynthesis": DebateSynthesis,
    "CEODecision": CEODecision,
}


def _pydantic_to_input_schema(model_cls: type[BaseModel]) -> dict:
    """把 pydantic schema 转成 Anthropic tool 的 input_schema。

    关键：**保留 $defs**——Claim/Source 是嵌套模型，schema 用 $ref
    指向 #/$defs/Claim 这种引用，强删 $defs 会让所有引用悬空。

    再做两件事强制好行为：
      1. 去掉兼容/弃用字段（analysis / consensus_position / rationale），
         否则模型会把所有内容倒进这些 free-text 字段，留 claims=[]。
      2. 把 claims/recommendation 加入 required。
    """
    schema = model_cls.model_json_schema()
    if schema.get("type") != "object":
        schema["type"] = "object"

    def _clean(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("title", None)
            for v in list(node.values()):
                _clean(v)
        elif isinstance(node, list):
            for v in node:
                _clean(v)

    _clean(schema)

    deprecated_fields = {
        "AgentPosition":   ["analysis"],
        "DebateSynthesis": ["consensus_position"],
        "CEODecision":     ["rationale"],
    }
    extra_required = {
        "AgentPosition":   ["claims", "recommendation"],
        "DebateSynthesis": ["consensus_claims", "recommended_option"],
        "CEODecision":     ["decision", "rationale_claims", "issue_ticket"],
    }
    name = model_cls.__name__
    props: dict = schema.get("properties", {})
    for f in deprecated_fields.get(name, []):
        props.pop(f, None)
    if name in extra_required:
        req = set(schema.get("required", []))
        req.update(extra_required[name])
        schema["required"] = sorted(req)

    schema.setdefault("additionalProperties", True)
    return schema


class AnthropicAgent:
    """绑一个 Soul 的真实 Anthropic 智能体。"""

    def __init__(
        self,
        soul,
        client: anthropic.Anthropic,
        model: str,
        toolbox: dict | None = None,
    ):
        self.role = soul.role
        self.display_name = soul.display_name
        self.squad = soul.squad
        self._soul = soul
        self._client = client
        self._model = model
        self._system = compose_system_prompt(soul, minimal=MINIMAL_SYSTEM_PROMPT)
        self._toolbox = toolbox or {}

    # ------------------ AgentLike contract ------------------
    def call_structured(
        self,
        *,
        prompt: str,
        output_schema: type[BaseModel],
        directive_id: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        action_type: str = "",
    ):
        tool_name = "submit_" + output_schema.__name__.lower()
        submit_tool = {
            "name": tool_name,
            "description": (
                f"Submit your {output_schema.__name__} as a tool call. "
                "This is the ONLY way to respond. Populate ``claims`` with "
                "3-5 atomic factual statements; each evidence Source must "
                "cite either odds_snapshot/sporttery_feed (when grounded in "
                "the market snapshot) or agent_memory (domain knowledge). "
                "Do NOT write free text outside this tool call."
            ),
            "input_schema": _pydantic_to_input_schema(output_schema),
        }
        extra_tools = self._build_data_tools(action_type)
        tools_first = [submit_tool] + extra_tools

        first_choice = (
            {"type": "any"} if extra_tools else {"type": "tool", "name": tool_name}
        )

        messages: list[dict] = [{"role": "user", "content": prompt}]
        force_submit_next = False

        # 🚨 HARD_MAX_TURNS_PER_AGENT 硬上限：通常 turn0 = call data，
        # turn1 = forced submit；用 turn 数限制兜底，模型若再不出 submit
        # 立即放弃，绝不进入第三轮 LLM 调用。
        for turn in range(HARD_MAX_TURNS_PER_AGENT):
            _enforce_deadline(f"agent:{self.role}:turn{turn}")
            choice = (
                {"type": "tool", "name": tool_name}
                if force_submit_next
                else first_choice
            )
            tools_now = [submit_tool] if force_submit_next else tools_first
            # 🚨 A 方案：HTTP 层防爆盾 —— 单点 HTTP 任何超时/错误都不许炸全场
            try:
                resp = self._client.messages.create(
                    model=self._model,
                    system=self._system,
                    tools=tools_now,
                    tool_choice=choice,
                    max_tokens=max_tokens,
                    messages=messages,
                )
            except anthropic.APITimeoutError as exc:
                print(f"\n  ⚠️  {self.role.upper()} HTTP timeout @ {ANTHROPIC_HTTP_TIMEOUT}s — 降级")
                fallback = output_schema()  # type: ignore[call-arg]
                if hasattr(fallback, "recommendation"):
                    fallback.recommendation = "(timeout)"
                if hasattr(fallback, "analysis"):
                    fallback.analysis = (
                        f"(HTTP timeout @ {ANTHROPIC_HTTP_TIMEOUT}s — agent "
                        f"{self.role} unable to respond within budget)"
                    )
                if hasattr(fallback, "confidence"):
                    fallback.confidence = "低"
                return _StructuredResp(fallback)
            except anthropic.APIStatusError as exc:
                print(f"\n  ⚠️  {self.role.upper()} HTTP {exc.status_code} — 降级")
                fallback = output_schema()  # type: ignore[call-arg]
                if hasattr(fallback, "analysis"):
                    fallback.analysis = f"(API error {exc.status_code}: {str(exc)[:200]})"
                return _StructuredResp(fallback)
            except anthropic.APIError as exc:
                print(f"\n  ⚠️  {self.role.upper()} API error: {type(exc).__name__} — 降级")
                fallback = output_schema()  # type: ignore[call-arg]
                if hasattr(fallback, "analysis"):
                    fallback.analysis = f"({type(exc).__name__}: {str(exc)[:200]})"
                return _StructuredResp(fallback)

            submit_block = next(
                (b for b in resp.content
                 if getattr(b, "type", "") == "tool_use" and b.name == tool_name),
                None,
            )
            if submit_block is not None:
                try:
                    parsed = output_schema(**submit_block.input)
                except Exception as exc:  # noqa: BLE001
                    parsed = output_schema()  # type: ignore[call-arg]
                    if hasattr(parsed, "recommendation"):
                        parsed.recommendation = f"(schema validation failed: {exc})"
                return _StructuredResp(parsed)

            data_calls = [
                b for b in resp.content
                if getattr(b, "type", "") == "tool_use" and b.name in self._toolbox
            ]
            if not data_calls:
                # 模型既没 submit 也没 call data → 下一轮强制 submit
                force_submit_next = True
                messages.append({"role": "user", "content": (
                    f"You did NOT call any tool. In the next turn, call "
                    f"`{tool_name}` directly and submit the structured result."
                )})
                continue

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for call in data_calls:
                args = self._normalize_data_args(call.name, call.input)
                fn = self._toolbox[call.name]
                try:
                    obs = fn(None, args)
                except Exception as exc:  # noqa: BLE001
                    obs = json.dumps({"tool_error": f"{type(exc).__name__}: {exc}"})
                if len(obs) > 4000:
                    obs = obs[:4000] + "\n... [truncated]"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": obs,
                })
                _emit_tool_use(self.role, call.name, args, obs)
            messages.append({"role": "user", "content": tool_results})
            # 拿到数据后立刻强制下一轮 submit，防止无穷 ping
            force_submit_next = True

        # HARD_MAX_TURNS_PER_AGENT 用完仍未 submit → 兜底，绝不再发 LLM 请求
        fallback = output_schema()  # type: ignore[call-arg]
        if hasattr(fallback, "recommendation"):
            fallback.recommendation = (
                f"(agent {self.role} exhausted {HARD_MAX_TURNS_PER_AGENT} "
                "turns without submitting)"
            )
        return _StructuredResp(fallback)

    def _normalize_data_args(self, tool_name: str, raw: dict | None) -> dict:
        """模型常把 home/away 写成 match='A vs B' 或 team=…，这里救场。"""
        r = dict(raw or {})
        if tool_name in {"match_odds", "sporttery_match"}:
            if "home" not in r or "away" not in r:
                m = r.get("match") or r.get("fixture") or ""
                team = r.get("team")
                if " vs " in str(m):
                    h, a = str(m).split(" vs ", 1)
                    r.setdefault("home", h.strip())
                    r.setdefault("away", a.strip())
                elif team:
                    r.setdefault("home", str(team))
                    r.setdefault("away", "")
        return r

    def _build_data_tools(self, action_type: str) -> list[dict]:
        """只在 round_1 / debate_round / synthesis 之外不开数据工具（避免反复调用）。"""
        if not self._toolbox:
            return []
        # 只在 round 1（debate_round_1）启用，避免 R2/R3 重复爬
        if action_type not in {"debate_round_1"}:
            return []
        defs = [
            {
                "name": "odds_snapshot",
                "description": "拉取竞彩官方实时盘口快照。无参数。",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "match_odds",
                "description": "查询 The Odds API 某场比赛的多家盘口。",
                "input_schema": {
                    "type": "object",
                    "properties": {"home": {"type": "string"}, "away": {"type": "string"}},
                    "required": ["home", "away"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "sporttery_match",
                "description": "在体彩快照中按队名子串搜索单场。",
                "input_schema": {
                    "type": "object",
                    "properties": {"home": {"type": "string"}, "away": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        ]
        # 过滤：只暴露该 soul 在 yaml 里声明能用的工具
        allowed = {t.split()[0] for t in self._soul.tools if not t.startswith("(")}
        return [d for d in defs if d["name"] in allowed]


class MockAgentRegistry:
    def __init__(self, agents: dict[str, AnthropicAgent]):
        self._agents = agents

    def get(self, role: str) -> AnthropicAgent:
        return self._agents[role]


# ----------------------------------------------------------------------
# 打印工具（中文友好）
# ----------------------------------------------------------------------


def _box(title: str, ch: str = "─") -> None:
    line = ch * 70
    print()
    print(line)
    print(f"  {title}")
    print(line)


def _emit_tool_use(role: str, tool: str, args: Any, obs: str) -> None:
    print()
    print(f"  ⟦TOOL⟧ {role.upper()} → {tool}({json.dumps(args, ensure_ascii=False)})")
    obs_preview = obs[:300] + ("...[+]" if len(obs) > 300 else "")
    print(f"          obs: {obs_preview}")


def _render_position(pos: AgentPosition) -> None:
    print(f"\n  ⟦{pos.agent_name} / {pos.role if hasattr(pos, 'role') else pos.agent_role}⟧")
    print(f"    confidence: {pos.confidence}")
    print(f"    recommendation: {pos.recommendation}")
    if pos.claims:
        print(f"    claims ({len(pos.claims)}):")
        for c in pos.claims:
            srcs = ",".join(
                (s.source_ref or s.source_type.value) for s in (c.evidence or [])
            )
            print(f"      • {c.text}  [{srcs}]")
    elif pos.analysis:
        print(f"    analysis: {textwrap.shorten(pos.analysis, 240)}")


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------


def pick_match(snapshot: dict) -> dict:
    """从快照中挑一场 2026 世界杯比赛。

    优先策略：优先世界杯 league；其次直接取第一场。
    """
    matches = snapshot.get("matches", [])
    if not matches:
        raise RuntimeError("Sporttery snapshot returned no matches; cannot proceed.")
    wc = [m for m in matches if "世界杯" in str(m.get("league", ""))]
    return (wc[0] if wc else matches[0])


def _force_local() -> bool:
    return os.getenv("DRY_RUN_FORCE_LOCAL", "0").lower() in {"1", "true", "yes"}


def build_market_context(match: dict, snap: dict) -> dict:
    dec = match.get("decimal", [None, None, None])
    return {
        "match": f"{match.get('home')} vs {match.get('away')}",
        "league": match.get("league"),
        "kickoff": f"{match.get('match_date')} {match.get('match_clock')}",
        "market_code": match.get("market_code"),
        "market_name": match.get("market_name"),
        "goal_line": match.get("goal_line") or "(no handicap)",
        "decimal_odds_HDA": dec,
        "implied_probs_raw": [
            round(100 / o, 1) if o else None for o in dec
        ],
        "snapshot_source_url": snap.get("source_url"),
        "snapshot_status": snap.get("status"),
    }


def main() -> int:
    global _pipeline_start_monotonic
    _pipeline_start_monotonic = time.monotonic()

    _box("STAGE 0 · 抓盘口（OFFLINE_ONLY = True）")
    print(f"  OFFLINE_ONLY  : {OFFLINE_ONLY}")
    print(f"  deadline      : {DEADLINE_SECONDS}s 总时长熔断")
    print(f"  agent_calls   : {HARD_TOTAL_AGENT_CALLS} 次硬上限 (debate.py)")
    print(f"  turns/agent   : {HARD_MAX_TURNS_PER_AGENT} 次硬上限 (本脚本)")
    t0 = time.monotonic()
    snap = sporttery_snapshot()  # OFFLINE_ONLY 下走 input_matches.json
    fetch_elapsed = time.monotonic() - t0
    print(f"  fetch elapsed : {fetch_elapsed:.3f}s")
    print(f"  status        : {snap.get('status')}")
    print(f"  source        : {snap.get('source')}")
    print(f"  source_url    : {snap.get('source_url')}")
    print(f"  matches count : {len(snap.get('matches', []))}")
    if snap.get("fallback_reason"):
        print(f"  fallback_reason: {snap.get('fallback_reason')[:140]}")
    if snap.get("status") not in {"ok", "ok_local_fallback"}:
        print(f"  error         : {snap.get('error', '')[:200]}")
        return 2

    match = pick_match(snap)
    _box(f"STAGE 1 · 选定焦点战  →  {match.get('home')} vs {match.get('away')}")
    print(f"  league        : {match.get('league')}")
    print(f"  kickoff       : {match.get('match_date')} {match.get('match_clock')}")
    print(f"  market        : {match.get('market_name')} (code={match.get('market_code')})")
    print(f"  goal_line     : {match.get('goal_line') or '(no handicap)'}")
    print(f"  decimal odds  : H={match['decimal'][0]} D={match['decimal'][1]} A={match['decimal'][2]}")

    market_ctx = build_market_context(match, snap)

    _box("STAGE 2 · 装配 4 角 Soul + 真实 Anthropic 客户端")
    api_key = os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
    base_url = os.getenv("ANTHROPIC_BASE_URL") or None
    # 🚨 全员 Sonnet：绕开 Apex 物理队列瓶颈。
    # 即便 ANTHROPIC_DEFAULT_OPUS_MODEL 环境变量指向 Opus，
    # dry-run 内强制覆盖为 sonnet-4-6（避免中转商 Apex 队列卡死）。
    sonnet_model = (
        os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or "claude-sonnet-4-6"
    )
    model_apex = sonnet_model
    model_primary = sonnet_model
    if not api_key:
        print("  ERROR: ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY not set.")
        return 3
    print(f"  base_url      : {base_url or '(default)'}")
    print(f"  apex model    : {model_apex}      (CEO — 强制 Sonnet)")
    print(f"  primary model : {model_primary}  (CFO/CRO/Analyst — 强制 Sonnet)")

    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
        timeout=ANTHROPIC_HTTP_TIMEOUT,  # 🚨 35s 单点 HTTP 超时硬关
    )

    souls = {role: load_soul(role) for role in ("ceo", "cfo", "cro", "analyst")}
    print(f"  souls loaded  : {list(souls.keys())}")
    print(f"  http timeout  : {ANTHROPIC_HTTP_TIMEOUT}s 单点")
    print(f"  max_tokens    : {DEFAULT_MAX_TOKENS} 紧凑模式")

    # B方案：CRO/Analyst 不再有 data 工具回路，直接消费 prompt 里的 market_context
    agents = {
        "ceo":     AnthropicAgent(souls["ceo"],     client, model_apex,    toolbox=None),
        "cfo":     AnthropicAgent(souls["cfo"],     client, model_primary, toolbox=None),
        "cro":     AnthropicAgent(souls["cro"],     client, model_primary, toolbox=None),
        "analyst": AnthropicAgent(souls["analyst"], client, model_primary, toolbox=None),
    }
    registry = MockAgentRegistry(agents)

    _ = MockLedgerStorage(); _ = MockTickHost()  # 仅证明 Protocol 满足
    print("  mock storage + host : Protocol 满足（无 SQLite 依赖）")

    _box("STAGE 3 · 启动 DebateEngine（2 轮辩论，max_agent_calls=8）")
    debate = DebateEngine(
        registry,
        num_rounds=2,
        max_agent_calls=HARD_TOTAL_AGENT_CALLS,   # 🚨 全局熔断 8 次
        on_round_end=lambda rt, positions: print(
            f"\n  >>> Round {rt.value} 完成，收到 {len(positions)} 份立场 "
            f"(budget used: {debate._budget.used}/{debate._budget.limit}) <<<"
        ),
    )

    question = (
        f"针对今晚 {match.get('home')} vs {match.get('away')}（{match.get('league')}，"
        f"开赛 {match.get('match_date')} {match.get('match_clock')}）的比赛，"
        f"请给出最大概率的体彩出单策略。"
    )
    print(f"\n  question: {question}")

    try:
        result = debate.run(question, market_context=market_ctx)
    except DryRunDeadlineExceeded as exc:
        _box("‼️ 90s 总时长熔断")
        print(f"  reason: {exc}")
        return 7

    elapsed_total = time.monotonic() - _pipeline_start_monotonic
    print(f"\n  total elapsed : {elapsed_total:.2f}s "
          f"(budget used: {debate._budget.used}/{debate._budget.limit})")

    _box("STAGE 4 · 各轮发言摘要")
    for i, rnd in enumerate(result.rounds, 1):
        print(f"\n── Round {i} ───────────────────────────────────")
        for pos in rnd:
            _render_position(pos)

    _box("STAGE 5 · CEO 综合 (Synthesis)")
    syn = result.synthesis
    print(f"\n  recommended_option : {syn.recommended_option}")
    print(f"  consensus_claims   :")
    for c in syn.effective_consensus_claims():
        print(f"    • {c.text}")
    print(f"  key_tensions       : {syn.key_tensions}")
    print(f"  risk_flags         : {syn.risk_flags}")

    _box("STAGE 6 · CEO 最终决断 (CEODecision JSON)")
    dec = result.decision
    payload = {
        "match": f"{match.get('home')} vs {match.get('away')}",
        "decision": dec.decision,
        "issue_ticket": dec.issue_ticket,
        "scorelines": dec.scorelines,
        "stake_allocation_CNY": dec.stake_allocation,
        "stake_total_CNY": round(sum(dec.stake_allocation.values()), 2),
        "rationale_claims": [
            {
                "text": c.text,
                "evidence": [
                    {"src_type": s.source_type.value, "src_ref": s.source_ref}
                    for s in (c.evidence or [])
                ],
            }
            for c in (dec.rationale_claims or [])
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    _box("STAGE 7 · CFO 风控宪法自动校验")
    total = payload["stake_total_CNY"]
    envelope_ok = 100 <= total <= 500 if dec.issue_ticket else True
    print(f"  总注金 = {total} CNY")
    print(f"  100~500 包络通过: {envelope_ok}")
    if dec.issue_ticket and dec.stake_allocation:
        per = ", ".join(f"{k}={v}" for k, v in dec.stake_allocation.items())
        print(f"  分配: {per}")
    print(f"  scorelines 数量: {len(dec.scorelines)}  (≤3: {len(dec.scorelines) <= 3})")
    print(f"  issue_ticket: {dec.issue_ticket}")
    print()
    return 0 if envelope_ok else 1


if __name__ == "__main__":
    sys.exit(main())
