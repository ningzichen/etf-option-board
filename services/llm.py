"""大模型决策：把市场快照交给 LLM，产出结构化的日内交易决策（方向/合约/买卖点/止盈止损/理由）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta

import httpx

from config import settings
from services import decision

log = logging.getLogger("llm")

_SYSTEM = """你是一位资深 A 股 ETF 期权日内交易员，擅长 50ETF/300ETF/500ETF/科创50/创业板等场内期权。
你的任务：基于给定的市场快照（标的行情、期权链与隐波、资金流、关联指数、国家队护盘信号、外围市场、Kronos 量化预测），
给出**当天日内**的可执行交易决策，当日做 1-2 次买卖，不频繁交易。

交易原则：
1) 期权与标的指数正相关，先判断标的 ETF 的日内方向，再选同向合约。
2) 关键变盘时点：10:30 / 11:30 / 13:00 / 14:30 附近重点观察。
3) 结合量能与主力资金判断真假突破（放量+主力净流入为真）。
4) 用分钟级 MACD 动能与背离过滤信号，避免频繁噪音交易。
5) 指数跌至支撑且出现国家队护盘（宽基 ETF 放量+主力净流入）时，倾向看反弹。
6) 选合约：优先流动性好（成交额大、持仓大、买卖价差小）、Delta 0.35~0.65 的平值/轻虚值合约；
   低溢价率更“实”，隐波过高则买方成本贵需谨慎。
7) 必须给出明确：方向、合约代码、入场价、止损价、止盈价（含百分比），以及不确定性说明。
8) 若信号冲突或置信度低，明确建议"观望"，不要强行给单。

规则层结论（快照里的「规则层策略」「风险预算与手数上限」字段，由确定性规则算出，**优先级高于你的判断**）：
- 策略类型：买单腿认购 / 买单腿认沽 / 牛市看涨价差 / 熊市看跌价差 / 观望；
- 临期约束：当月合约距到期 ≤3 天时不得新开当月头寸，改用次月；
- 手数上限：「风险预算与手数上限」给出 max_contracts，你的 contracts 不得超过它。
你的 action 必须与规则层策略类型一致（规则层观望就输出 wait，且不要给合约代码），
价差策略请用 legs 给出两条腿。若你认为规则层有误，仍按其执行，把你的异议写进 risks。

