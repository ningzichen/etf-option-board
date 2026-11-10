"""上证指数手动支撑/压力位：每个交易日存一条，顺延生效。

口径（2026-11-09 与用户确认）：
- 键 = 保存时弹窗正在展示的指数分时交易日（盘中=今天，盘前/周末=最近交易日）；
- 生效值 = 日期 ≤ 今天的**最大日期**那条记录（顺延；今天没新设就用上一条）；
- 同一天重复保存为覆盖，不留修订历史；
- 必须两个都填，数值区间 100–99999，且支撑位 < 压力位，否则拒绝保存；
- 不提供清除入口（设过就一直顺延，只能覆盖）。

`parse_record()` / `effective()` 是纯函数（不读文件、不读时钟、不写盘），
`load()` / `save()` 只是文件读写薄壳，失败一律降级、绝不抛异常。

存储 `data/index_levels_manual.json` 命中 .gitignore 的 `data/*.json` 规则，
不随 auto-backup 进入 git 历史。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

from paths import data_dir, data_path

log = logging.getLogger("index_levels")

_PATH = data_path("index_levels_manual.json")

MIN_VALUE = 100.0
MAX_VALUE = 99999.0
_FIELDS = ("support", "pressure")


def _to_float(value) -> float | None:
    """尝试转 float；bool 与不可转类型返回 None（bool 是 int 的子类，须显式排除）。"""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_day(day) -> bool:
    if not isinstance(day, str):
        return False
    try:
        dt.datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def parse_record(raw, day: str | None = None) -> tuple[dict | None, list[str]]:
    """校验一条手动位，返回 (记录, 错误列表)。纯函数：不读文件、不读时钟。"""
    if not isinstance(raw, dict):
        raw = {}
    errors: list[str] = []

    key = day if day is not None else raw.get("date")
    if not _valid_day(key):
        errors.append("日期必须形如 YYYY-MM-DD")

    values: dict[str, float | None] = {}
    for field, label in (("support", "支撑位"), ("pressure", "压力位")):
        num = _to_float(raw.get(field))
        if num is None:
            errors.append(f"{label}必须是数字")
        elif not (MIN_VALUE <= num <= MAX_VALUE):
            errors.append(f"{label}需在 {MIN_VALUE:.0f}–{MAX_VALUE:.0f} 之间")
        else:
            values[field] = round(num, 2)

    support, pressure = values.get("support"), values.get("pressure")
    if support is not None and pressure is not None and support >= pressure:
        errors.append("支撑位必须小于压力位")

    if errors:
        return None, errors
    return {"date": key, "support": support, "pressure": pressure}, []


def has_record(records: dict, day: str) -> bool:
    """当天是否保存过（顺延的旧记录不算）。纯函数。"""
    if not isinstance(records, dict) or not _valid_day(day):
        return False
    rec = records.get(day)
    if not isinstance(rec, dict):
        return False
    return _to_float(rec.get("support")) is not None and _to_float(rec.get("pressure")) is not None


def effective(records: dict, day: str) -> dict | None:
    """顺延生效：取 date ≤ day 的最大日期记录；没有或该条数值非法则继续往前找。纯函数。"""
    if not isinstance(records, dict):
        return None
    if not _valid_day(day):
        return None
    keys = sorted((k for k in records if _valid_day(k) and k <= day), reverse=True)
    for key in keys:
        rec = records.get(key)
        if not isinstance(rec, dict):
            continue
        support = _to_float(rec.get("support"))
        pressure = _to_float(rec.get("pressure"))
        if support is None or pressure is None:
            continue
        return {
            "support": round(support, 2),
            "pressure": round(pressure, 2),
            "date": key,
            "carried": key < day,
        }
    return None


def load_records() -> dict:
    """读取全部记录；文件缺失或损坏一律回落空表，绝不抛异常。"""
    try:
        with open(_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("读取手动关键位失败，按无记录处理: %s", exc)
        return {}
    records = raw.get("records") if isinstance(raw, dict) else None
    return records if isinstance(records, dict) else {}


def save(record: dict) -> bool:
    """把一条记录并入文件（同日覆盖）；先写临时文件再替换。失败只记日志并返回 False。"""
    if not isinstance(record, dict) or not _valid_day(record.get("date")):
        return False
    records = load_records()
    records[record["date"]] = {
        "support": record.get("support"),
        "pressure": record.get("pressure"),
        "at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    tmp = _PATH + ".tmp"
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"records": records}, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, _PATH)
        return True
    except OSError as exc:
        log.warning("写入手动关键位失败: %s", exc)
        return False


def current(day: str | None = None) -> dict | None:
    """读盘 + 顺延，返回当前生效的手动位（含 date/carried）。"""
    day = day or dt.date.today().isoformat()
    return effective(load_records(), day)
