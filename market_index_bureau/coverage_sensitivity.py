"""非应答商户情景下交易景气结论的敏感性包络分析。

本模块只读面板输入与既有 vintage 数据库，不重算、不修改正式指数发布。
方法为区间包络（部分识别）：在业务给定的缺报相对变化区间内，全框指数
对缺报值是线性的，因此精确上下界在情景区间角点处取得。结果是确定性的
界限与转折点，不是抽样误差线，也不把情景填充值当作观测事实。

输出字段：study_id、coverage、distribution、composition、envelopes、
tipping_points、assumptions。同一正式指数版本可对应多份假设底稿；
底稿以 study_id 指纹存入 coverage_studies 表，只增不改，旧底稿不被覆盖。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from market_index_bureau.vintages.index_store import DomainError, fingerprint, utc_now

METHOD_VERSION = "coverage-sensitivity-1.0.0"
SCHEMA_VERSION = 1
GROWTH_FLOOR = -1.0  # 交易额非负：相对变化不可能低于 -100%
EPS = 1e-9
QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)

DIRECTION_NOTES = {
    "growth_holds": "全部情景下指数高于 1，增长方向保持不变",
    "decline_holds": "全部情景下指数低于 1，下降方向保持不变",
    "reversal_possible": "情景区间内同时存在高于与低于 1 的结果，方向可能反转",
    "indeterminate": "业务未给出完整上下界，无法判别方向范围",
}

UNBOUNDED_TOKENS = {"", "null", "none", "unbounded"}


# ---------------------------------------------------------------- 基础工具

def _r6(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


def _strict_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(f"{field} 必须是数值，得到 {value!r}")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise DomainError(f"{field} 必须是有限数值，得到 {value!r}")
    return result


def _parse_instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DomainError(f"{field} 必须是带时区的 ISO 8601 字符串，得到 {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError(f"{field} 不是合法 ISO 8601 时刻: {value!r}") from exc
    if parsed.tzinfo is None:
        raise DomainError(f"{field} 缺少时区信息: {value!r}")
    return parsed


def _quantile(sorted_xs: list[float], p: float) -> float:
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    pos = (n - 1) * p
    low = int(pos)
    high = min(low + 1, n - 1)
    frac = pos - low
    return sorted_xs[low] * (1.0 - frac) + sorted_xs[high] * frac


def _summarize(values: list[float], weights: Optional[list[float]] = None) -> dict[str, Any]:
    ordered = sorted(values)
    n = len(ordered)
    summary: dict[str, Any] = {"count": n}
    if n == 0:
        summary.update({"mean": None, "min": None, "max": None})
        for q in QUANTILES:
            summary[f"p{int(q * 100)}"] = None
        return summary
    summary["mean"] = _r6(sum(ordered) / n)
    summary["min"] = _r6(ordered[0])
    summary["max"] = _r6(ordered[-1])
    for q in QUANTILES:
        summary[f"p{int(q * 100)}"] = _r6(_quantile(ordered, q))
    if weights is not None:
        total_weight = sum(weights)
        if total_weight > 0:
            summary["weighted_mean"] = _r6(sum(w * x for w, x in zip(weights, values)) / total_weight)
    return summary


# ---------------------------------------------------------------- 输入校验与装配

REQUIRED_KEYS = (
    "schema_version", "as_of", "source_versions", "frame", "turnover",
    "assumption_sets", "index_reference", "periods", "baseline_case_id",
)


def load_panel(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise DomainError(f"无法读取输入文件: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DomainError(f"输入不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DomainError("面板输入必须是 JSON 对象")
    return data


def validate_panel(data: Mapping[str, Any]) -> None:
    for key in REQUIRED_KEYS:
        if key not in data:
            raise DomainError(f"输入缺少必需字段: {key}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise DomainError(f"不支持的 schema_version: {data['schema_version']!r}")
    if not isinstance(data["frame"], list) or not data["frame"]:
        raise DomainError("frame 必须是非空数组")
    if not isinstance(data["turnover"], list):
        raise DomainError("turnover 必须是数组")
    if not isinstance(data["assumption_sets"], list) or not data["assumption_sets"]:
        raise DomainError("assumption_sets 必须是非空数组")
    reference = data["index_reference"]
    if not isinstance(reference, Mapping) or not str(reference.get("vintage") or "").strip():
        raise DomainError("index_reference.vintage 缺失")
    _strict_number(reference.get("value"), "index_reference.value")
    for period in ("base", "current"):
        window = data["periods"].get(period)
        if not isinstance(window, Mapping):
            raise DomainError(f"periods.{period} 缺失")
        start = _parse_instant(window.get("start"), f"periods.{period}.start")
        end = _parse_instant(window.get("end"), f"periods.{period}.end")
        if end < start:
            raise DomainError(f"periods.{period} 的 end 早于 start")


def build_merchants(data: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """装配商户级面板：仅采用 available_at 不晚于 as_of 的记录。

    晚到事实保留在输入中但不参与本期计算；同一商户同一期有多条记录时，
    取可用时刻最新的一条，被取代的记录计入 superseded_records。
    """
    as_of = _parse_instant(data["as_of"], "as_of")
    base_start = _parse_instant(data["periods"]["base"]["start"], "periods.base.start")
    current_end = _parse_instant(data["periods"]["current"]["end"], "periods.current.end")

    merchants: dict[str, dict[str, Any]] = {}
    for row in data["frame"]:
        if not isinstance(row, Mapping):
            raise DomainError("frame 元素必须是对象")
        merchant_id = str(row.get("merchant_id") or "").strip()
        if not merchant_id:
            raise DomainError("frame 存在缺失 merchant_id 的记录")
        if merchant_id in merchants:
            raise DomainError(f"frame 中 merchant_id 重复: {merchant_id}")
        weight = _strict_number(row.get("weight"), f"frame[{merchant_id}].weight")
        if weight <= 0:
            raise DomainError(f"frame[{merchant_id}].weight 必须为正数")
        open_at = _parse_instant(row.get("open_at"), f"frame[{merchant_id}].open_at")
        close_raw = row.get("close_at")
        close_at = _parse_instant(close_raw, f"frame[{merchant_id}].close_at") if close_raw is not None else None
        if close_at is not None and close_at < open_at:
            raise DomainError(f"frame[{merchant_id}].close_at 早于 open_at")
        merchants[merchant_id] = {
            "merchant_id": merchant_id,
            "sector": str(row.get("sector") or "unknown"),
            "size": str(row.get("size") or "unknown"),
            "weight": weight,
            "open_at": open_at,
            "close_at": close_at,
        }

    units = {row.get("unit") for row in data["turnover"] if isinstance(row, Mapping)}
    units.discard(None)
    if len(units) > 1:
        raise DomainError(f"turnover 存在不一致的计量单位: {sorted(units)!r}")

    late_excluded = 0
    superseded = 0
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for row in data["turnover"]:
        if not isinstance(row, Mapping):
            raise DomainError("turnover 元素必须是对象")
        merchant_id = str(row.get("merchant_id") or "").strip()
        if merchant_id not in merchants:
            raise DomainError(f"turnover 引用了框外商户: {merchant_id!r}")
        period = str(row.get("period") or "")
        if period not in ("base", "current"):
            raise DomainError(f"turnover[{merchant_id}] 的 period 非法: {period!r}")
        available_at = _parse_instant(row.get("available_at"), f"turnover[{merchant_id}].available_at")
        if available_at > as_of:
            late_excluded += 1
            continue
        response = str(row.get("response") or "")
        value = row.get("value")
        if response == "confirmed":
            value = _strict_number(value, f"turnover[{merchant_id}].value")
            if value < 0:
                raise DomainError(f"turnover[{merchant_id}].value 为负数")
        elif response == "missing":
            if value is not None:
                raise DomainError(f"turnover[{merchant_id}] 标记 missing 却带有数值")
        else:
            raise DomainError(f"turnover[{merchant_id}] 的 response 非法: {response!r}")
        key = (merchant_id, period)
        existing = records.get(key)
        if existing is not None:
            superseded += 1
            if existing["available_at"] >= available_at:
                continue
        records[key] = {"response": response, "value": value, "available_at": available_at}

    absent_current = 0
    panel: list[dict[str, Any]] = []
    for merchant_id, merchant in merchants.items():
        base_rec = records.get((merchant_id, "base"))
        if base_rec is None or base_rec["response"] != "confirmed":
            raise DomainError(f"商户 {merchant_id} 缺少已确认的 base 期交易额，无法构造相对情景")
        current_rec = records.get((merchant_id, "current"))
        if current_rec is None:
            absent_current += 1
            current_rec = {"response": "missing", "value": None, "available_at": None}
        eligible = merchant["open_at"] < current_end and (
            merchant["close_at"] is None or merchant["close_at"] > base_start
        )
        panel.append({
            **merchant,
            "eligible": eligible,
            "base_value": base_rec["value"],
            "current_response": current_rec["response"],
            "current_value": current_rec["value"],
        })
    panel.sort(key=lambda item: item["merchant_id"])
    stats = {
        "late_records_excluded": late_excluded,
        "superseded_records": superseded,
        "current_record_absent": absent_current,
        "unit": sorted(units)[0] if units else None,
    }
    return panel, stats


# ---------------------------------------------------------------- 情景假设

def _parse_growth_bound(value: Any, field: str, allow_null: bool) -> Optional[float]:
    if value is None:
        if allow_null:
            return None
        raise DomainError(f"{field} 不允许为 null")
    return _strict_number(value, field)


def _normalize_overrides(raw: Any, field: str) -> dict[str, dict[str, Optional[float]]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DomainError(f"{field} 必须是对象")
    overrides: dict[str, dict[str, Optional[float]]] = {}
    for key, bounds in raw.items():
        if not isinstance(bounds, Mapping):
            raise DomainError(f"{field}.{key} 必须是对象")
        low = _parse_growth_bound(bounds.get("min"), f"{field}.{key}.min", allow_null=False)
        high = _parse_growth_bound(bounds.get("max"), f"{field}.{key}.max", allow_null=True)
        if high is not None and low > high:
            raise DomainError(f"{field}.{key} 的 min 大于 max")
        overrides[str(key)] = {"min": low, "max": high}
    return overrides


def normalize_sets(data: Mapping[str, Any], operator_set: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    sets: list[dict[str, Any]] = []
    for raw in data["assumption_sets"]:
        if not isinstance(raw, Mapping):
            raise DomainError("assumption_sets 元素必须是对象")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise DomainError("assumption_sets 存在未命名情景")
        g_min = _parse_growth_bound(raw.get("missing_growth_min"), f"assumption_sets[{name}].missing_growth_min", allow_null=False)
        g_max = _parse_growth_bound(raw.get("missing_growth_max"), f"assumption_sets[{name}].missing_growth_max", allow_null=True)
        if g_max is not None and g_min > g_max:
            raise DomainError(f"assumption_sets[{name}] 的 min 大于 max")
        sets.append({
            "name": name,
            "missing_growth_min": g_min,
            "missing_growth_max": g_max,
            "sector_overrides": _normalize_overrides(raw.get("sector_overrides"), f"assumption_sets[{name}].sector_overrides"),
            "size_overrides": _normalize_overrides(raw.get("size_overrides"), f"assumption_sets[{name}].size_overrides"),
            "origin": "input",
        })
    if operator_set is not None:
        sets.append(operator_set)
    names = [item["name"] for item in sets]
    if len(names) != len(set(names)):
        raise DomainError(f"情景名称重复: {sorted(names)!r}")
    return sets


def _effective_bounds(set_def: Mapping[str, Any], merchant: Mapping[str, Any]) -> tuple[float, Optional[float], bool]:
    """返回 (下界因子, 上界因子或 None, 是否触发非负截断)。行业覆盖优先于规模覆盖。"""
    bounds: dict[str, Optional[float]] = {
        "min": set_def["missing_growth_min"],
        "max": set_def["missing_growth_max"],
    }
    sector_override = set_def["sector_overrides"].get(merchant["sector"])
    size_override = set_def["size_overrides"].get(merchant["size"])
    if sector_override is not None:
        bounds = sector_override
    elif size_override is not None:
        bounds = size_override
    g_min = bounds["min"]
    g_max = bounds["max"]
    assert g_min is not None
    clamped = g_min < GROWTH_FLOOR
    lower_factor = max(0.0, 1.0 + g_min)
    upper_factor = None if g_max is None else max(0.0, 1.0 + g_max)
    return lower_factor, upper_factor, clamped


# ---------------------------------------------------------------- 分析计算

def _classify_direction(lower: Optional[float], upper: Optional[float]) -> str:
    if lower is None or upper is None:
        return "indeterminate"
    if lower > 1.0 + EPS:
        return "growth_holds"
    if upper < 1.0 - EPS:
        return "decline_holds"
    return "reversal_possible"


def _contains_published(published: float, lower: Optional[float], upper: Optional[float]) -> Optional[bool]:
    if lower is not None and published < lower - EPS:
        return False
    if upper is not None and published > upper + EPS:
        return False
    if lower is None or upper is None:
        return None
    return True


def _group_rates(eligible: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for merchant in eligible:
        name = str(merchant[key])
        group = groups.setdefault(name, {
            "merchants": 0, "responders": 0, "missing": 0, "confirmed_zero": 0,
            "weighted_base_total": 0.0, "weighted_base_responded": 0.0, "weighted_base_missing": 0.0,
        })
        weighted_base = merchant["weight"] * merchant["base_value"]
        group["merchants"] += 1
        group["weighted_base_total"] += weighted_base
        if merchant["current_response"] == "confirmed":
            group["responders"] += 1
            group["weighted_base_responded"] += weighted_base
            if merchant["current_value"] == 0:
                group["confirmed_zero"] += 1
        else:
            group["missing"] += 1
            group["weighted_base_missing"] += weighted_base
    for group in groups.values():
        total = group["weighted_base_total"]
        group["response_rate_count"] = _r6(group["responders"] / group["merchants"])
        group["response_rate_weighted_base"] = _r6(group["weighted_base_responded"] / total) if total > 0 else None
        group["weighted_base_total"] = _r6(total)
        group["weighted_base_responded"] = _r6(group["weighted_base_responded"])
        group["weighted_base_missing"] = _r6(group["weighted_base_missing"])
    return dict(sorted(groups.items()))


def compute_coverage(
    data: Mapping[str, Any],
    panel: list[dict[str, Any]],
    collection: Mapping[str, Any],
) -> dict[str, Any]:
    eligible = [m for m in panel if m["eligible"]]
    excluded = [m["merchant_id"] for m in panel if not m["eligible"]]
    responders = [m for m in eligible if m["current_response"] == "confirmed"]
    missing = [m for m in eligible if m["current_response"] == "missing"]
    confirmed_zero = [m for m in responders if m["current_value"] == 0]
    weighted_base_total = sum(m["weight"] * m["base_value"] for m in eligible)
    weighted_base_responded = sum(m["weight"] * m["base_value"] for m in responders)
    weighted_base_missing = weighted_base_total - weighted_base_responded
    closed = [m for m in eligible if m["close_at"] is not None]
    count = len(eligible)
    return {
        "as_of": data["as_of"],
        "periods": data["periods"],
        "frame_merchants": len(panel),
        "eligible_merchants": count,
        "excluded_merchants": sorted(excluded),
        "current_period": {
            "responders": len(responders),
            "confirmed_positive": len(responders) - len(confirmed_zero),
            "confirmed_zero": len(confirmed_zero),
            "missing": len(missing),
            "response_rate_count": _r6(len(responders) / count) if count else None,
            "weighted_base_total": _r6(weighted_base_total),
            "weighted_base_responded": _r6(weighted_base_responded),
            "weighted_base_missing": _r6(weighted_base_missing),
            "response_rate_weighted_base": _r6(weighted_base_responded / weighted_base_total) if weighted_base_total > 0 else None,
            "missing_weighted_base_share": _r6(weighted_base_missing / weighted_base_total) if weighted_base_total > 0 else None,
        },
        "by_sector": _group_rates(eligible, "sector"),
        "by_size": _group_rates(eligible, "size"),
        "closure": {
            "closed_total": len(closed),
            "closed_confirmed_zero": sum(1 for m in closed if m["current_response"] == "confirmed" and m["current_value"] == 0),
            "closed_missing": sum(1 for m in closed if m["current_response"] == "missing"),
            "policy": "close_at 仅作停业事实报告；停业且缺报者不自动记零，仍按情景区间处理",
        },
        "late_records_excluded": collection["late_records_excluded"],
        "superseded_records": collection["superseded_records"],
        "current_record_absent": collection["current_record_absent"],
        "response_mechanism": {
            "missing_assumed_random": False,
            "note": "缺报率随规模与行业不同（见 by_size/by_sector），不采用随机缺失假设，按非随机缺报构造情景",
        },
    }


def compute_distribution(panel: list[dict[str, Any]], unit: Optional[str]) -> dict[str, Any]:
    eligible = [m for m in panel if m["eligible"]]
    responders = [m for m in eligible if m["current_response"] == "confirmed"]
    base_values = [m["base_value"] for m in eligible]
    base_weights = [m["weight"] for m in eligible]
    current_values = [m["current_value"] for m in responders]
    current_weights = [m["weight"] for m in responders]
    matched_ratios = [m["current_value"] / m["base_value"] for m in responders if m["base_value"] > 0]

    weighted_base_total = sum(m["weight"] * m["base_value"] for m in eligible)
    shares = sorted(
        (m["weight"] * m["base_value"] / weighted_base_total for m in eligible if weighted_base_total > 0),
        reverse=True,
    )
    large_weighted_base = sum(m["weight"] * m["base_value"] for m in eligible if m["size"] == "large")
    return {
        "basis": "observed",
        "unit": unit,
        "base": _summarize(base_values, base_weights),
        "current_observed": _summarize(current_values, current_weights),
        "matched_growth_ratio": _summarize(matched_ratios),
        "concentration": {
            "top1_weighted_base_share": _r6(shares[0]) if shares else None,
            "top3_weighted_base_share": _r6(sum(shares[:3])) if len(shares) >= 3 else None,
            "large_size_weighted_base_share": _r6(large_weighted_base / weighted_base_total) if weighted_base_total > 0 else None,
            "hhi_weighted_base": _r6(sum(share * share for share in shares)),
        },
        "note": "仅统计已观测记录（确认零成交计入观测），未含任何情景填补值",
    }


def compute_composition(panel: list[dict[str, Any]]) -> dict[str, Any]:
    """把分层结构变化与实际交易变化分列，不合并为单一结论。

    恒等式：(1 + 朴素变化) = (1 + 结构效应) × (1 + 配对交易效应)，
    其中朴素变化直接用应答者当期加权均值对比全框基期加权均值（混入了结构）。
    """
    eligible = [m for m in panel if m["eligible"]]
    responders = [m for m in eligible if m["current_response"] == "confirmed"]
    weight_all = sum(m["weight"] for m in eligible)
    weight_resp = sum(m["weight"] for m in responders)
    base_all = sum(m["weight"] * m["base_value"] for m in eligible)
    base_resp = sum(m["weight"] * m["base_value"] for m in responders)
    current_resp = sum(m["weight"] * m["current_value"] for m in responders)

    frame_base_mean = base_all / weight_all if weight_all > 0 else None
    resp_base_mean = base_resp / weight_resp if weight_resp > 0 else None
    resp_current_mean = current_resp / weight_resp if weight_resp > 0 else None

    naive = composition = matched = None
    if frame_base_mean and resp_base_mean and resp_current_mean is not None:
        naive = resp_current_mean / frame_base_mean - 1.0
        composition = resp_base_mean / frame_base_mean - 1.0
        matched = resp_current_mean / resp_base_mean - 1.0

    strata: dict[tuple[str, str], dict[str, Any]] = {}
    for merchant in eligible:
        key = (merchant["sector"], merchant["size"])
        stratum = strata.setdefault(key, {
            "sector": key[0], "size": key[1],
            "frame_merchants": 0, "frame_weight": 0.0, "frame_weighted_base": 0.0,
            "responder_merchants": 0, "responder_weight": 0.0, "responder_weighted_base": 0.0,
            "responder_weighted_current": 0.0,
            "missing_merchants": 0, "missing_weighted_base": 0.0,
        })
        weighted_base = merchant["weight"] * merchant["base_value"]
        stratum["frame_merchants"] += 1
        stratum["frame_weight"] += merchant["weight"]
        stratum["frame_weighted_base"] += weighted_base
        if merchant["current_response"] == "confirmed":
            stratum["responder_merchants"] += 1
            stratum["responder_weight"] += merchant["weight"]
            stratum["responder_weighted_base"] += weighted_base
            stratum["responder_weighted_current"] += merchant["weight"] * merchant["current_value"]
        else:
            stratum["missing_merchants"] += 1
            stratum["missing_weighted_base"] += weighted_base
    strata_rows = []
    for key in sorted(strata):
        stratum = strata[key]
        row = {
            "sector": stratum["sector"],
            "size": stratum["size"],
            "frame_merchants": stratum["frame_merchants"],
            "frame_weight_share": _r6(stratum["frame_weight"] / weight_all) if weight_all > 0 else None,
            "responder_weight_share": _r6(stratum["responder_weight"] / weight_resp) if weight_resp > 0 else None,
            "frame_weighted_base": _r6(stratum["frame_weighted_base"]),
            "responder_weighted_base": _r6(stratum["responder_weighted_base"]),
            "missing_merchants": stratum["missing_merchants"],
            "missing_weighted_base": _r6(stratum["missing_weighted_base"]),
            "response_rate_weighted_base": _r6(stratum["responder_weighted_base"] / stratum["frame_weighted_base"]) if stratum["frame_weighted_base"] > 0 else None,
            "matched_growth_ratio": _r6(stratum["responder_weighted_current"] / stratum["responder_weighted_base"]) if stratum["responder_weighted_base"] > 0 else None,
        }
        strata_rows.append(row)
    return {
        "frame_base_weighted_mean": _r6(frame_base_mean),
        "responder_base_weighted_mean": _r6(resp_base_mean),
        "responder_current_weighted_mean": _r6(resp_current_mean),
        "naive_change_mixing_structure": _r6(naive),
        "composition_effect": _r6(composition),
        "matched_transaction_effect": _r6(matched),
        "identity": "(1 + naive_change) = (1 + composition_effect) × (1 + matched_transaction_effect)",
        "strata": strata_rows,
        "note": "结构效应来自应答者与全框的分层权重差异，交易效应来自同一批应答者两期变化；两者分列，不得合并解读",
    }


def compute_envelopes(
    panel: list[dict[str, Any]],
    sets: list[dict[str, Any]],
    published: float,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    eligible = [m for m in panel if m["eligible"]]
    responders = [m for m in eligible if m["current_response"] == "confirmed"]
    missing = [m for m in eligible if m["current_response"] == "missing"]
    base_all = sum(m["weight"] * m["base_value"] for m in eligible)
    base_resp = sum(m["weight"] * m["base_value"] for m in responders)
    current_resp = sum(m["weight"] * m["current_value"] for m in responders)
    base_missing = base_all - base_resp
    totals = {"base_all": base_all, "base_resp": base_resp, "current_resp": current_resp, "base_missing": base_missing}

    envelopes: list[dict[str, Any]] = []
    if base_resp > 0:
        observed_index = current_resp / base_resp
        direction = _classify_direction(observed_index, observed_index)
        envelopes.append({
            "scope": "responders_only",
            "label": "全响应子样本（完全观测，无情景假设）",
            "basis": "observed",
            "missing_weighted_base_share": 0.0,
            "index_lower": _r6(observed_index),
            "index_upper": _r6(observed_index),
            "width": 0.0,
            "bounded": True,
            "direction": direction,
            "direction_note": DIRECTION_NOTES[direction],
            "contains_published_reference": _contains_published(published, observed_index, observed_index),
            "note": "该子样本无缺报，包络收缩为点；全框包络宽度由缺报加权基期占比驱动，占比归零时宽度归零",
        })
    if base_all <= 0:
        return envelopes, totals

    missing_share = base_missing / base_all
    for set_def in sets:
        lower_sum = current_resp
        upper_sum = current_resp
        bounded = True
        clamped = False
        for merchant in missing:
            lower_factor, upper_factor, was_clamped = _effective_bounds(set_def, merchant)
            clamped = clamped or was_clamped
            weighted_base = merchant["weight"] * merchant["base_value"]
            lower_sum += weighted_base * lower_factor
            if upper_factor is None:
                bounded = False
            else:
                upper_sum += weighted_base * upper_factor
        lower = lower_sum / base_all
        upper = upper_sum / base_all if bounded else None
        direction = _classify_direction(lower, upper)
        envelopes.append({
            "scope": "full_frame",
            "assumption_set": set_def["name"],
            "basis": "observed_plus_assumption",
            "missing_growth_range": [set_def["missing_growth_min"], set_def["missing_growth_max"]],
            "differentiated": bool(set_def["sector_overrides"] or set_def["size_overrides"]),
            "nonnegativity_clamped": clamped,
            "missing_weighted_base_share": _r6(missing_share),
            "index_lower": _r6(lower),
            "index_upper": _r6(upper),
            "width": _r6(upper - lower) if upper is not None else None,
            "bounded": bounded,
            "direction": direction,
            "direction_note": DIRECTION_NOTES[direction],
            "contains_published_reference": _contains_published(published, lower, upper),
        })
    return envelopes, totals


def compute_tipping_points(totals: Mapping[str, float], published: float) -> list[dict[str, Any]]:
    """均匀缺报情景下结论翻转的转折点；缺报为零时不存在转折点。"""
    base_all = totals["base_all"]
    current_resp = totals["current_resp"]
    base_missing = totals["base_missing"]
    points: list[dict[str, Any]] = []
    specs = [
        ("growth_boundary", 1.0, "全框指数等于 1（增长与下降分界）"),
        ("published_reference", published, "全框指数等于既有发布值"),
    ]
    for kind, threshold, label in specs:
        entry: dict[str, Any] = {
            "scope": "full_frame",
            "threshold_kind": kind,
            "threshold": _r6(threshold),
            "threshold_label": label,
        }
        if base_missing <= 0 or base_all <= 0:
            entry.update({
                "exists": False,
                "uniform_missing_growth_at_threshold": None,
                "feasible": None,
                "note": "当前无缺报，结论不依赖任何缺报情景，不存在转折点",
            })
        else:
            growth = (threshold * base_all - current_resp) / base_missing - 1.0
            feasible = growth >= GROWTH_FLOOR - EPS
            entry.update({
                "exists": True,
                "uniform_missing_growth_at_threshold": _r6(growth),
                "feasible": feasible,
            })
            if kind == "growth_boundary":
                entry["note"] = (
                    f"缺报商户加权平均相对变化低于 {growth:.1%} 时，全框指数跌破 1，增长结论反转"
                    if feasible else
                    "即使缺报商户交易额全部归零，全框指数仍不低于 1，方向在业务可行域内不会反转"
                )
            else:
                entry["note"] = (
                    f"缺报商户加权平均相对变化需达到 {growth:.1%}，全框指数才与发布值 {_r6(threshold)} 一致"
                    if feasible else
                    "在业务可行域内（交易额非负）无法达到发布值，发布口径与缺报情景需业务复核"
                )
        points.append(entry)
    return points


def build_assumptions(
    data: Mapping[str, Any],
    sets: list[dict[str, Any]],
) -> dict[str, Any]:
    source_versions = data.get("source_versions") or {}
    operations = source_versions.get("operations", "unknown")
    reference = source_versions.get("reference", "unknown")
    clamped_sets = []
    for set_def in sets:
        bounds = [set_def["missing_growth_min"]]
        bounds.extend(item["min"] for item in set_def["sector_overrides"].values())
        bounds.extend(item["min"] for item in set_def["size_overrides"].values())
        if any(bound < GROWTH_FLOOR for bound in bounds if bound is not None):
            clamped_sets.append(set_def["name"])
    return {
        "method": "interval_envelope",
        "method_version": METHOD_VERSION,
        "method_note": "全框指数对缺报值为线性，包络在情景区间角点处精确取得；这是确定性界限与转折点，不是抽样误差线",
        "missingness": {
            "assumed_random": False,
            "note": "缺报率随规模与行业不同（见 coverage.by_size/by_sector），不采用随机缺失假设",
        },
        "imputation": {
            "assumption_values_marked": True,
            "note": "情景填充值仅用于包络计算并标记为 observed_plus_assumption，不作为观测事实输出",
        },
        "nonnegativity": {
            "growth_floor": GROWTH_FLOOR,
            "clamped_sets": clamped_sets,
            "note": "交易额非负，相对变化低于 -100% 的部分按 0 截断",
        },
        "closure_policy": "close_at 仅作停业事实报告；停业且缺报的商户不自动记零，仍按情景区间处理",
        "unbounded_rule": "missing_growth_max 为 null 表示业务未给上界；对应包络上界输出 null（未知），不以 0 或任意点值替代",
        "availability_rule": "仅采用 available_at 不晚于 as_of 的记录；晚到事实保留在输入但不参与本期计算",
        "differentiation_rule": "sector_overrides 优先于 size_overrides，未命中的商户使用该情景全局区间",
        "sets": [
            {
                "name": set_def["name"],
                "missing_growth_min": set_def["missing_growth_min"],
                "missing_growth_max": set_def["missing_growth_max"],
                "sector_overrides": set_def["sector_overrides"],
                "size_overrides": set_def["size_overrides"],
                "origin": set_def["origin"],
            }
            for set_def in sets
        ],
        "source_versions": {
            "turnover": f"operations@{operations}",
            "closure": f"operations@{operations}",
            "weights": f"operations@{operations}",
            "reference": f"reference@{reference}",
            "raw": source_versions,
        },
    }


def compute_study(data: Mapping[str, Any], operator_set: Optional[dict[str, Any]]) -> dict[str, Any]:
    validate_panel(data)
    panel, collection = build_merchants(data)
    sets = normalize_sets(data, operator_set)
    published = _strict_number(data["index_reference"]["value"], "index_reference.value")

    envelopes, totals = compute_envelopes(panel, sets, published)
    reference_version = (data.get("source_versions") or {}).get("reference", "unknown")
    study_id = "cov-" + fingerprint({
        "method_version": METHOD_VERSION,
        "panel": data,
        "operator_set": operator_set,
    })[:32]
    return {
        "study_id": study_id,
        "method_version": METHOD_VERSION,
        "created_at": utc_now(),
        "as_of": data["as_of"],
        "source_versions": data["source_versions"],
        "index_reference": {
            "vintage": str(data["index_reference"]["vintage"]),
            "value": _r6(published),
            "source_version": f"reference@{reference_version}",
            "usage": "仅引用既有发布，不重算正式指数",
        },
        "baseline_case_id": str(data["baseline_case_id"]),
        "coverage": compute_coverage(data, panel, collection),
        "distribution": compute_distribution(panel, collection["unit"]),
        "composition": compute_composition(panel),
        "envelopes": envelopes,
        "tipping_points": compute_tipping_points(totals, published),
        "assumptions": build_assumptions(data, sets),
    }


# ---------------------------------------------------------------- 底稿存储（只增不改）

class StudyStore:
    """coverage_studies 表：同一指数版本可对应多份假设底稿，旧底稿不被覆盖。"""

    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS coverage_studies(
                    study_id TEXT PRIMARY KEY,
                    method_version TEXT NOT NULL,
                    index_vintage TEXT NOT NULL,
                    baseline_case_id TEXT,
                    input_fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS coverage_studies_vintage ON coverage_studies(index_vintage);
            """)
            connection.commit()

    def get(self, study_id: str) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT result FROM coverage_studies WHERE study_id=?", (study_id,)
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def put(self, study: Mapping[str, Any]) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO coverage_studies"
                "(study_id, method_version, index_vintage, baseline_case_id, input_fingerprint, result, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    study["study_id"],
                    study["method_version"],
                    study["index_reference"]["vintage"],
                    study["baseline_case_id"],
                    study["study_id"],
                    json.dumps(study, ensure_ascii=False, sort_keys=True),
                    study["created_at"],
                ),
            )
            connection.commit()

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM coverage_studies").fetchone()[0])

    def vintage_linkage(self, baseline_case_id: str) -> dict[str, Any]:
        """关联既有发布运行（runs 表）；库中尚无该运行时如实报告未关联。"""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT run_id, state, revision FROM runs WHERE case_id=?"
                    " ORDER BY revision DESC, updated_at DESC LIMIT 1",
                    (baseline_case_id,),
                ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            return {
                "linked": False,
                "baseline_case_id": baseline_case_id,
                "reason": "数据库中未找到该 baseline_case_id 的发布运行记录",
            }
        return {
            "linked": True,
            "baseline_case_id": baseline_case_id,
            "run_id": row["run_id"],
            "state": row["state"],
            "revision": int(row["revision"]),
        }


