"""Synthetic, in-memory regression cases for local scientific claim validation."""
import copy
import math
import unittest

from desktop_app.decision_validation import validate_retained


class DecisionValidationTests(unittest.TestCase):
    def setup_case(self, direction=1, mismatch=0, header=True, zero=False):
        lines = ["R1: ID | Gene | Untreated | Treated | Effect_Diff | Type"] if header else []
        pairs = [(12, 24), (18, 9), (5, 20), (7, 28)]
        for i, (a, b) in enumerate(pairs, 2):
            if zero:
                b = a
            value = direction * math.log2(b / a) + (mismatch if i == 5 else 0)
            lines.append(f"R{i}: {i} | G{i} | {a} | {b} | {value:.9f} | Other")
        segments = {"table": {"artifact_id": "book", "sheet": "Expression comparison", "row_start": 1, "text": "\n".join(lines)}}
        quote = lines[-1]
        candidate = {"title": "log2计算方向不一致", "kind": "table", "observation": "应核对log2比值与Effect_Diff。",
                     "anchors": [{"artifact_id": "book", "sheet": "Expression comparison", "cells": [5, 5, 1, 6], "quote": quote}]}
        decision = {"candidate_id": "synthetic", "action": "keep", "reason_code": "supported_concern",
                    "reason": "引用行与其他行方向不同，可能造成比值计算错误。", "impact": "差值符号会影响后续比较。",
                    "evidence": [{"segment_id": "table", "quote": quote}], "decision_validated": True}
        return candidate, decision, segments

    def test_matching_rows_are_refuted_by_machine_calculation(self):
        c, d, s = self.setup_case()
        out = validate_retained(c, d, s)
        self.assertEqual((out["action"], out["reason_code"]), ("drop", "context_resolved"))
        self.assertTrue(out["calculation_records"][-1]["matches"])
        self.assertEqual(out["calculation_records"][0]["numerator_column"], 4)
        self.assertEqual(out["original_model_decision"], d)

    def test_actual_inconsistency_is_kept(self):
        c, d, s = self.setup_case(mismatch=.4)
        out = validate_retained(c, d, s)
        self.assertEqual(out["action"], "keep")
        self.assertEqual(out["validation_status"], "calculation_discrepancy_supported")
        self.assertAlmostEqual(out["calculation_records"][-1]["residual"], .4)

    def test_reversed_but_consistent_direction_is_not_an_error(self):
        c, d, s = self.setup_case(direction=-1)
        out = validate_retained(c, d, s)
        self.assertEqual(out["action"], "drop")
        self.assertEqual(out["calculation_records"][0]["numerator_column"], 3)

    def test_unknown_direction_or_header_is_pending(self):
        for options in ({"zero": True}, {"header": False}):
            with self.subTest(options=options):
                c, d, s = self.setup_case(**options)
                self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def test_disputed_row_cannot_establish_its_own_direction(self):
        c, d, s = self.setup_case()
        s["table"]["text"] = "\n".join(s["table"]["text"].splitlines()[0:2] + s["table"]["text"].splitlines()[-1:])
        self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def test_conflicting_reference_directions_are_pending(self):
        c, d, s = self.setup_case()
        s["table"]["text"] = s["table"]["text"].replace("18 | 9 | -1.000000000", "18 | 9 | 1.000000000")
        self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def test_zero_input_is_not_repaired_with_an_invented_pseudocount(self):
        c, d, s = self.setup_case()
        s["table"]["text"] = s["table"]["text"].replace("7 | 28", "0 | 28")
        d["evidence"][0]["quote"] = s["table"]["text"].splitlines()[-1]
        c["anchors"][0]["quote"] = d["evidence"][0]["quote"]
        self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def test_unverified_or_derived_evidence_cannot_refute_a_candidate(self):
        for kind in ("made_up", "metadata"):
            c, d, s = self.setup_case()
            if kind == "made_up":
                d["evidence"][0]["quote"] = "R5: 5 | G5 | 999 | 28 | 2 | Other"
            else:
                s["table"]["source_type"] = "derived_provenance"
            self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def classification_case(self):
        c, d, s = self.setup_case()
        c["title"] = "分类标签与判定阈值不一致"
        d["reason"] = "该行值超出声称阈值但类别标签没有变化。"
        return c, d, s

    def test_missing_classification_rule_is_only_a_coverage_gap(self):
        c, d, s = self.classification_case()
        out = validate_retained(c, d, s)
        self.assertEqual((out["action"], out["reason_code"]), ("drop", "coverage_only"))
        self.assertEqual(out["calculation_records"], [])

    def test_source_rule_with_real_violation_can_be_retained(self):
        c, d, s = self.classification_case()
        quote = "Expression comparison: values are classified Up if Effect_Diff > 1."
        s["method"] = {"artifact_id": "paper", "page": 3, "text": quote}
        d["rule_evidence"] = [{"segment_id": "method", "quote": quote}]
        # The cited row has Effect_Diff=2 but Type=Other.
        out = validate_retained(c, d, s)
        self.assertEqual(out["action"], "keep")
        self.assertEqual(len(out["validated_rule_evidence"]), 1)

    def test_other_analysis_or_fabricated_rule_does_not_apply(self):
        for quote in ("Another assay: values are classified Up if Effect_Diff > 1.",
                      "A p-value < .05 was considered statistically significant."):
            c, d, s = self.classification_case()
            s["method"] = {"artifact_id": "paper", "page": 3, "text": quote}
            d["rule_evidence"] = [{"segment_id": "method", "quote": quote}]
            self.assertEqual(validate_retained(c, d, s)["reason_code"], "coverage_only")
        c, d, s = self.classification_case()
        d["rule_evidence"] = [{"segment_id": "table", "quote": "The threshold is 1 for Expression comparison."}]
        self.assertEqual(validate_retained(c, d, s)["reason_code"], "coverage_only")

    def convention_case(self):
        c = {"title": "体积公式不同于常规方法", "observation": "文中系数与常见公式不同。", "anchors": []}
        quote = "All animal volumes used V = 0.3 * L * W * H."
        s = {"a": {"artifact_id": "paper", "page": 1, "text": quote}}
        d = {"action": "keep", "reason_code": "supported_concern", "reason": "公式可能影响所有体积曲线。",
             "evidence": [{"segment_id": "a", "quote": quote}]}
        return c, d, s

    def test_conventional_formula_preference_is_not_evidence_of_error(self):
        c, d, s = self.convention_case()
        self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def test_two_real_conflicting_sources_allow_retention(self):
        c, d, s = self.convention_case()
        second = "All animal volumes used V = 0.5 * L * W * H."
        s["b"] = {"artifact_id": "paper", "page": 7, "text": second}
        d["contradiction_evidence"] = d["evidence"] + [{"segment_id": "b", "quote": second}]
        self.assertEqual(validate_retained(c, d, s)["action"], "keep")
        d["contradiction_evidence"][1]["quote"] = "This is an invented alternative formula."
        self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def test_repeated_same_statement_is_not_two_conflict_sources(self):
        c, d, s = self.convention_case()
        s["b"] = {**s["a"], "page": 4}
        d["contradiction_evidence"] = d["evidence"] + [{"segment_id": "b", "quote": s["b"]["text"]}]
        self.assertEqual(validate_retained(c, d, s)["action"], "needs_context")

    def test_inputs_are_immutable_and_non_keep_is_unchanged(self):
        c, d, s = self.setup_case()
        before = copy.deepcopy((c, d, s))
        out = validate_retained(c, d, s)
        self.assertEqual((c, d, s), before)
        out["original_model_decision"]["evidence"].clear()
        self.assertEqual((c, d, s), before)
        for action in ("drop", "merge", "needs_context"):
            d["action"] = action
            result = validate_retained(c, d, s)
            self.assertEqual(result, d)
            self.assertIsNot(result, d)


if __name__ == "__main__":
    unittest.main()
