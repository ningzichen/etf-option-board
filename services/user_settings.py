"""看板用户设置：总资金与单笔风险%。

`parse()` 是纯函数，负责校验与归一化，可脱离 I/O 与时钟做确定性测试；
`load()` / `save()` 只是文件读写薄壳，失败一律降级、绝不抛异常。

存储位置 `data/settings.json` 命中 .gitignore 的 `data/*.json` 规则，
不会随 auto-backup 进入 git 历史 —— 总资金属敏感输入，见 docs/adr/0001。
"""

from __future__ import annotations

import json
import logging
import os

from paths import data_dir
from services.decision import SIZE_LABELS, SIZES, TIER_LABELS, TIERS

log = logging.getLogger("settings")

_DATA_DIR = data_dir()
_PATH = os.path.join(_DATA_DIR, "settings.json")

# 单笔风险%三档。与风险偏好档联动：1%→保守 / 2%→标准 / 3%→激进。
RISK_PCT_CHOICES: tuple[float, ...] = (1.0, 2.0, 3.0)
DEFAULT_RISK_PCT: float = 1.0
DEFAULT_TOTAL_CAPITAL: float | None = None

# 敞口档位与档位规模：取值域与文案一律取自纯决策模块，不在本模块重写一份
DEFAULT_TIER: str = "flat"
DEFAULT_SIZE: str = "mid"

_FIELDS = ("total_capital", "risk_pct", "tier", "size")


def _unset(value) -> bool:
    """None 与空字符串都表示「未设置」。"""
    return value is None or (isinstance(value, str) and value == "")


def _to_float(value) -> float | None:
    """尝试转 float；bool 与不可转类型返回 None（bool 是 int 的子类，须显式排除）。"""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse(raw) -> tuple[dict, list[str]]:
    """校验并归一化设置，返回 (设置, 错误列表)。

    纯函数：不读文件、不读时钟、不改动入参。字段独立降级 —— 某个字段非法时
    只有它回落到默认值，同行的合法字段照常接受，错误列表逐条点名字段。
    """
    if not isinstance(raw, dict):
        raw = {}

    errors: list[str] = []

    total_capital: float | None = DEFAULT_TOTAL_CAPITAL
    if not _unset(raw.get("total_capital")):
        num = _to_float(raw["total_capital"])
        if num is None or num <= 0:
            errors.append("总资金必须是大于 0 的数字")
        else:
            total_capital = num

    risk_pct: float = DEFAULT_RISK_PCT
    if not _unset(raw.get("risk_pct")):
        num = _to_float(raw["risk_pct"])
        if num is None or num not in RISK_PCT_CHOICES:
            errors.append("单笔风险% 只能是 1 / 2 / 3 三档之一")
        else:
            risk_pct = num

    tier: str = DEFAULT_TIER
    if not _unset(raw.get("tier")):
        val = raw["tier"]
        if isinstance(val, str) and val in TIERS:
            tier = val
        else:
            errors.append("敞口档位只能是 空仓 / 持有ETF / 持认购 / 持认沽 之一")

    size: str = DEFAULT_SIZE
    if not _unset(raw.get("size")):
        val = raw["size"]
        if isinstance(val, str) and val in SIZES:
            size = val
        else:
            errors.append("档位规模只能是 轻 / 中 / 重 之一")

    return {
        "total_capital": total_capital,
        "risk_pct": risk_pct,
        "tier": tier,
        "size": size,
        # 只填了风险档而没填总资金仍算未配置：手数上限无从算起
        "configured": total_capital is not None,
    }, errors


def view(settings: dict) -> dict:
    """给前端的视图：设置值 + 各枚举的可选项与文案。纯函数，选项不写死在前端。"""
    tier = settings.get("tier") if settings.get("tier") in TIERS else DEFAULT_TIER
    size = settings.get("size") if settings.get("size") in SIZES else DEFAULT_SIZE
    return {
        **settings,
        "tier": tier,
        "size": size,
        "tier_label": TIER_LABELS.get(tier),
        "size_label": SIZE_LABELS.get(size),
        "risk_pct_choices": list(RISK_PCT_CHOICES),
        "tier_choices": [{"key": k, "label": TIER_LABELS[k]} for k in TIERS],
        "size_choices": [{"key": k, "label": SIZE_LABELS[k]} for k in SIZES],
    }


def load() -> dict:
    """读取设置；文件缺失或损坏一律回落默认值，绝不抛异常。"""
    raw = None
    try:
        with open(_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("读取设置失败，改用默认值: %s", exc)

    settings, errors = parse(raw)
    for err in errors:
        log.warning("设置文件含非法值，已回落默认: %s", err)
    return settings


def save(settings: dict) -> bool:
    """写盘；先写临时文件再替换，避免留下半截文件。失败只记日志并返回 False。"""
    payload = {k: settings.get(k) for k in _FIELDS}
    tmp = _PATH + ".tmp"
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
        return True
    except OSError as exc:
        log.warning("写入设置失败: %s", exc)
        return False
