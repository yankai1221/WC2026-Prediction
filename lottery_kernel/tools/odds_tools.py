"""体彩数据工具箱 —— 给 Analyst 注入实时盘口与基本面数据。

🚨 **OFFLINE_ONLY 模式（用户授权 ACK）** 🚨
本模块当前**完全禁用网络**。``OFFLINE_ONLY=True`` 是顶层风控开关，
所有暴露给智能体的工具（odds_snapshot / match_odds / sporttery_match）
在该模式下直接读取项目根目录的 ``input_matches.json``，绝不发起
任何 urllib / socket 调用。

历史背景：
- 国内彩票中心网关在世界杯期间反爬 → urllib 蜜罐 socket 永挂；
- 此前 ThreadPoolExecutor + 同步 urllib 组合发生事件循环死锁，
  导致 LLM 链式调用 170+ 次刷账单事故。

数据源契约（input_matches.json）：
    {"matches": [{home, away, league, match_date, match_clock,
                  market_code, market_name, goal_line,
                  decimal: [H, D, A]}] urllib 函数（fetch_json_request_cached 等）保留为 dead code，
便于将来在生产环境（有合规代理）下显式打开 OFFLINE_ONLY=False。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import error, parse, request


# ======================================================================
# 风控总开关 —— 改这个值之前请确认有合规代理 + 账单上限
# ======================================================================

OFFLINE_ONLY = True


print(
    "[lottery_kernel.odds_tools] OFFLINE_ONLY=True — 所有数据来自 "
    "input_matches.json；网络函数已被短路。"
)


# ----------------------------------------------------------------------
# 环境配置（修复容器 connection refused：默认 SPORTTERY_HTTP_PROXY = ""）
# ----------------------------------------------------------------------

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_BASE_URL = os.getenv(
    "ODDS_API_BASE_URL", "https://api.the-odds-api.com/v4"
).rstrip("/")

SPORTTERY_HTTP_PROXY = os.getenv("SPORTTERY_HTTP_PROXY", "").strip()
SPORTTERY_PROXY_URL = os.getenv("SPORTTERY_PROXY_URL", "").strip()
SPORTTERY_PROXY_URLS = [
    item.strip()
    for item in os.getenv("SPORTTERY_PROXY_URLS", "").split(",")
    if item.strip()
]
SPORTTERY_OFFICIAL_URL = (
    "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry"
    "?clientCode=3001"
)

USER_AGENT_GENERIC = "lottery-kernel/0.1 (+sporttery-feed)"
USER_AGENT_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ThreadPoolExecutor —— 把阻塞 urllib 移到工作线程，绝不卡死 ticker。
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="lottery-odds",
)

# 进程内简易 TTL 缓存（替代 worldcut 全局 DATA_CACHE）
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


# ----------------------------------------------------------------------
# 通用 HTTP（从 worldcut server.py:1162 抽取）
# ----------------------------------------------------------------------


def fetch_json_request_cached(
    url: str,
    headers: dict | None = None,
    ttl_seconds: int = 900,
    timeout_seconds: int = 25,
    proxy_url: str = "",
) -> dict:
    """带 TTL 缓存、可选代理的 JSON GET。

    完全保持 worldcut 原签名（方便 NativeRunner 工具协议透传）。
    """
    cache_id = json.dumps(
        {
            "url": url,
            "headers": sorted((headers or {}).items()),
            "proxy": proxy_url,
        },
        sort_keys=True,
    )
    now = _now()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_id)
        if cached and now - cached["ts"] < ttl_seconds:
            return cached["data"]

    req = request.Request(
        url, headers=headers or {"User-Agent": USER_AGENT_GENERIC}
    )
    opener = (
        request.build_opener(
            request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        if proxy_url
        else request.build_opener()
    )
    with opener.open(req, timeout=timeout_seconds) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    with _CACHE_LOCK:
        _CACHE[cache_id] = {"ts": now, "data": data}
    return data


def _unwrap_proxy_json(data: dict) -> dict:
    """AllOrigins / cors-anywhere 包装层剥离。"""
    if isinstance(data, dict) and isinstance(data.get("contents"), str):
        try:
            return json.loads(data["contents"])
        except json.JSONDecodeError:
            return data
    if isinstance(data, dict) and isinstance(data.get("body"), str):
        try:
            return json.loads(data["body"])
        except json.JSONDecodeError:
            return data
    return data


# ----------------------------------------------------------------------
# sporttery 解析（从 worldcut server.py:1346..1406 抽取）
# ----------------------------------------------------------------------


def _walk_json(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _pick(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _normalize_decimal(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _choose_pool(row: dict) -> tuple[dict, str, str]:
    candidates: list[dict] = []
    odds_list = row.get("oddsList")
    if isinstance(odds_list, list):
        candidates.extend(it for it in odds_list if isinstance(it, dict))
    for key in ("had", "HAD", "spf", "hhad", "HHAD", "odds"):
        v = row.get(key)
        if isinstance(v, list):
            candidates.extend(it for it in v if isinstance(it, dict))
        elif isinstance(v, dict):
            candidates.append(v)
    if not candidates:
        candidates.append(row)
    for wanted in ("HAD", "HHAD"):
        for item in candidates:
            code = str(item.get("poolCode") or item.get("pool_code") or "").upper()
            if code == wanted or (wanted == "HAD" and not code):
                goal_line = str(
                    item.get("goalLine") or item.get("goal_line") or ""
                ).strip()
                return item, wanted, goal_line
    return {}, "", ""


def parse_sporttery_matches(data: dict) -> list[dict]:
    matches: list[dict] = []
    for row in _walk_json(data):
        home = _pick(row, (
            "homeTeamAllName", "homeTeamName", "homeTeam",
            "homeTeamAbbName", "hostName", "homeName", "h_cn", "home_team",
        ))
        away = _pick(row, (
            "awayTeamAllName", "awayTeamName", "awayTeam",
            "awayTeamAbbName", "guestName", "awayName", "a_cn", "away_team",
        ))
        if not home or not away:
            continue
        had, market_code, goal_line = _choose_pool(row)
        win = _normalize_decimal(_pick(had, ("h", "home", "win", "had_h", "h_sp", "a")))
        draw = _normalize_decimal(_pick(had, ("d", "draw", "had_d", "d_sp", "b")))
        lose = _normalize_decimal(_pick(had, ("a", "away", "lose", "had_a", "a_sp", "c")))
        if not all([win, draw, lose]):
            continue
        matches.append({
            "home": str(home),
            "away": str(away),
            "league": _pick(row, ("leagueName", "leagueAbbName", "leagueAllName", "league", "l_cn")),
            "match_no": _pick(row, ("matchNumStr", "matchNum", "matchNo", "num", "issueNum")),
            "match_date": _pick(row, ("matchDate", "businessDate", "date")),
            "match_clock": _pick(row, ("matchTime", "time")),
            "market_code": market_code or "HAD",
            "market_name": "让球胜平负" if market_code == "HHAD" else "胜平负",
            "goal_line": goal_line,
            "decimal": [win, draw, lose],
            "source": "中国体育彩票竞彩网固定奖金",
        })
    return matches


def _build_sporttery_urls() -> list[str]:
    urls: list[str] = []
    for raw_url in [SPORTTERY_PROXY_URL, *SPORTTERY_PROXY_URLS, SPORTTERY_OFFICIAL_URL]:
        if not raw_url:
            continue
        if "{url}" in raw_url:
            raw_url = raw_url.replace(
                "{url}", parse.quote(SPORTTERY_OFFICIAL_URL, safe="")
            )
        if raw_url not in urls:
            urls.append(raw_url)
    return urls


def sporttery_fetch_payload(
    *, timeout_seconds: float = 3.0
) -> tuple[dict | None, str, str]:
    """多 URL fallback 拉取 sporttery 官方网关。

    返回 ``(payload, used_url, error_summary)``。

    **硬超时**：默认 3 秒；世界杯期间反爬严，会出现 SYN 蜜罐导致
    socket 永远不回包；之前的 8s 超时不够强，配合 ThreadPoolExecutor
    死锁。每个 URL 单独计时；任意一条失败立刻尝试下一条。
    """
    errors: list[str] = []
    for url in _build_sporttery_urls():
        try:
            use_proxy = bool(SPORTTERY_HTTP_PROXY) and url == SPORTTERY_OFFICIAL_URL
            data = fetch_json_request_cached(
                url,
                headers={
                    "User-Agent": USER_AGENT_BROWSER,
                    "Referer": "https://www.sporttery.cn/",
                    "Origin": "https://www.sporttery.cn",
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                ttl_seconds=60,
                timeout_seconds=timeout_seconds,
                proxy_url=SPORTTERY_HTTP_PROXY if use_proxy else "",
            )
            return _unwrap_proxy_json(data), url, ""
        except Exception as exc:  # noqa: BLE001 — fallback 链路语义
            errors.append(f"{url}: {str(exc)[:180]}")
    source_url = (
        _build_sporttery_urls()[0]
        if _build_sporttery_urls()
        else SPORTTERY_OFFICIAL_URL
    )
    return None, source_url, " | ".join(errors)


# ----------------------------------------------------------------------
# 本地 Fallback：input_matches.json + 内置 Mock
# ----------------------------------------------------------------------

# 紧急情况下的内置硬编码焦点战。沿用 image.png 的真实赔率（用户给定）。
# 单一 Source of Truth：荷兰 vs 日本，让球 -1，HHAD 1.85 / 3.55 / 3.95
_BUILTIN_FALLBACK_MATCHES: list[dict] = [
    {
        "home": "荷兰",
        "away": "日本",
        "league": "世界杯",
        "match_no": "周一003",
        "match_date": "2026-06-16",
        "match_clock": "21:00",
        "market_code": "HHAD",
        "market_name": "让球胜平负",
        "goal_line": "-1.00",
        "decimal": [1.85, 3.55, 3.95],
        "source": "builtin_fallback (image.png)",
    },
    {
        "home": "巴西",
        "away": "墨西哥",
        "league": "世界杯",
        "match_no": "周一008",
        "match_date": "2026-06-17",
        "match_clock": "03:00",
        "market_code": "HHAD",
        "market_name": "让球胜平负",
        "goal_line": "-1.00",
        "decimal": [1.72, 3.85, 4.10],
        "source": "builtin_fallback (image.png)",
    },
]


def _load_local_matches_file() -> list[dict] | None:
    """读取项目根目录的 ``input_matches.json``。

    格式约定：
        {"matches": [{home, away, league, match_dach_clock,
                      market_code, market_name, goal_line, decimal:[h,d,a]}]}
    """
    # 在 lottery_kernel/tools 下，所以根 = 上 3 层
    project_root = Path(__file__).resolve().parents[2]
    candidates = [
        project_root / "input_matches.json",
        Path.cwd() / "input_matches.json",
    ]
    for p in candidates:
        try:
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("matches"), list):
                    return raw["matches"]
                if isinstance(raw, list):
                    return raw
        except Exception:  # noqa: BLE001 — 本地文件损坏时回退到内置
            continue
    return None


def _fallback_snapshot(reason: str) -> dict:
    """组装一个本地 fallback 快照，结构与在线快照完全一致。"""
    matches = _load_local_matches_file()
    source = "local file: input_matches.json"
    if not matches:
        matches = list(_BUILTIN_FALLBACK_MATCHES)
        source = "builtin fallback (image.png reference odds)"
    return {
        "source": source,
        "source_url": "local://input_matches.json",
        "status": "ok_local_fallback",
        "fallback_reason": reason[:200],
        "matches": matches[:200],
        "raw_count": len(matches),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------------------------------------------------
# Native 工具表面 —— Analyst 通过这些函数获得"observation string"
# ----------------------------------------------------------------------


def _to_observation(payload: Any) -> str:
    """NativeRunner 的工具协议要求返回 str。"""
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _run_blocking(fn: Callable[..., Any], *args, timeout: float = 30.0, **kwargs) -> Any:
    """把阻塞调用丢进线程池，避免堵 asyncio / ticker。"""
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    return future.result(timeout=timeout)


def sporttery_snapshot(
    *,
    online_timeout_seconds: float = 3.0,
    allow_fallback: bool = True,
) -> dict:
    """同步包装：拉取竞彩盘口快照。

    🚨 OFFLINE_ONLY 模式下永远走本地文件，**不发起任何网络调用**。
    保留 ``online_timeout_seconds`` / ``allow_fallback`` 参数仅作前向
    兼容（dry_run_pipeline 仍然按旧签名传参，无须 break）。
    """
    if OFFLINE_ONLY:
        return _fallback_snapshot(
            "OFFLINE_ONLY=True (network disabled by user policy)"
        )

    # 下面这段在 OFFLINE_ONLY=True 时永远不会被执行。
    try:
        data, url, err = sporttery_fetch_payload(
            timeout_seconds=online_timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001
        data, url, err = None, "(fetch crash)", f"{type(exc).__name__}: {exc}"

    if data is not None:
        matches = parse_sporttery_matches(data)
        if matches:
            return {
                "source": "中国体育彩票竞彩网",
                "source_url": url,
                "status": "ok",
                "matches": matches[:200],
                "raw_count": len(matches),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        err = (err or "parsed 0 matches from upstream")

    if allow_fallback:
        return _fallback_snapshot(err or "online fetch returned no data")

    return {
        "source": "中国体育彩票竞彩网",
        "source_url": url,
        "status": "blocked_or_unavailable",
        "error": (err or "")[:300],
        "matches": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def odds_api_lookup(home: str, away: str) -> list[dict]:
    """The Odds API 的多家盘口聚合（h2h 默认）。

    🚨 OFFLINE_ONLY 模式下短路：返回 offline_mode 占位结果，零网络。
    """
    if OFFLINE_ONLY:
        return [{
            "type": "offline_mode",
            "source": "OFFLINE_ONLY",
            "home": home,
            "away": away,
            "note": (
                "OFFLINE_ONLY=True；本工具已禁用所有网络访问。"
                "请使用 odds_snapshot/sporttery_match 直接消费本地快照。"
            ),
            "items": [],
        }]

    # 下面这段在 OFFLINE_ONLY=True 时永远不会被执行。
    if not ODDS_API_KEY:
        return [{
            "type": "odds_api_not_configured",
            "source": "The Odds API",
            "note": "设置 ODDS_API_KEY 环境变量后启用。",
        }]
    sport_key = os.getenv("ODDS_API_SPORT", "soccer_fifa_world_cup")
    query = parse.urlencode({
        "apiKey": ODDS_API_KEY,
        "regions": os.getenv("ODDS_API_REGIONS", "us,eu,uk"),
        "markets": os.getenv("ODDS_API_MARKETS", "h2h"),
        "oddsFormat": os.getenv("ODDS_API_FORMAT", "decimal"),
    })
    url = f"{ODDS_API_BASE_URL}/sports/{sport_key}/odds?{query}"
    try:
        data = fetch_json_request_cached(url, ttl_seconds=600)
    except Exception as exc:  # noqa: BLE001
        return [{
            "type": "odds_api_error",
            "source": "The Odds API",
            "error": str(exc),
        }]
    items = data if isinstance(data, list) else []
    matched = [
        it for it in items
        if home in {it.get("home_team"), it.get("away_team")}
        or away in {it.get("home_team"), it.get("away_team")}
        or home in str(it) or away in str(it)
    ]
    return [{
        "type": "odds_api_market_snapshot",
        "source": "The Odds API",
        "sport_key": sport_key,
        "home": home,
        "away": away,
        "items": matched[:3],
        "raw_count": len(items),
    }]


# ----------------------------------------------------------------------
# NativeRunner 工具：签名 (workspace, args) -> observation:str
# ----------------------------------------------------------------------


def odds_snapshot(_workspace, _args: dict | None = None) -> str:
    """工具 ``odds_snapshot`` —— 拉取竞彩官方实时盘口。"""
    payload = _run_blocking(sporttery_snapshot, timeout=20.0)
    return _to_observation(payload)


def match_odds(_workspace, args: dict | None = None) -> str:
    """工具 ``match_odds`` —— 查询某场比赛的 The Odds API 多家盘口。

    Args:
        args.home: 主队（中文或英文，按 sport_key 而定）
        args.away: 客队
    """
    a = args or {}
    home = str(a.get("home", "")).strip()
    away = str(a.get("away", "")).strip()
    if not home or not away:
        return _to_observation({
            "type": "tool_error",
            "error": "match_odds requires 'home' and 'away' arguments",
        })
    payload = _run_blocking(odds_api_lookup, home, away, timeout=25.0)
    return _to_observation(payload)


def sporttery_match_lookup(_workspace, args: dict | None = None) -> str:
    """工具 ``sporttery_match`` —— 在体彩快照中按队名搜索单场。"""
    a = args or {}
    home = str(a.get("home", "")).strip()
    away = str(a.get("away", "")).strip()
    snap = _run_blocking(sporttery_snapshot, timeout=20.0)
    matches = snap.get("matches", []) if isinstance(snap, dict) else []
    hits = [
        m for m in matches
        if (not home or home in str(m.get("home", "")) or home in str(m.get("away", "")))
        and (not away or away in str(m.get("home", "")) or away in str(m.get("away", "")))
    ]
    return _to_observation({
        "source": snap.get("source"),
        "source_url": snap.get("source_url"),
        "hits": hits[:5],
        "snapshot_status": snap.get("status"),
    })


# ----------------------------------------------------------------------
# 注册表（与 Kompany NativeRunner native_tools._TOOLS 同形）
# ----------------------------------------------------------------------

LOTTERY_TOOL_DOCS = """\
Lottery tools (args is always a JSON object):

