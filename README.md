# WC2026-Prediction

> 四角专家辩论 · 体彩量化决策系统 · FastAPI 常驻 + 暗黑量化单页前端

从 Kompany 主干剥离 4 个核心基建（`autonomy` / `ledger` / `debate` / `ticker`），通过 PEP 544 Protocol 解耦；4 个角色（CEO / CFO / CRO / Analyst）通过 YAML Soul 加载；FastAPI 包装为常驻 Web 服务；Zeabur 一键起飞。

---

## ▌ 在线交互（前端 SPA）

`GET /` 直接返回 `static/index.html`，四区暗黑量化单页：

| 区域 | 功能 | 数据流 |
|---|---|---|
| 01 · 密钥配置 | API Key 输入 + localStorage 持久化 | 浏览器本地，不上传服务器存储 |
| 02 · 数据注入 | 主队/客队/让球数/HHAD 赔率/HAD 赔率 表单 | `POST /api/match/update` 写 `input_matches.json` |
| 03 · 拉起辩论 | 单按钮触发 + 实时 budget/elapsed 指示 | `POST /api/debate` 拿 `run_id` |
| 04 · 大黑板 + CEO 终审 | 1.5s 轮询 → 流式气泡 + 终审面板 | `GET /api/debate/{id}` 增量渲染 |

样式：JetBrains Mono / 霓虹绿青(`#00ffcc`) / 风控橙(`#ffae3d`) / 反向红(`#ff5d6c`) / CEO 紫(`#d6a3ff`) / fadeIn 动画 / pulse 状态点 / 无外部 CDN 依赖。

---

## ▌ FastAPI 路由（5 个）

```
GET  /                     c/index.html (SPA)
GET  /api/health              → 状态 + 风控参数（Zeabur health check 钩点）
GET  /api/match               → 读 input_matches.json
POST /api/match/update        → 写单场盘口
    body: {home, away, match_date, match_clock, goal_line,
           market_code, decimal:[H,D,A], decimal_HAD:[H,D,A], index}
POST /api/debate              → 启动一次辩论（异步线程 + run_id）
    body: {api_key, match_index}
GET  /api/debate/{run_id}     → 轮询进度 / 完整结果
    返回: {status, budget_used/limit, events[], result?, error?}
```

`/docs` 自动 Swagger。

---

## ▌ 多层防爆风控锁（继承阶段二/三压力测试经验）

| 层 | 默认值 | 位置 | 作用 |
|---|---|---|---|
| `MAX_CONCURRENT_RUNS` | 1 | `main.py` `BoundedSemaphore` | 重复点击立即 HTTP 429 |
| `HARD_TOTAL_AGENT_CALLS` | 8 | `lottery_kernel/debate.py` | 单次 debate.run LLM 调用硬熔断 |
| `HARD_MAX_TURNS_PER_AGENT` | 2 | `main.py` | 单 agent 内 LLM-turn 上限 |
| `DEADLINE_SECONDS` | 180 | `main.py` | 全 pipeline 总时长 |
| `ANTHROPIC_HTTP_TIMEOUT` | 60 s | `main.py` | 单点 HTTP 超时 |
| `APITimeoutError / APIStatusError / APIError` 三层捕获 | 启用 | `AnthropicAgent.call_structured` | 单 agent 失败降级，不炸全场 |
| `OFFLINE_ONLY` | `True` | `lottery_kernel/tools/odds_tools.py` | 拒绝任何 urllib 调用 |
| `MIN_TICK_INTERVAL_SECONDS` / `MAX_TICKS_PER_SESSION` | 1 s / 10000 | `lottery_kernel/ticker.py` | 心跳防 0 延迟空转 |
| `vault_key` fail-loud | 启用 | `lottery_kernel/boot.py` | 修复 Kompany 审计发现 S1 |
| `LOCKED_MODEL` | `claude-sonnet-4-6` | `main.py` | 全员强制 Sonnet 绕开 Apex 队列 |
| `compose_system_prompt(..., minimal=True)` | 启用 | `lottery_kernel/agents/__init__.py` | 系统段 ~140 tokens 脱水 |

历史教训：未加 `_AgentCallBudget` 前，单次 dry-run 触发链式调用 170+ 次。本系统从 HTTP 入口（FastAPI）到内核（DebateEngine）共 5 道独立闸门，账单受控。

---

## ▌ 目录结构

```
WC2026-Prediction/
├── main.py                    FastAPI 常驻服务 (459 行)
├── dry_run_pipeline.py        阶段二单跑脚本（保留作回滚）
├── input_matches.json         OFFLINE_ONLY 单一数据源
├── requirements.txt           anthropic / pydantic / pyyaml / fastapi / uvicorn[standard]
├── zeabur.json                Zeabur 部署配置（healthcheck=/api/health）
├── static/
│   └── index.html             暗黑量化 SPA（无外部 CDN 依赖）
└── lottery_kernel/
    ├── boot.py                装配器 + vault_key fail-loud
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
            ├── ceo.yaml       总操盘手（commanding · brief）
            ├── cfo.yaml       体彩风控官（100~500 CNY + 50/50 对冲）
            ├── cro.yaml       庄家心理精算师（大众心理向量）
            └── analyst.yaml   基本面精算师（博彩数据向量）
```

---

## ▌ 本地启动

```bash
pip install -r requirements.txt
python -X utf8 main.py
# 浏览器打开 http://localhost:8000
# 1) 粘 API Key → 保存
# 2) 改盘口 → 更新
# 3) 拉起四角专家辩论
```

## ▌ Zeabur 部署

```
build:  pip install --no-cache-dir -r requirements.txt
start:  python -X utf8 main.py
health: GET /api/health
env:    PORT (由 Zeabur 注入)
```

部署后浏览器打开 Zeabur 给的域名即可。**API Key 不存服务器**，每个用户用自己的 `localStorage`。

---

## ▌ 技术演进时间线

- **阶段一** — Kompany `core/engine.py` 安全合规审计，发现 S1（vault_key 静默回退）等问题；评测三个开源世界杯预测项目，选定 `worldcut-2026` 作为爬取层来源。
- **阶段二** — 剥离 `autonomy / ledger / debate / ticker` 为独立 `lottery_kernel` 包；4 个 PEP 544 Protocol 端口解耦；编写 4 个角色 Soul YAML；从 `worldcut-2026` 抽取赔率工具线程化包装；修复 S1。
- **阶段三** — 真实 LLM 干跑暴露中转网络瓶颈，建立 5 道独立防爆锁（budget / turns / deadline / http timeout / 离线数据）；OFFLINE_ONLY 离线数据源固化；脱水 system prompt 协议。
- **阶段四** — 改造为 FastAPI 常驻 Web 服务 + 暗黑量化 SPA + Zeabur 配置；保留所有阶段二/三的风控锁；前后端契约固化。