# ---------------------------------------------------------------- 命令行

def _parse_max_argument(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if text.casefold() in UNBOUNDED_TOKENS:
        return None
    try:
        return _strict_number(float(text), "--missing-growth-max")
    except ValueError as exc:
        raise DomainError(f"--missing-growth-max 不是数值或 unbounded: {raw!r}") from exc


def build_operator_set(args: argparse.Namespace) -> Optional[dict[str, Any]]:
    min_given = args.missing_growth_min is not None
    max_given = args.missing_growth_max is not None
    if not (min_given or max_given):
        return None
    g_min = _strict_number(args.missing_growth_min, "--missing-growth-min") if min_given else GROWTH_FLOOR
    g_max = _parse_max_argument(args.missing_growth_max)
    if g_max is not None and g_min > g_max:
        raise DomainError("--missing-growth-min 大于 --missing-growth-max")
    name = str(args.assumption_name or "").strip() or "operator"
    return {
        "name": name,
        "missing_growth_min": g_min,
        "missing_growth_max": g_max,
        "sector_overrides": {},
        "size_overrides": {},
        "origin": "cli",
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    data = load_panel(args.input)
    operator_set = build_operator_set(args)
    study = compute_study(data, operator_set)
    store = StudyStore(args.database)
    existing = store.get(study["study_id"])
    if existing is not None:
        result = existing
        result["replayed"] = True
    else:
        study["vintage_linkage"] = store.vintage_linkage(study["baseline_case_id"])
        store.put(study)
        result = study
        result["replayed"] = False
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="market_index_bureau.coverage_sensitivity",
        description="非应答商户情景下交易景气结论的敏感性包络分析（只读引用正式指数，不重算发布）",
    )
    parser.add_argument("--input", required=True, help="面板 JSON，口径见 fixtures/analysis/fields.md")
    parser.add_argument("--database", required=True, help="vintage SQLite 数据库（与发布流程同库）")
    parser.add_argument("--output", required=True, help="分析底稿 JSON 输出路径")
    parser.add_argument("--missing-growth-min", type=float, default=None,
                        help="运营自定义缺报相对变化下界；单独给出上界时下界默认 -1")
    parser.add_argument("--missing-growth-max", default=None,
                        help="运营自定义缺报相对变化上界；null/unbounded 表示业务未给上界")
    parser.add_argument("--assumption-name", default="operator", help="自定义情景名称（默认 operator）")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except DomainError as exc:
        print(f"分析失败: {exc}", file=sys.stderr)
        return 2
    print(f"study_id={result['study_id']} replayed={str(result['replayed']).lower()} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
