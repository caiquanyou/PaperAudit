"""Conservative, local checks on retained model claims; no I/O or model calls.

The supplied segments must be the source-preserving parser output for this run.
Empirically establishing a log-ratio direction refutes only a claimed arithmetic
inconsistency. It does not establish the authors' complete analysis procedure.
"""
from __future__ import annotations

import copy
import math
import re


VERSION = "local-decision-validation-1"
ABS_TOLERANCE = 1e-7
ROW = re.compile(r"(?:^|\n)\s*R(\d+):\s*([^\n]*)")
LOG2 = re.compile(r"log\s*[_₂]?\s*2|log₂|以\s*2\s*为底", re.I)


def _normal(value):
    return re.sub(r"\s+", " ", value).strip()


def _text(obj, fields):
    return "\n".join(obj.get(k, "") for k in fields if isinstance(obj.get(k), str))


def _verified(items, segments):
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sid, quote = item.get("segment_id"), item.get("quote")
        source = segments.get(sid) if isinstance(sid, str) else None
        if not isinstance(source, dict) or source.get("source_type") == "derived_provenance":
            continue
        if not isinstance(quote, str) or not isinstance(source.get("text"), str):
            continue
        normalized = _normal(quote)
        if len(normalized) < 8 or len(re.findall(r"\w", normalized)) < 4:
            continue
        if normalized not in _normal(source["text"]):
            continue
        # Source identity comes from the trusted segment, never the model's fields.
        result.append({"segment_id": sid, "quote": quote,
                       **{k: copy.deepcopy(v) for k, v in source.items() if k != "text"}})
    return result


def _key(item):
    aid, sheet = item.get("artifact_id"), item.get("sheet")
    return (aid, sheet) if isinstance(aid, str) and isinstance(sheet, str) else None


def _number(value):
    try:
        value = float(value.strip().replace("−", "-"))
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _rows(text):
    return [(int(m.group(1)), [v.strip() for v in m.group(2).split("|")])
            for m in ROW.finditer(text)]


def _diff_header(value):
    value = re.sub(r"[^a-z0-9]", "", value.lower())
    return value in {"diff", "log2fc", "logfc", "log2foldchange", "log2ratio"} or value.endswith("diff")


def _revise(out, action, code, reason, status):
    out.update(action=action, reason_code=code, reason=reason,
               validation_reason=reason, validation_status=status,
               decision_validated=action != "needs_context", merge_into=None)
    if action != "keep":
        out["impact"] = ""
    return out