- odds_snapshot: {} — 拉取中国体育彩票竞彩网的实时胜平负/让球盘口快照。
  返回 {source, source_url, status, matches:[{home, away, decimal:[h,d,a],
  goal_line, market_code, market_name, ...}]}。

- match_odds: {"home": "<team>", "away": "<team>"} — 查询 The Odds API
  的多家欧美博彩盘口（h2h，默认 decimal 格式）。返回最多 3 条匹配场次。

- sporttery_match: {"home": "<team>", "away": "<team>"} — 在体彩快照
  中按队名子串搜索单场，用于 CRO/Analyst 锁定具体比赛。

所有工具通过 ThreadPoolExecutor 异步执行；调用方收到 JSON 字符串作为 observation。
"""


LOTTERY_TOOLS: dict[str, Callable[[Any, dict], str]] = {
    "odds_snapshot": odds_snapshot,
    "match_odds": match_odds,
    "sporttery_match": sporttery_match_lookup,
}


class OddsToolbox:
    """便于 Analyst 直接调用的同步门面（绕开 NativeRunner 时也能用）。"""

    def __init__(self, executor: concurrent.futures.ThreadPoolExecutor | None = None):
        self._exec = executor or _EXECUTOR

    def snapshot(self) -> dict:
        future = self._exec.submit(sporttery_snapshot)
        return future.result(timeout=20.0)

    def lookup(self, home: str, away: str) -> list[dict]:
        future = self._exec.submit(odds_api_lookup, home, away)
        return future.result(timeout=25.0)


__all__ = [
    "fetch_json_request_cached",
    "sporttery_fetch_payload",
    "parse_sporttery_matches",
    "sporttery_snapshot",
    "odds_api_lookup",
    "odds_snapshot",
    "match_odds",
    "sporttery_match_lookup",
    "OddsToolbox",
    "LOTTERY_TOOL_DOCS",
    "LOTTERY_TOOLS",
]
