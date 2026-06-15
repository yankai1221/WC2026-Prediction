# lottery_kernel

足彩多智能体辩论 + 风控决策内核。从 Kompany 主干剥离 4 个核心基建（`autonomy`/`ledger`/`debate`/`ticker`），通过 PEP 544 Protocol 解耦；4 个角色（CEO / CFO / CRO / Analyst）通过 YAML Soul 加载。

## 风控护栏（多层熔断）

| 层 | 默认值 | 来源 |
|---|---|---|
| `OFFLINE_ONLY` | `True` | `lottery_kernel/tools/odds_tools.py` —— 拒绝任何 urllib 调用 |
| `_AgentCallBudget` | 8 次/run | `lottery_kernel/debate.py` —— 单次 debate.run 内 LLM 调用硬上限 |
| `HARD_MAX_TURNS_PER_AGENT` | 2 turn | `dry_run_pipeline.py` —— 单 agent 内 LLM-turn 上限 |
| `DEADLINE_SECONDS` | 180s | `dry_run_pipeline.py` —— 全 pipeline 总时长熔断 |
| `ANTHROPIC_HTTP_TIMEOUT` | 60s | `dry_run_pipeline.py` —— 单点 HTTP 超时 |
| `MIN_TICK_INTERVAL_SECONDS` / `MAX_TICKS_PER_SESSION` | 1s / 10000 | `lottery_kernel/ticker.py` —— 心跳防 0 延迟空转 |

## 目录结构

```
lottery_kernel/
├── boot.py                装配器 + vault_key fail-loud (修复审计 S1)
├── autonomy.py            AutonomyGate
├── ledger.py              资金账本（含 record_stake / record_payout）
├── debate.py              4 角辩论协调器 + 熔断器
├── ticker.py              心跳循环
├── ports/                 PEP 544 Protocol 端口
├── models/                Claim / Source / CEODecision 等
├── tools/odds_tools.py    odds_snapshot / match_odds (OFFLINE_ONLY)
└── agents/
    ├── __init__.py        Soul 加载器 + compose_system_prompt
    └── souls/
        ├── ceo.yaml       总操盘手
        ├── cfo.yaml       体彩风控官（100~500 CNY + 50/50 对冲）
        ├── cro.yaml       庄家心理精算师（大众心理向量）
        └── analyst.yaml   基本面精算师（博彩数据向量）

dry_run_pipeline.py        端到端干跑入口
input_matches.json         OFFLINE_ONLY 模式的数据源
```

## 运行

```bash
pip install -r requirements.txt
export ANTHROPIC_AUTH_TOKEN=sk-...
python -X utf8 dry_run_pipeline.py
```

OFFLINE_ONLY=True 下永远不会发起 sporttery 网络请求；比赛数据来自 `input_matches.json`。