def _log2_check(candidate, out, segments, evidence):
    anchors = [a for a in candidate.get("anchors", []) if isinstance(a, dict)]
    keys = {_key(a) for a in anchors if _key(a)}
    if not keys:
        keys = {_key(e) for e in evidence if _key(e)}
    unknown = "未能从同表表头、已验证引文和多条独立邻行确定列定义及一致的log2比值方向；计算指控尚未获得本地验证。"
    if len(keys) != 1:
        return _revise(out, "needs_context", "insufficient_context", unknown, "calculation_unresolved")
    key = next(iter(keys))
    table = {}
    conflicts = set()
    for sid, segment in segments.items():
        if not isinstance(segment, dict) or _key(segment) != key or segment.get("source_type") == "derived_provenance":
            continue
        for row, values in _rows(segment.get("text", "")):
            if row in table and table[row][0] != values:
                conflicts.add(row)
            else:
                table[row] = (values, sid)
    # No guessing which numeric columns are treatment and control: require one
    # named difference column, preceded by two distinctly named source columns.
    layouts = []
    for row, (values, sid) in table.items():
        indices = [i for i, value in enumerate(values) if _diff_header(value)]
        if len(indices) == 1:
            i = indices[0]
            if i >= 2 and all(values[j] and _number(values[j]) is None for j in (i - 2, i - 1)) and values[i - 2] != values[i - 1]:
                layouts.append((row, values, sid, i))
    if len(layouts) != 1 or layouts[0][0] in conflicts:
        return _revise(out, "needs_context", "insufficient_context", unknown, "calculation_unresolved")
    _, header, header_sid, diff_col = layouts[0]
    alleged = set()
    for anchor in anchors:
        if _key(anchor) != key:
            continue
        cells = anchor.get("cells")
        if isinstance(cells, list) and len(cells) == 4 and all(type(v) is int for v in cells):
            lo, hi = cells[:2]
            if 1 <= lo <= hi and hi - lo <= 10000:
                alleged.update(range(lo, hi + 1))
        if isinstance(anchor.get("quote"), str):
            alleged.update(row for row, _ in _rows(anchor["quote"]))
    cited = {row for e in evidence if _key(e) == key for row, _ in _rows(e["quote"])}
    if not alleged:
        alleged = set(cited)
    if not alleged or not alleged.intersection(cited) or alleged.intersection(conflicts):
        return _revise(out, "needs_context", "insufficient_context", unknown, "calculation_unresolved")

    numeric = {}
    for row, (values, sid) in table.items():
        if row in conflicts or len(values) < len(header):
            continue
        a, b, observed = (_number(values[i]) for i in (diff_col - 2, diff_col - 1, diff_col))
        if a is None or b is None or observed is None or a <= 0 or b <= 0:
            continue
        expected = math.log2(b) - math.log2(a)
        numeric[row] = {"row": row, "segment_id": sid, "first_value": a,
                        "second_value": b, "observed": observed, "forward": expected}
    if any(row not in numeric for row in alleged):
        return _revise(out, "needs_context", "insufficient_context", unknown, "calculation_unresolved")
    # Independent support rows prevent a disputed row from choosing its own
    # direction. Equal-valued rows cannot establish direction.
    references = [v for row, v in numeric.items() if row not in alleged and abs(v["forward"]) > 2 * ABS_TOLERANCE]
    support = {sign: [v for v in references if abs(v["observed"] - sign * v["forward"]) <= ABS_TOLERANCE]
               for sign in (1, -1)}
    directions = [sign for sign in (1, -1) if len(support[sign]) >= 2
                  and len(support[sign]) >= .9 * len(references) and not support[-sign]]
    if len(directions) != 1:
        return _revise(out, "needs_context", "insufficient_context", unknown, "calculation_unresolved")
    sign = directions[0]
    source_cols = [diff_col - 1, diff_col]  # one-based source column indices
    numerator, denominator = (source_cols[1], source_cols[0]) if sign == 1 else (source_cols[0], source_cols[1])
    formula = f"log2({header[numerator - 1]}/{header[denominator - 1]})"
    out["calculation_records"].append({"kind": "direction_support", "artifact_id": key[0], "sheet": key[1],
        "header_segment_id": header_sid, "formula": formula, "numerator_column": numerator,
        "denominator_column": denominator, "observed_column": diff_col + 1,
        "support_count": len(support[sign]), "reference_count": len(references),
        "support_rows": [v["row"] for v in support[sign]], "absolute_tolerance": ABS_TOLERANCE,
        "scope": "empirical relation in supplied table; not an inferred author threshold or complete analysis method"})
    mismatches = []
    for row in sorted(alleged):
        value = numeric[row]
        expected = sign * value["forward"]
        residual = value["observed"] - expected
        matches = abs(residual) <= ABS_TOLERANCE
        out["calculation_records"].append({"kind": "alleged_row", "artifact_id": key[0], "sheet": key[1],
            **{k: v for k, v in value.items() if k != "forward"}, "formula": formula,
            "expected": expected, "residual": residual, "absolute_tolerance": ABS_TOLERANCE,
            "matches": matches, "cells": [row, row, source_cols[0], diff_col + 1]})
        if not matches:
            mismatches.append(row)
    if not mismatches:
        return _revise(out, "drop", "context_resolved",
            f"本地按同表表头和{len(support[sign])}条非指控行支持的{formula}重算，全部{len(alleged)}条指控实例均在容差{ABS_TOLERANCE:g}内吻合；现有实例不支持算术或方向不一致。此结果不说明整项实验或其他未核对计算通过。",
            "refuted_by_calculation")
    out.update(validation_reason="本地重算发现指控行与同表多数独立行支持的比值关系不一致：" + ", ".join(map(str, mismatches)) + "；保留为待核对事项，归一化、舍入及作者实际公式仍需结合方法判断。",
               validation_status="calculation_discrepancy_supported")
    return out


