"""WC2026-Prediction · FastAPI 常驻量化服务。

把单次干跑脚本（``dry_run_pipeline.py``）改造成可常驻 Zeabur 的 Web 服务：
    GET  /                直接返回前端 SPA（static/index.html）
    GET  /api/health      存活探针
    GET  /api/match       读取 input_matches.json
    POST /api/match/update 写入新 POST /api/debate      启动一次辩论；返回 ``run_id``
    GET  /api/debate/{id} 拉取该 run 的进度 + 完整结果（前端轮询）

风控总开关（继承 dry_run_pipeline.py 完整经验）：
    OFFLINE_ONLY=True         — 不发任何 sporttery 请求
    HARD_TOTAL_AGENT_CALLS=8  — debate.py 内置硬上限
    HARD_MAX_TURNS_PER_AGENT=2
    DEADLINE_SECONDS=180
    ANTHROPIC_HTTP_TIMEOUT=60
    MAX_CONCURRENT_RUNS=1     — 同一时刻只允许 1 个 run；高频点击直接 429
    全员模型 = claude-sonnet-4-6
    System prompt 强制 minimal=True
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

# Windows 控制台中文兜底
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lottery_kernel.agents import compose_system_prompt, load_soul
from lottery_kernel.debate import (
    CircuitBreakerTripped,
    DebateEngine,
    HARD_TOTAL_AGENT_CALLS,
)
from lottery_kernel.models.debate_models import (
    AgentPosition,
    CEODecision,
    DebateRound,
    DebateSynthesis,
)


# ======================================================================
# 配置
# ======================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("wc2026")

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
MATCHES_FILE = ROOT / "input_matches.json"

# 风控常量（与 dry_run_pipeline.py 同源）
HARD_MAX_TURNS_PER_AGENT = 2
DEADLINE_SECONDS = 180
ANTHROPIC_HTTP_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 800
MAX_CONCURRENT_RUNS = 1

# 模型路由（任务要求：全员 Sonnet）
LOCKED_MODEL = "claude-sonnet-4-6"
LOCKED_BASE_URL = "https://www.zzzplus.com/v1"


# ======================================================================
# run 注册表（内存）
# ======================================================================


class RunStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"


class RunRecord(BaseModel):
    run_id: str
    match: dict
    status: str
    started_at: float
    finished_at: float | None = None
    budget_used: int = 0
    budget_limit: int = HARD_TOTAL_AGENT_CALLS
    events: list[dict] = Field(default_factory=list)
    result: dict | None = None
    error: str | None = None


_RUNS: dict[str, RunRecord] = {}
_RUNS_LOCK = threading.Lock()
_ACTIVE_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)


def _emit(rec: RunRecord, kind: str, payload: dict) -> None:
    rec.events.append({"t": time.time(), "kind": kind, **payload})


# ======================================================================
# Anthropic 智能体（精简版，继承 dry_run_pipeline 的 schema / 工具协议）
# ======================================================================


class _StructuredResp:
    def __init__(self, parsed: Any):
        self.parsed = parsed


def _pydantic_to_input_schema(model_cls: type[BaseModel]) -> dict:
    """复用 dry_run_pipeline.py 中的 schema 转换：保留 $defs、去 title、
    把 claims / recommendation 等加进 required。"""
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
    deprecated = {
        "AgentPosition": ["analysis"],
        "DebateSynthesis": ["consensus_position"],
        "CEODecision": ["rationale"],
    }
    required = {
        "AgentPosition": ["claims", "recommendation"],
        "DebateSynthesis": ["consensus_claims", "recommended_option"],
        "CEODecision": ["decision", "rationale_claims", "issue_ticket"],
    }
    name = model_cls.__name__
    props = schema.get("properties", {})
    for f in deprecated.get(name, []):
        props.pop(f, None)
    if name in required:
        req = set(schema.get("required", []))
        req.update(required[name])
        schema["required"] = sorted(req)
    schema.setdefault("additionalProperties", True)
    return schema


class AnthropicAgent:
    """绑定一个 Soul 的 Anthropic 智能体。强制 minimal=True。"""

    def __init__(
        self,
        soul,
        client: anthropic.Anthropic,
        model: str,
        deadline_monotonic: float,
        emit_fn,
    ):
        self.role = soul.role
        self.display_name = soul.display_name
        self.squad = soul.squad
        self._soul = soul
        self._client = client
        self._model = model
        self._system = compose_system_prompt(soul, minimal=True)
        self._deadline = deadline_monotonic
        self._emit = emit_fn

    def _enforce_deadline(self, stage: str) -> None:
        if time.monotonic() > self._deadline:
            raise TimeoutError(f"pipeline deadline exceeded at {stage}")

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
                "Populate ``claims`` with 3-5 atomic statements citing "
                "user_input / agent_memory. Do NOT write free text."
            ),
            "input_schema": _pydantic_to_input_schema(output_schema),
        }
        messages: list[dict] = [{"role": "user", "content": prompt}]
        force_submit = False

        for turn in range(HARD_MAX_TURNS_PER_AGENT):
            self._enforce_deadline(f"agent:{self.role}:turn{turn}")
            try:
                resp = self._client.messages.create(
                    model=self._model,
                    system=self._system,
                    tools=[submit_tool],
                    tool_choice={"type": "tool", "name": tool_name},
                    max_tokens=max_tokens,
                    messages=messages,
                )
            except anthropic.APITimeoutError:
                self._emit("agent_timeout", {"role": self.role, "timeout_s": ANTHROPIC_HTTP_TIMEOUT})
                fallback = output_schema()  # type: ignore[call-arg]
                if hasattr(fallback, "recommendation"):
                    fallback.recommendation = "(timeout)"
                if hasattr(fallback, "confidence"):
                    fallback.confidence = "低"
                return _StructuredResp(fallback)
            except anthropic.APIStatusError as exc:
                self._emit("agent_api_error", {"role": self.role, "status": exc.status_code})
                fallback = output_schema()  # type: ignore[call-arg]
                if hasattr(fallback, "recommendation"):
                    fallback.recommendation = f"(api {exc.status_code})"
                return _StructuredResp(fallback)
            except anthropic.APIError as exc:
                self._emit("agent_api_error", {"role": self.role, "error": type(exc).__name__})
                fallback = output_schema()  # type: ignore[call-arg]
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
                        parsed.recommendation = f"(schema invalid: {exc})"
                return _StructuredResp(parsed)

            # 没出 submit → 强制下一轮
            force_submit = True
            messages.append({"role": "user", "content": f"You did NOT call `{tool_name}`. Call it now."})

        fallback = output_schema()  # type: ignore[call-arg]
        if hasattr(fallback, "recommendation"):
            fallback.recommendation = "(exhausted turns)"
        return _StructuredResp(fallback)


class MockAgentRegistry:
    def __init__(self, agents: dict[str, AnthropicAgent]):
        self._agents = agents

    def get(self, role: str) -> AnthropicAgent:
        return self._agents[role]


# ======================================================================
# 数据 IO
# ======================================================================


_MATCHES_LOCK = threading.Lock()


def load_matches() -> dict:
    if not MATCHES_FILE.exists():
        return {"matches": []}
    with _MATCHES_LOCK:
        return json.loads(MATCHES_FILE.read_text(encoding="utf-8"))


def save_matches(payload: dict) -> None:
    with _MATCHES_LOCK:
        MATCHES_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ======================================================================
# 辩论协程
# ======================================================================


def _build_market_context(match: dict) -> dict:
    dec = match.get("decimal", [None, None, None])
    dec_had = match.get("decimal_HAD") or []
    ctx = {
        "match": f"{match.get('home')} vs {match.get('away')}",
        "league": match.get("league"),
        "kickoff": f"{match.get('match_date', '')} {match.get('match_clock', '')}".strip(),
        "market_code": match.get("market_code"),
        "market_name": match.get("market_name"),
        "goal_line": match.get("goal_line") or "(no handicap)",
        "decimal_HHAD_HDA": dec,
        "decimal_HAD_HDA": dec_had,
        "implied_probs_raw_pct": [
            round(100 / o, 1) if o else None for o in (dec or [])
        ],
    }
    return ctx


def _run_debate_sync(rec: RunRecord, api_key: str) -> None:
    """在工作线程里跑 DebateEngine.run；写回 rec。"""
    rec.status = RunStatus.RUNNING
    rec.started_at = time.time()
    deadline = time.monotonic() + DEADLINE_SECONDS

    def emit(kind: str, payload: dict) -> None:
        with _RUNS_LOCK:
            _emit(rec, kind, payload)

    try:
        client = anthropic.Anthropic(
            api_key=api_key,
            base_url=LOCKED_BASE_URL,
            timeout=ANTHROPIC_HTTP_TIMEOUT,
        )
        souls = {r: load_soul(r) for r in ("ceo", "cfo", "cro", "analyst")}
        agents = {
            r: AnthropicAgent(souls[r], client, LOCKED_MODEL, deadline, emit)
            for r in ("ceo", "cfo", "cro", "analyst")
        }
        registry = MockAgentRegistry(agents)

        def on_round_end(rt: DebateRound, positions: list[AgentPosition]) -> None:
            emit("round_end", {
                "round": rt.value,
                "positions": [
                    {
                        "role": p.agent_role,
                        "display_name": p.agent_name,
                        "squad": p.squad,
                        "confidence": p.confidence,
                        "recommendation": p.recommendation,
                        "claims": [
                            {"text": c.text, "evidence": [
                                {"src_type": s.source_type.value, "src_ref": s.source_ref}
                                for s in (c.evidence or [])
                            ]}
                            for c in (p.claims or [])
                        ],
                    }
                    for p in positions
                ],
                "budget_used": registry.get("ceo")._client and 0,  # placeholder
            })

        debate = DebateEngine(
            registry,
            num_rounds=2,
            max_agent_calls=HARD_TOTAL_AGENT_CALLS,
            on_round_end=on_round_end,
        )
        emit("started", {
            "match": f"{rec.match.get('home')} vs {rec.match.get('away')}",
            "model": LOCKED_MODEL,
            "deadline_s": DEADLINE_SECONDS,
        })

        question = (
            f"针对 {rec.match.get('home')} vs {rec.match.get('away')}"
            f"（{rec.match.get('league')}，{rec.match.get('match_date')} "
            f"{rec.match.get('match_clock')}）的多类别概率分布下的资金分配建议，"
            f"请给出最大概率的结构化决策方案。"
        )
        market_ctx = _build_market_context(rec.match)
        result = debate.run(question, market_context=market_ctx)
        rec.budget_used = debate._budget.used

        dec = result.decision
        rec.result = {
            "match": f"{rec.match.get('home')} vs {rec.match.get('away')}",
            "decision": dec.decision,
            "issue_ticket": dec.issue_ticket,
            "scorelines": list(dec.scorelines or []),
            "stake_allocation_CNY": dict(dec.stake_allocation or {}),
            "stake_total_CNY": round(sum((dec.stake_allocation or {}).values()), 2),
            "rationale_claims": [
                {"text": c.text, "evidence": [
                    {"src_type": s.source_type.value, "src_ref": s.source_ref}
                    for s in (c.evidence or [])
                ]}
                for c in (dec.rationale_claims or [])
            ],
            "synthesis": {
                "recommended_option": result.synthesis.recommended_option,
                "consensus_claims": [
                    {"text": c.text} for c in result.synthesis.effective_consensus_claims()
                ],
                "key_tensions": list(result.synthesis.key_tensions or []),
                "risk_flags": list(result.synthesis.risk_flags or []),
            },
            "agents_participated": list(result.agents_participated or []),
            "budget_used": debate._budget.used,
            "budget_limit": debate._budget.limit,
        }
        rec.status = RunStatus.DONE
        emit("done", {"budget_used": debate._budget.used})
    except TimeoutError as exc:
        rec.status = RunStatus.TIMEOUT
        rec.error = str(exc)
        emit("deadline", {"error": str(exc)})
    except CircuitBreakerTripped as exc:
        rec.status = RunStatus.FAILED
        rec.error = str(exc)
        emit("circuit_breaker", {"used": exc.used, "limit": exc.limit, "stage": exc.stage})
    except Exception as exc:  # noqa: BLE001
        rec.status = RunStatus.FAILED
        rec.error = f"{type(exc).__name__}: {exc}"
        emit("error", {"error": rec.error, "tb": traceback.format_exc()[-800:]})
    finally:
        rec.finished_at = time.time()
        _ACTIVE_SEM.release()
        log.info("run %s finished: status=%s budget=%d", rec.run_id, rec.status, rec.budget_used)


# ======================================================================
# FastAPI
# ======================================================================

app = FastAPI(title="WC2026-Prediction", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "index.html missing"}, status_code=500)
    return FileResponse(str(idx))


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "active_runs": MAX_CONCURRENT_RUNS - _ACTIVE_SEM._value,
        "model": LOCKED_MODEL,
        "base_url": LOCKED_BASE_URL,
        "deadline_s": DEADLINE_SECONDS,
        "max_agent_calls": HARD_TOTAL_AGENT_CALLS,
    }


@app.get("/api/match")
def get_matches():
    data = load_matches()
    return {"ok": True, **data}


class MatchUpdatePayload(BaseModel):
    home: str
    away: str
    league: str = "世界杯"
    match_date: str = ""
    match_clock: str = ""
    market_code: str = "HHAD"
    market_name: str = "让球胜平负"
    goal_line: str = "-1.00"
    decimal: list[float]
    decimal_HAD: list[float] = Field(default_factory=list)
    index: int = 0


@app.post("/api/match/update")
def update_match(payload: MatchUpdatePayload):
    if len(payload.decimal) != 3:
        raise HTTPException(400, "decimal must have 3 values [H,D,A]")
    if payload.decimal_HAD and len(payload.decimal_HAD) != 3:
        raise HTTPException(400, "decimal_HAD must have 3 values [H,D,A] or be empty")
    data = load_matches()
    matches = data.get("matches") or []
    entry = {
        "home": payload.home.strip(),
        "away": payload.away.strip(),
        "league": payload.league.strip() or "世界杯",
        "match_date": payload.match_date.strip(),
        "match_clock": payload.match_clock.strip(),
        "market_code": payload.market_code.strip() or "HHAD",
        "market_name": payload.market_name.strip() or "让球胜平负",
        "goal_line": payload.goal_line.strip() or "-1.00",
        "decimal": payload.decimal,
        "decimal_HAD": payload.decimal_HAD,
        "source": "web ui update",
    }
    if 0 <= payload.index < len(matches):
        matches[payload.index] = entry
    else:
        matches.append(entry)
    data["matches"] = matches
    save_matches(data)
    return {"ok": True, "saved": entry, "total": len(matches)}


class DebateStartPayload(BaseModel):
    api_key: str
    match_index: int = 0


@app.post("/api/debate")
def start_debate(payload: DebateStartPayload):
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(400, "api_key required")

    data = load_matches()
    matches = data.get("matches") or []
    if not matches:
        raise HTTPException(400, "no matches in input_matches.json")
    if payload.match_index < 0 or payload.match_index >= len(matches):
        raise HTTPException(400, f"match_index out of range (0..{len(matches)-1})")
    match = matches[payload.match_index]

    if not _ACTIVE_SEM.acquire(blocking=False):
        raise HTTPException(429, "another debate is running; please wait")

    run_id = uuid.uuid4().hex[:12]
    rec = RunRecord(
        run_id=run_id,
        match=match,
        status=RunStatus.PENDING,
        started_at=time.time(),
    )
    with _RUNS_LOCK:
        _RUNS[run_id] = rec

    thread = threading.Thread(
        target=_run_debate_sync,
        args=(rec, api_key),
        name=f"debate-{run_id}",
        daemon=True,
    )
    thread.start()
    return {"ok": True, "run_id": run_id, "match": match}


@app.get("/api/debate/{run_id}")
def poll_debate(run_id: str):
    with _RUNS_LOCK:
        rec = _RUNS.get(run_id)
        if rec is None:
            raise HTTPException(404, "run_id not found")
        # 浅变
        return {
            "ok": True,
            "run_id": rec.run_id,
            "status": rec.status,
            "started_at": rec.started_at,
            "finished_at": rec.finished_at,
            "budget_used": rec.budget_used,
            "budget_limit": rec.budget_limit,
            "events": list(rec.events),
            "result": rec.result,
            "error": rec.error,
        }


# ======================================================================
# uvicorn 入口
# ======================================================================

def run_server() -> None:
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    log.info("WC2026 server starting on :%d (model=%s)", port, LOCKED_MODEL)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    run_server()