严格只输出一个 JSON 对象，不要任何解释文字或代码块标记。字段：
{
  "direction": "call|put|neutral",
  "action": "buy_call|buy_put|bull_call_spread|bear_put_spread|wait",
  "contracts": 整数（建议张数，不得超过手数上限）或null,
  "legs": {"long": {"code":"","name":"","strike":数字或null}, "short": {"code":"","name":"","strike":数字或null}} 或 null,
  "contract_code": "合约代码或空串（价差填多头腿）",
  "contract_name": "合约简称或空串",
  "entry": 数字或null,
  "stop": 数字或null,
  "target": 数字或null,
  "stop_pct": 数字（相对入场的止损%）或null,
  "target_pct": 数字或null,
  "confidence": 0-100 整数,
  "hold_window": "建议持有到的时间点，如 14:30",
  "reasons": ["要点1","要点2"],
  "risks": ["风险1"],
  "summary": "一句话结论"
}"""


def _load_key() -> str:
    if settings.llm_auth_key:
        return settings.llm_auth_key
    try:
        data = json.load(open(settings.llm_auth_json))
        for name in ("opencode-go", "opencode"):
            if name in data and data[name].get("key"):
                return data[name]["key"]
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 LLM 密钥失败: %s", exc)
    return ""


def available() -> bool:
    return bool(_load_key())


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None
    return None


def _compact(bundle: dict) -> dict:
    """把策略快照裁剪为适合提示词的精简结构。"""
    keep_keys = {"code", "name", "type", "strike", "expiry", "days", "last", "pct",
                 "volume", "amount", "oi", "spread", "moneyness", "itm",
                 "premium_rate", "iv", "delta", "gamma", "leverage", "score"}
    return {
        "标的": bundle.get("underlying"),
        "链统计(近月隐波/偏斜/PCR)": bundle.get("chain_stats"),
        "方向评分": bundle.get("bias"),
        "技术点位": bundle.get("levels"),
        "交易方案(按delta折算)": bundle.get("plans"),
        "候选合约": [{k: c.get(k) for k in keep_keys} for c in (bundle.get("candidates") or [])[:6]],
        "信号": [s.get("text") for s in (bundle.get("signals") or [])],
        "Kronos量化预测": bundle.get("kronos"),
        "关联指数": bundle.get("track"),
        "国家队护盘": {k: bundle.get("national_team", {}).get(k)
                    for k in ("score", "level", "surge_count", "main_5m_sum")},
        "外围市场": [{"name": e.get("name_cn"), "pct": e.get("pct")}
                  for e in (bundle.get("external") or [])],
        "当前量能": bundle.get("volume"),
        "当前MACD(1分钟)": bundle.get("macd"),
        "当前MACD(15分钟背离)": bundle.get("macd15"),
        # ── 规则层结论：模型必须与之对齐（见 decide 中的一致性校验）──
        "规则层策略": bundle.get("strategy"),
        "到期日历": bundle.get("expiry"),
        "风险预算与手数上限": bundle.get("position"),
        "敞口与限额": {"敞口": bundle.get("exposure"), "限额": bundle.get("limits"),
                     "超限": bundle.get("limit_check")},
    }


def browser_context(bundle: dict) -> dict:
    """只生成浏览器调用所需的提示词与规则，不读取密钥、不调用模型。"""
    # 股指详情使用 advice 而不是 ETF 的 strategy；将已有结论翻译成同一约束，
    # 避免接入浏览器模型后把「观望」或无期权的 IC 当成可开仓标的。
    if bundle.get("kind") == "index" and not bundle.get("strategy"):
        advice = bundle.get("advice") or {}
        action = decision.STRATEGY_WAIT
        if advice.get("action") == "买入" and advice.get("side") in ("call", "put"):
            action = decision.STRATEGY_SINGLE_CALL if advice["side"] == "call" else decision.STRATEGY_SINGLE_PUT
        bundle = {**bundle, "strategy": {"strategy": action, "reason": advice.get("reason")}}
    user = ("以下是当前市场快照（JSON）。请给出今日日内期权交易决策，"
            "严格按系统要求输出 JSON：\n" + json.dumps(_compact(bundle), ensure_ascii=False))
    cap = (bundle.get("position") or {}).get("max_contracts")
    return {
        "system": _SYSTEM,
        "user": user,
        "guardrails": {
            "action": decision.expected_action(bundle.get("strategy")),
            "max_contracts": cap if type(cap) is int and cap >= 0 else None,
            "action_labels": dict(decision.ACTION_LABELS),
        },
        "bundle_bias": bundle.get("bias"),
        "signals": bundle.get("signals"),
        "ts": bundle.get("ts"),
    }


def _http_error_text(r: httpx.Response) -> str:
    if r.status_code == 429:
        # 免费模型额度用尽：把恢复时间直白告诉用户，而不是一串原始 JSON
        ra = (r.headers.get("retry-after") or "").strip()
        if ra.isdigit():
            mins = max(1, round(int(ra) / 60))
            when = (datetime.now() + timedelta(seconds=int(ra))).strftime("%H:%M")
            return f"免费模型额度已用尽，约 {mins} 分钟后（{when}）恢复；续订 OpenCode Go 可立即使用。"
        return "免费模型额度已用尽，请稍后重试或续订 OpenCode Go。"
    if r.status_code in (401, 403):
        return f"LLM 鉴权失败 HTTP {r.status_code}：{r.text[:160]}"
    return f"LLM HTTP {r.status_code}: {r.text[:200]}"


def _cli_ready() -> bool:
    return bool(settings.llm_cli_fallback and shutil.which(settings.llm_cli_bin))


def _run_cli(prompt: str, timeout: int) -> str:
    """同步调用本机 opencode CLI（客户端通道），返回助手最终文本。"""
    exe = shutil.which(settings.llm_cli_bin)
    if not exe:
        raise RuntimeError(f"未找到 {settings.llm_cli_bin} CLI")
    os.makedirs(settings.llm_cli_dir, exist_ok=True)
    # 当前 opencode 版本没有 `--pure` / `--dir` 两个 flag（旧写法会直接
    # `Unrecognized flag` 退出）：工作目录改用 subprocess 的 cwd。
    cmd = [exe, "run", "--format", "json", "-m", settings.llm_cli_model, prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=settings.llm_cli_dir)
    texts: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        part = ev.get("part") or {}
        if ev.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
    content = "".join(texts).strip()
    if not content:
        tail = " ".join((proc.stderr or "").strip().splitlines()[-2:])
        raise RuntimeError(f"CLI 无输出（exit={proc.returncode}）{tail[:200]}")
    return content


async def _cli_decide(prompt: str, timeout: int) -> str:
    return await asyncio.to_thread(_run_cli, prompt, timeout)


async def decide(bundle: dict, timeout: int | None = None) -> dict:
    user = browser_context(bundle)["user"]
    content = ""
    err = ""
    via = "api"
    key = _load_key()
    if not key:
        err = "未找到 LLM 密钥"
    else:
        body = {
            "model": settings.llm_model,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": user}],
            "temperature": 0.4,
            "max_tokens": 3000,
        }
        headers = {"Authorization": f"Bearer {key}", "x-opencode-session": uuid.uuid4().hex}
        try:
            async with httpx.AsyncClient(timeout=timeout or settings.llm_timeout) as c:
                r = await c.post(f"{settings.llm_api_base}/chat/completions", json=body, headers=headers)
            if r.status_code == 200:
                msg = r.json()["choices"][0]["message"]
                content = msg.get("content") or msg.get("reasoning_content") or ""
            else:
                err = _http_error_text(r)
                log.warning("LLM REST 失败，尝试 CLI 兜底: %s", err)
        except Exception as exc:  # noqa: BLE001
            err = f"LLM 请求异常: {exc}"
            log.warning("LLM REST 异常，尝试 CLI 兜底: %s", exc)

    if not content and _cli_ready():
        # 免费模型只对 opencode 客户端放行：REST 429/403 时改走本机 CLI 通道
        try:
            prompt = _SYSTEM + "\n\n" + user
            content = await _cli_decide(prompt, settings.llm_cli_timeout)
            via = "cli"
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM CLI 兜底失败: %s", exc)
            return {"error": f"{err}；CLI 通道也失败：{exc}" if err else f"CLI 通道失败：{exc}"}

    if not content:
        return {"error": err or "LLM 调用失败"}

    parsed = _extract_json(content)
    if not parsed:
        return {"error": "LLM 返回无法解析为 JSON", "raw": content[:500]}
    # 与规则层对齐：规则层观望时不得给买单，张数不得超手数上限（改动留痕）
    aligned = decision.align_llm_decision(parsed, bundle.get("strategy"),
                                         bundle.get("position"))
    out = aligned["decision"]
    if isinstance(out, dict):
        out["_model"] = settings.llm_model
        out["_via"] = via
        if aligned["adjustments"]:
            out["adjustments"] = aligned["adjustments"]
    return out
