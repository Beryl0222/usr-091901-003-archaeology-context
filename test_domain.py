"""领域纯规则测试：空间层级、几何冲突、证据评分重算、公众脱敏。"""

import unittest

from domain import (
    DomainError,
    geometry_overlap,
    normalize_evidence,
    parent_allowed,
    parse_ts,
    public_entity,
    recompute_conclusions,
)


class HierarchyTest(unittest.TestCase):
    def test_spatial_chain_parents(self):
        self.assertTrue(parent_allowed("stratum", "site"))
        self.assertTrue(parent_allowed("feature", "stratum"))
        self.assertTrue(parent_allowed("find", "feature"))
        self.assertTrue(parent_allowed("sample", "find"))
        self.assertTrue(parent_allowed("sample", "feature"))
        # 不可断裂：器物不能直挂发掘区，样品不能直挂地层
        self.assertFalse(parent_allowed("find", "site"))
        self.assertFalse(parent_allowed("sample", "stratum"))
        self.assertFalse(parent_allowed("feature", "find"))

    def test_geometry_overlap_rules(self):
        box = {"type": "box", "min": [0, 0], "max": [10, 10]}
        self.assertTrue(geometry_overlap({"type": "point", "coordinates": [5, 5]}, box))
        self.assertFalse(geometry_overlap({"type": "point", "coordinates": [11, 5]}, box))
        self.assertTrue(geometry_overlap(box, {"type": "box", "min": [9, 9], "max": [20, 20]}))
        self.assertFalse(geometry_overlap(box, {"type": "box", "min": [11, 11], "max": [20, 20]}))
        self.assertTrue(geometry_overlap(
            {"type": "point", "coordinates": [0, 0]},
            {"type": "point", "coordinates": [0.3, 0.3]}))
        self.assertFalse(geometry_overlap(
            {"type": "point", "coordinates": [0, 0]},
            {"type": "point", "coordinates": [2, 2]}))


class EvidenceTest(unittest.TestCase):
    def _ev(self, eid, etype, hyp, conf=0.9, claim="owner", rng=None):
        return {"id": eid, "subject_id": "M1", "type": etype, "claim_kind": claim,
                "hypothesis": hyp, "label": hyp, "confidence": conf,
                "status": "active", "date_range_bp": rng}

    def test_competing_hypotheses_start_contested(self):
        evs = [self._ev("e1", "碳十四测年", "甲", 0.5),
               self._ev("e2", "铭文释读", "乙", 0.95)]
        result = recompute_conclusions(evs, "M1")
        self.assertEqual(len(result), 1)
        conclusion = result[0]
        self.assertEqual(conclusion["winner"]["hypothesis"], "甲")  # 5*0.75 > 3*0.975
        self.assertTrue(conclusion["contested"])
        self.assertEqual(len(conclusion["contenders"]), 1)

    def test_strong_convergent_evidence_resolves(self):
        evs = [self._ev("e1", "碳十四测年", "甲", 0.9),
               self._ev("e2", "铭文释读", "乙", 0.9),
               self._ev("e3", "人骨测年", "甲", 1.0)]
        conclusion = recompute_conclusions(evs, "M1")[0]
        self.assertEqual(conclusion["winner"]["hypothesis"], "甲")
        self.assertFalse(conclusion["contested"])
        self.assertGreaterEqual(conclusion["winner"]["share"], 0.6)

    def test_voided_evidence_excluded(self):
        evs = [self._ev("e1", "碳十四测年", "甲", 1.0),
               dict(self._ev("e2", "铭文释读", "乙", 1.0), status="void")]
        conclusion = recompute_conclusions(evs, "M1")[0]
        self.assertEqual(conclusion["winner"]["hypothesis"], "甲")
        self.assertEqual(conclusion["evidence_count"], 1)

    def test_invalid_bp_range_rejected(self):
        with self.assertRaises(DomainError):
            normalize_evidence({"subject_id": "M1", "type": "碳十四测年",
                                "claim_kind": "date", "hypothesis": "秦代",
                                "date_range_bp": [100, 300]})

    def test_parse_time_defaults_cst(self):
        dt = parse_ts("2026-04-02T09:00:00")
        self.assertEqual(dt.utctimetuple().tm_hour, 1)


class PublicRedactionTest(unittest.TestCase):
    def test_public_entity_has_no_coordinates(self):
        view = public_entity({
            "id": "M1", "kind": "feature", "code": "M1", "name": "陪葬墓",
            "geometry": {"type": "point", "coordinates": [43.2, 44.1]},
            "public_note": "已公开概述", "region": "上召窑"})
        self.assertNotIn("geometry", view)
        self.assertEqual(view["region"], "上召窑")
        self.assertEqual(view["kind_cn"], "遗迹单位")


if __name__ == "__main__":
    unittest.main()
