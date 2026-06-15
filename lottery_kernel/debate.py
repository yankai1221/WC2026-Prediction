"""DebateEngine —— 多智能体辩论协调器。

从 Kompany core/debate.py 剥离：
- ``AgentRegistry`` 改为 ``AgentRegistryLike`` Protocol（鸭子类型）
- STAGE_PROFILES 精简为 4 角色阵容（ceo / cfo / cro / analyst）
- CoS 角色合并到 CEO 自身：CEO 既做综合也做最终裁决
- 调试 hook 通过 ``on_round_end`` 暴露给宿主，不背 EventHub

🚨 防爆风控锁（用户授权 ACK，2026-06-15）：
- ``HARD_TOTAL_AGENT_CALLS = 8``：单次 debate.run() 内对
  ``registry.get(role).call_structured(...)`` 的累计调用次数硬上限。
  上限到达即 raise ``CircuitBreakerTripped``，绝不允许继续 LLM 计费。
- 计数粒度：每个 round 内每个 debater 一次 + synthesize 一次 + decide 一次。
  正常路径 = 2 round × 3 debater + 1 synth + 1 decide = 8（刚好用完）。
- 历史教训：先前没有该熔断，单场 dry-run 触发 LLM 链式调用 170+ 次。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from lottery_kernel.models.debate_models import (
    AgentPosition,
    CEODecision,
    Claim,
    DebateResult,
    DebateRound,
    DebateSynthesis,
    Source,
    SourceType,
)
from lottery_kernel.ports.agent_registry import AgentRegistryLike

log = logging.getLogger(__name__)


# ======================================================================
# 风控总开关
# ======================================================================

HARD_TOTAL_AGENT_CALLS = 8
"""单次 debate.run() 内的 agent.call_structured 累计调用硬上限。"""


class CircuitBreakerTripped(RuntimeError):
    """LLM 调用次数硬熔断 —— 立刻停止辩论，向上冒泡。"""

    def __init__(self, used: int, limit: int, stage: str):
        self.used = used
        self.limit = limit
        self.stage = stage
        super().__init__(
            f"DebateEngine circuit breaker tripped: "
            f"{used}/{limit} agent calls used at stage={stage}. "
            f"Aborting to prevent runaway LLM billing."
        )


class _AgentCallBudget:
    """计数 + 熔断的小工具，每次 agent 调用前显式 check。"""

    def __init__(self, limit: int = HARD_TOTAL_AGENT_CALLS):
        self.limit = max(1, int(limit))
        self.used = 0

    def before_call(self, stage: str) -> None:
        if self.used >= self.limit:
            raise CircuitBreakerTripped(self.used, self.limit, stage)
        self.used += 1
        log.debug("agent call %d/%d at %s", self.used, self.limit, stage)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


CLAIMS_SCHEMA_HINT = (
    "Output schema:\n"
    "- ``claims``: list of atomic factual statements; split compound\n"
    "  statements so each can be cited individually.\n"
    "- Each claim has ``evidence: list[Source]`` citing concrete sources:\n"
    "  source_type ∈ {user_input, template_default, ledger_entry,\n"
    "  agent_memory, audit_event, odds_snapshot, sporttery_feed, inferred};\n"
    "  ``source_ref`` is the entry id / tool call id / field name;\n"
    "  ``claim_supported`` is a short label.\n"
    "- Claims marked inferred-only will be flagged in the UI and will NOT\n"
    "  be promoted to long-term agent memory. Prefer odds_snapshot /\n"
    "  sporttery_feed whenever you can.\n"
    "- The deprecated ``analysis`` string field MAY be left empty."
)


# 4 角阵容：CEO 召集 + 决断；CFO 风控；CRO 反向心理；Analyst 基本面。
# CEO 不参与辩论 round（只在 synthesize/decide 阶段说话）。
LOTTERY_DEBATERS = ["cfo", "cro", "analyst"]
LOTTERY_NON_DEBATERS = {"ceo"}


RoundHook = Callable[[DebateRound, list[AgentPosition]], None]


class DebateEngine:
    """足彩四角辩论协调器。

    使用方式::

        engine = DebateEngine(registry, num_rounds=2)
        result = engine.run("巴西 vs 阿根廷 该如何出单？")
        result.decision.issue_ticket  # True → CEO 下出单令
    """

    def __init__(
        self,
        registry: AgentRegistryLike,
        *,
        num_rounds: int = 2,
        on_round_end: Optional[RoundHook] = None,
        max_agent_calls: int = HARD_TOTAL_AGENT_CALLS,
    ):
        if num_rounds < 1 or num_rounds > 3:
            raise ValueError("num_rounds must be 1..3")
        self._registry = registry
        self._num_rounds = num_rounds
        self._on_round_end = on_round_end
        self._max_agent_calls = max_agent_calls
        self._budget = _AgentCallBudget(max_agent_calls)  # 每次 run 重建

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(
        self,
        question: str,
        market_context: dict | None = None,
        directive_id: str | None = None,
    ) -> DebateResult:
        # 🚨 每次 run 重置预算计数 —— 防爆风控锁
        self._budget = _AgentCallBudget(self._max_agent_calls)
        log.info(
            "DebateEngine.run start: budget=%d agent calls",
            self._max_agent_calls,
        )

        all_rounds: list[list[AgentPosition]] = []
        synthesis = DebateSynthesis()
        decision = CEODecision()

        try:
            # Round 1: 独立立场
            r1 = self._run_round(
                DebateRound.POSITION, question, [], directive_id, market_context
            )
            all_rounds.append(r1)
            self._emit(DebateRound.POSITION, r1)

            # Round 2: 反驳
            if self._num_rounds >= 2:
                r2 = self._run_round(
                    DebateRound.REBUTTAL, question, all_rounds,
                    directive_id, market_context,
                )
                all_rounds.append(r2)
                self._emit(DebateRound.REBUTTAL, r2)

            # Round 3: 收敛（仅 3-round 配置）
            if self._num_rounds >= 3:
                r3 = self._run_round(
                    DebateRound.CONVERGENCE, question, all_rounds,
                    directive_id, market_context,
                )
                all_rounds.append(r3)
                self._emit(DebateRound.CONVERGENCE, r3)

            # CEO 综合 + 决断（CEO 兼任 CoS）
            synthesis = self._synthesize(question, all_rounds, directive_id)
            decision = self._ceo_decide(
                question, all_rounds, synthesis, directive_id
            )
        except CircuitBreakerTripped as exc:
            log.error(
                "circuit breaker tripped: %s (used=%d/%d at %s)",
                exc, exc.used, exc.limit, exc.stage,
            )
            # 标记决策为熔断，issue_ticket=False，不出单
            decision = CEODecision(
                decision=(
                    f"[CIRCUIT BREAKER] {exc.stage} stage aborted "
                    f"({exc.used}/{exc.limit} LLM calls used)"
                ),
                issue_ticket=False,
            )

        agents_part = [p.agent_role for p in (all_rounds[0] if all_rounds else [])]
        return DebateResult(
            question=question,
            rounds=all_rounds,
            synthesis=synthesis,
            decision=decision,
            agents_participated=agents_part,
        )

    # ------------------------------------------------------------------
    # Round
    # ------------------------------------------------------------------

    def _run_round(
        self,
        round_type: DebateRound,
        question: str,
        prior_rounds: list[list[AgentPosition]],
        directive_id: str | None,
        market_context: dict | None,
    ) -> list[AgentPosition]:
        positions: list[AgentPosition] = []
        context = self._format_prior_rounds(prior_rounds)
        market_text = self._format_market(market_context)

        action_label = {
            DebateRound.POSITION: "debate_round_1",
            DebateRound.REBUTTAL: "debate_round_2",
            DebateRound.CONVERGENCE: "debate_round_3",
        }.get(round_type, "debate_round")

        for role in LOTTERY_DEBATERS:
            agent = self._registry.get(role)
            prompt = self._build_round_prompt(
                round_type, question, context, market_text, role
            )
            # 🚨 风控熔断：每次调用前 check 预算
            self._budget.before_call(f"{round_type.value}:{role}")
            resp = agent.call_structured(
                prompt=prompt,
                output_schema=AgentPosition,
                directive_id=directive_id,
                max_tokens=800,
                action_type=action_label,
            )
            pos: AgentPosition = resp.parsed
            pos.agent_role = role
            pos.agent_name = getattr(agent, "display_name", role.upper())
            pos.squad = getattr(agent, "squad", "executive")
            pos.round = round_type
            positions.append(pos)
        return positions

    # ------------------------------------------------------------------
    # 合议 + 决断
    # ------------------------------------------------------------------

    def _synthesize(
        self,
        question: str,
        all_rounds: list[list[AgentPosition]],
        directive_id: str | None,
    ) -> DebateSynthesis:
        ceo = self._registry.get("ceo")
        context = self._format_prior_rounds(all_rounds)
        prompt = (
            f"The team debated this lottery question:\n\n"
            f'"{question}"\n\n'
            f"All positions:\n\n{context}\n\n"
            "As CEO acting as chair, synthesize the debate.\n"
            "List consensus_claims (atomic factual statements with cited\n"
            "evidence), key_tensions (short labels), recommended_option\n"
            "(headline pick), risk_flags. Be neutral; surface tradeoffs.\n\n"
            + CLAIMS_SCHEMA_HINT
        )
        # 🚨 风控熔断
        self._budget.before_call("synthesize")
        resp = ceo.call_structured(
            prompt=prompt,
            output_schema=DebateSynthesis,
            directive_id=directive_id,
            max_tokens=800,
            action_type="debate_synthesis",
        )
        return resp.parsed

    def _ceo_decide(
        self,
        question: str,
        all_rounds: list[list[AgentPosition]],
        synthesis: DebateSynthesis,
        directive_id: str | None,
    ) -> CEODecision:
        ceo = self._registry.get("ceo")
        context = self._format_prior_rounds(all_rounds)
        consensus_text = self._format_claim_block(synthesis.effective_consensus_claims())
        prompt = (
            f'The executive team debated: "{question}"\n\n'
            f"Debate positions:\n{context}\n\n"
            f"Synthesis:\n- Consensus:\n{consensus_text}\n"
            f"- Tensions: {', '.join(synthesis.key_tensions)}\n"
            f"- Recommended: {synthesis.recommended_option}\n"
            f"- Risks: {', '.join(synthesis.risk_flags)}\n\n"
            "As CEO, make the final lottery decision. Be decisive.\n"
            "- ``decision`` is the headline verdict (one line).\n"
            "- ``issue_ticket=true`` ONLY if CFO's risk envelope and CRO's\n"
            "  trap-check both pass. Otherwise false.\n"
            "- ``scorelines``: at most 3 比分 like ['1-1', '2-1', '0-1'].\n"
            "- ``stake_allocation``: CNY allocation per leg, must sum to\n"
            "  the CFO-approved envelope (100~500 CNY hard cap).\n"
            "- ``rationale_claims``: atomic factual statements with cited evidence.\n\n"
            + CLAIMS_SCHEMA_HINT
        )
        # 🚨 风控熔断
        self._budget.before_call("decide")
        resp = ceo.call_structured(
            prompt=prompt,
            output_schema=CEODecision,
            directive_id=directive_id,
            max_tokens=800,
            action_type="debate_decision",
        )
        return resp.parsed

    # ------------------------------------------------------------------
    # Prompt 组装
    # ------------------------------------------------------------------

    def _build_round_prompt(
        self,
        round_type: DebateRound,
        question: str,
        context: str,
        market_text: str,
        role: str,
    ) -> str:
        if round_type == DebateRound.POSITION:
            body = (
                f'CEO asks: "{question}"\n\n'
                f"Market context:\n{market_text}\n\n"
                f"Provide your independent position as {role.upper()}.\n"
                "Produce 3-5 atomic claims and a concrete recommendation,\n"
                "plus your confidence level (极高/高/中高/中/低)."
            )
        elif round_type == DebateRound.REBUTTAL:
            body = (
                f'CEO asks: "{question}"\n\n'
                f"Market context:\n{market_text}\n\n"
                f"Prior positions:\n{context}\n\n"
                f"As {role.upper()}, review all positions.\n"
                "Acknowledge valid points by name, challenge points you\n"
                "disagree with, update your claim list if warranted.\n"
                "Cite the source of every factual claim you add."
            )
        else:  # CONVERGENCE
            body = (
                f'CEO asks: "{question}"\n\n'
                f"Market context:\n{market_text}\n\n"
                f"Prior rounds:\n{context}\n\n"
                f"As {role.upper()}, move toward consensus.\n"
                "State concessions and any non-negotiable hard lines.\n"
                "Cite the source of any new factual claim."
            )
        return body + "\n\n" + CLAIMS_SCHEMA_HINT

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _emit(self, round_type: DebateRound, positions: list[AgentPosition]) -> None:
        if self._on_round_end is not None:
            try:
                self._on_round_end(round_type, positions)
            except Exception:
                # 调试 hook 是 best-effort，绝不能影响主流程。
                # 这里区别于 S1 静默回退：失败的是观测层，不是凭据层。
                pass

    @staticmethod
    def _format_market(market_context: dict | None) -> str:
        if not market_context:
            return "(no market snapshot supplied)"
        lines: list[str] = []
        for k, v in market_context.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)

    @staticmethod
    def _format_prior_rounds(rounds: list[list[AgentPosition]]) -> str:
        if not rounds:
            return "(no prior positions)"
        parts: list[str] = []
        for i, rnd in enumerate(rounds, 1):
            parts.append(f"--- Round {i} ---")
            for pos in rnd:
                claim_lines = DebateEngine._format_claim_block(pos.effective_claims())
                parts.append(
                    f"[{pos.agent_name} ({pos.squad})] "
                    f"Recommendation: {pos.recommendation}\n"
                    f"Claims:\n{claim_lines}\n"
                    f"Confidence: {pos.confidence}"
                )
        return "\n\n".join(parts)

    @staticmethod
    def _format_claim_block(claims: list[Claim]) -> str:
        if not claims:
            return "  (no claims)"
        lines: list[str] = []
        for claim in claims:
            sources = [
                s.source_ref or s.source_type.value
                for s in claim.evidence
                if s.source_type != SourceType.INFERRED
            ]
            marker = "  ▸" if sources else "  ⚠"
            src_part = f" [{', '.join(sources)}]" if sources else ""
            lines.append(f"{marker} {claim.text}{src_part}")
        return "\n".join(lines)


__all__ = [
    "CLAIMS_SCHEMA_HINT",
    "DebateEngine",
    "LOTTERY_DEBATERS",
    "LOTTERY_NON_DEBATERS",
    "RoundHook",
    "HARD_TOTAL_AGENT_CALLS",
    "CircuitBreakerTripped",
]