def _classification_claim(text):
    return bool(re.search(r"阈值|cut.?off|threshold|判定规则|筛选条件|统计依据", text, re.I)
                and re.search(r"分类|判定|标签|标记|NoChange|up.?regulat|down.?regulat|classif|label|末列|最终判定", text, re.I))


def _rule_applies(evidence, keys):
    if not keys:
        return False
    quote = evidence["quote"]
    condition = re.search(r"[<>≤≥=]\s*[-+]?\d|大于|小于|至少|不超过|greater than|less than", quote, re.I)
    criterion = re.search(r"阈值|判定|分类|标记|定义|筛选|\bif\b|\bwhen\b|threshold|cut.?off|classif|label", quote, re.I)
    # A generic significance sentence in a different analysis cannot stand in
    # for a rule attached to this table. Cross-document evidence must name it.
    applies = _key(evidence) in keys or any(sheet.casefold() in quote.casefold() for _, sheet in keys)
    return bool(condition and criterion and applies)


def validate_retained(candidate, decision, segments):
    """Return a fresh decision; modify only an original ``keep`` judgment.

    Optional ``rule_evidence`` and ``contradiction_evidence`` are lists of
    ``{segment_id, quote}`` entries, validated again against supplied originals.
    Call this after model/schema validation and before resolving merge targets.
    """
    out = copy.deepcopy(decision)
    if not isinstance(decision, dict) or decision.get("action") != "keep":
        return out
    out["original_model_decision"] = copy.deepcopy(decision.get("original_model_decision", decision))
    out.update(validation_version=VERSION, validation_status="not_applicable",
               validation_reason="此保留项未触发本地计算、分类规则或惯例比较校验。", calculation_records=[])
    text = _text(candidate, ("title", "observation")) + "\n" + _text(decision, ("reason", "impact"))
    evidence = _verified(decision.get("evidence"), segments)
    if _classification_claim(text):
        anchors = [a for a in candidate.get("anchors", []) if isinstance(a, dict)]
        keys = {_key(a) for a in anchors if _key(a)} or {_key(e) for e in evidence if _key(e)}
        rules = [e for e in _verified(decision.get("rule_evidence"), segments) if _rule_applies(e, keys)]
        out["validated_rule_evidence"] = rules
        if not rules:
            return _revise(out, "drop", "coverage_only",
                "现有复核未提供明确适用于该表或分析的分类规则原文，不能凭数值接近某个默认阈值或其他实验的显著性说明认定标签错误；仅保留为判定规则尚未确认的范围缺口。",
                "classification_rule_unestablished")
        out.update(validation_status="classification_rule_source_verified",
                   validation_reason="已核验指向本表或分析的明确分类规则原文；保留模型提出的具体违规实例供核对，本地未推定未声明阈值。")
        return out
    if LOG2.search(text) and re.search(r"公式|比值|计算|折算|方向|符号|不一致|diff|ratio|calculat|inconsisten|sign", text, re.I):
        return _log2_check(candidate, out, segments, evidence)
    if re.search(r"公式|formula|参数|parameter", text, re.I) and re.search(r"常规|常见|惯例|非常用|usual|conventional|standard|common", text, re.I):
        contradictions = _verified(decision.get("contradiction_evidence"), segments)
        # Reprinting one assertion on two pages is still only one assertion.
        unique = {_normal(e["quote"]) for e in contradictions}
        out["validated_contradiction_evidence"] = contradictions
        if len(unique) < 2:
            return _revise(out, "needs_context", "insufficient_context",
                "现有依据仅比较常见做法，尚无两处可定位的内部冲突陈述或本地计算支持；不能将采用不同公式或参数本身视为错误，需确认实际定义和计算依据。",
                "convention_comparison_unsubstantiated")
        out.update(validation_status="contradiction_sources_verified",
                   validation_reason="两处不同的内部冲突依据均可在原文定位，保留待核对；此校验确认来源，不替代对实验条件是否相同的判断。")
    return out
