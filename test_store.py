"""事件存储测试：空间链、离线合并、冲突复核、交接责任、发布冻结与全链路还原。"""

import tempfile
import unittest
from pathlib import Path

from store import ContextStore
from domain import DomainError


def register(eid, kind, code, parent=None, occurred="2026-04-02T09:00:00+08:00",
             actor="测绘员甲", geometry=None, **extra):
    payload = {"id": eid, "kind": kind, "code": code, **extra}
    if parent:
        payload["parent_id"] = parent
    if geometry:
        payload["geometry"] = geometry
    return {"event_id": f"evt-{eid}", "type": "entity_registered",
            "actor": actor, "occurred_at": occurred, "payload": payload}


def custody(eid, obj, action, responsible, frm, to, occurred, actor="库房管理员"):
    return {"event_id": f"evt-{eid}", "type": "custody_transferred",
            "actor": actor, "occurred_at": occurred,
            "payload": {"object_id": obj, "action": action,
                        "from_party": frm, "to_party": to, "responsible": responsible}}


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.store = ContextStore()
        self.store.submit_batch([
            register("Z", "site", "I区", geometry={"type": "box", "min": [0, 0], "max": [200, 200]}),
            register("L", "stratum", "第3层", "Z", occurred="2026-04-02T10:00:00+08:00"),
            register("M1", "feature", "M1", "L",
                     occurred="2026-04-03T09:00:00+08:00",
                     geometry={"type": "box", "min": [40, 40], "max": [46, 48]}),
        ])

    def test_unbreakable_chain_confirmed(self):
        self.assertEqual(self.store.entity("M1")["status"], "已确认")
        result = self.store.submit_batch([
            register("D1", "find", "铜鼎01", "M1",
                     occurred="2026-04-03T10:00:00+08:00",
                     geometry={"type": "point", "coordinates": [43.2, 44.1]})])
        self.assertEqual(len(result["accepted"]), 1)
        chain = self.store.ancestors_chain("D1")
        self.assertEqual([c["code"] for c in chain], ["铜鼎01", "M1", "第3层", "I区"])

    def test_find_cannot_hang_directly_on_site(self):
        result = self.store.submit_batch([
            register("BAD", "find", "野挂器", "Z",
                     occurred="2026-04-03T11:00:00+08:00")])
        self.assertEqual(len(result["rejected"]), 1)
        self.assertNotIn("BAD", self.store.entities)

    def test_sample_needs_find_or_feature(self):
        bad = self.store.submit_batch([
            register("SBAD", "sample", "土样X", "Z",
                     occurred="2026-04-03T11:30:00+08:00")])
        self.assertEqual(len(bad["rejected"]), 1)
        ok = self.store.submit_batch([
            register("SOK", "sample", "骨样01", "M1",
                     occurred="2026-04-03T12:00:00+08:00", actor="田野队员乙")])
        self.assertEqual(len(ok["accepted"]), 1)


class OfflineMergeTest(unittest.TestCase):
    def test_orphan_from_offline_device_reviewed_then_auto_resolves(self):
        store = ContextStore()
        # 平板离线先登记器物，父级尚未同步
        first = store.submit_batch([
            {"event_id": "e-find", "device_id": "tab-7", "device_seq": 3,
             "type": "entity_registered", "actor": "队员乙",
             "occurred_at": "2026-04-03T16:00:00+08:00",
             "payload": {"id": "D9", "kind": "find", "code": "陶罐09", "parent_id": "M9"}}])
        self.assertEqual(len(first["accepted"]), 1)
        self.assertEqual(store.entity("D9")["status"], "待复核")
        self.assertEqual(first["conflicts"][0]["kind"], "context_mismatch")

        # 设备回联补传更早发生的发掘区/地层/墓葬
        store.submit_batch([
            {"event_id": "e-site", "device_id": "tab-7", "device_seq": 0,
             "type": "entity_registered", "actor": "队员甲",
             "occurred_at": "2026-04-02T08:00:00+08:00",
             "payload": {"id": "Z9", "kind": "site", "code": "IX区",
                         "geometry": {"type": "box", "min": [0, 0], "max": [300, 300]}}},
            {"event_id": "e-lay", "device_id": "tab-7", "device_seq": 1,
             "type": "entity_registered", "actor": "队员甲",
             "occurred_at": "2026-04-02T09:00:00+08:00",
             "payload": {"id": "L9", "kind": "stratum", "code": "第2层", "parent_id": "Z9"}},
            {"event_id": "e-feat", "device_id": "tab-7", "device_seq": 2,
             "type": "entity_registered", "actor": "队员乙",
             "occurred_at": "2026-04-03T15:00:00+08:00",
             "payload": {"id": "M9", "kind": "feature", "code": "M9", "parent_id": "L9",
                         "geometry": {"type": "box", "min": [10, 10], "max": [20, 20]}}}])
        # 父级补齐后票单自动解除，无需人工
        self.assertEqual(store.entity("D9")["status"], "已确认")
        self.assertEqual(store.list_tickets("open"), [])

    def test_idempotent_resend_by_event_id_and_device_seq(self):
        store = ContextStore()
        event = {"event_id": "dup1", "device_id": "tab-1", "device_seq": 1,
                 "type": "entity_registered", "actor": "甲",
                 "occurred_at": "2026-04-02T08:00:00+08:00",
                 "payload": {"id": "S1", "kind": "site", "code": "S1"}}
        first = store.submit_batch([dict(event)])
        second = store.submit_batch([dict(event)])
        self.assertEqual(len(first["accepted"]), 1)
        self.assertEqual(len(second["duplicates"]), 1)
        self.assertEqual(len(store.events), 1)

        # 不同 event_id 但相同设备事件号 => 仍判重复，不产生新实体
        clash = dict(event, event_id="dup2",
                     payload={"id": "S2", "kind": "site", "code": "S2"})
        third = store.submit_batch([clash])
        self.assertEqual(len(third["duplicates"]), 1)
        self.assertNotIn("S2", store.entities)

    def test_recorded_vs_occurred_time_kept(self):
        store = ContextStore()
        store.submit_batch([
            {"event_id": "late1", "type": "entity_registered", "actor": "甲",
             "occurred_at": "2026-04-02T08:00:00+08:00",
             "recorded_at": "2026-04-10T20:00:00+08:00",
             "payload": {"id": "S1", "kind": "site", "code": "S1"}}])
        prov = store.provenience("S1")
        self.assertEqual(prov["registration"]["occurred_at"][:10], "2026-04-02")
        self.assertEqual(prov["registration"]["recorded_at"][:10], "2026-04-10")


class ConflictReviewTest(unittest.TestCase):
    def setUp(self):
        self.store = ContextStore()
        self.store.submit_batch([
            register("Z", "site", "I区", geometry={"type": "box", "min": [0, 0], "max": [200, 200]}),
            register("L", "stratum", "第3层", "Z", occurred="2026-04-02T10:00:00+08:00"),
            register("M1", "feature", "M1", "L", occurred="2026-04-03T09:00:00+08:00",
                     geometry={"type": "box", "min": [40, 40], "max": [46, 48]}),
        ])

    def test_duplicate_code_opens_ticket_not_overwrite(self):
        result = self.store.submit_batch([
            register("M1b", "feature", "M1", "L",
                     occurred="2026-04-08T09:00:00+08:00",
                     geometry={"type": "box", "min": [80, 80], "max": [86, 88]})])
        self.assertEqual(result["conflicts"][0]["kind"], "duplicate_code")
        # 原记录未被覆盖：两者都在、都待复核
        self.assertEqual(self.store.entity("M1")["code"], "M1")
        self.assertEqual(self.store.entity("M1")["status"], "待复核")
        self.assertEqual(self.store.entity("M1b")["status"], "待复核")
        ticket = self.store.list_tickets("open")[0]
        self.assertEqual(len(ticket["candidates"]), 2)

    def test_position_conflict_between_siblings(self):
        self.store.submit_batch([
            register("D1", "find", "陶碗01", "M1", occurred="2026-04-03T10:00:00+08:00",
                     geometry={"type": "point", "coordinates": [43.0, 44.0]})])
        result = self.store.submit_batch([
            register("D2", "find", "陶碗02", "M1", occurred="2026-04-03T11:00:00+08:00",
                     geometry={"type": "point", "coordinates": [43.2, 44.1]})])
        self.assertEqual(result["conflicts"][0]["kind"], "position_overlap")

    def test_review_keep_old_dismisses_new(self):
        self.store.submit_batch([
            register("M1b", "feature", "M1", "L",
                     occurred="2026-04-08T09:00:00+08:00",
                     geometry={"type": "box", "min": [80, 80], "max": [86, 88]})])
        ticket = next(t for t in self.store.list_tickets("open") if t["kind"] == "duplicate_code")
        self.store.resolve_ticket("项目负责人", ticket["id"], "维持原记录",
                                  "坐标不同，新记录编号笔误", occurred_at="2026-04-09T09:00:00+08:00")
        self.assertEqual(self.store.entity("M1")["status"], "已确认")
        self.assertEqual(self.store.entity("M1b")["status"], "驳回")
        resolved = self.store.list_tickets("resolved")[0]
        self.assertEqual(resolved["resolutions"][0]["reviewer"], "项目负责人")
        self.assertEqual(resolved["resolutions"][0]["rationale"], "坐标不同，新记录编号笔误")

    def test_review_keep_both_records_aliases(self):
        # 两支队伍在同一位置各编一号（位置冲突而非重号），双记并存并互记别名
        self.store.submit_batch([
            register("D1", "find", "甲编-07", "M1",
                     occurred="2026-04-03T10:00:00+08:00",
                     geometry={"type": "point", "coordinates": [43.0, 44.0]}),
            register("D2", "find", "乙编-12", "M1",
                     occurred="2026-04-03T11:00:00+08:00",
                     geometry={"type": "point", "coordinates": [43.1, 44.1]})])
        ticket = next(t for t in self.store.list_tickets("open")
                      if t["kind"] == "position_overlap")
        self.store.resolve_ticket("项目负责人", ticket["id"], "双记并存",
                                  "两套编号指向同一位置，全部保留",
                                  occurred_at="2026-04-09T09:00:00+08:00")
        self.assertEqual(self.store.entity("D1")["status"], "已确认")
        self.assertEqual(self.store.entity("D2")["status"], "已确认")
        self.assertIn("乙编-12", self.store.entity("D1")["aliases"])
        self.assertIn("甲编-07", self.store.entity("D2")["aliases"])

    def test_reopen_after_new_conflict_keeps_history(self):
        self.store.submit_batch([
            register("M1b", "feature", "M1", "L",
                     occurred="2026-04-08T09:00:00+08:00",
                     geometry={"type": "box", "min": [80, 80], "max": [86, 88]})])
        ticket = next(t for t in self.store.list_tickets("open") if t["kind"] == "duplicate_code")
        tid = ticket["id"]
        self.store.resolve_ticket("项目负责人", tid, "维持原记录", "第一次裁定",
                                  occurred_at="2026-04-09T09:00:00+08:00")
        # 第三个队伍再次登记 M1
        self.store.submit_batch([
            register("M1c", "feature", "M1", "L",
                     occurred="2026-04-12T09:00:00+08:00",
                     geometry={"type": "box", "min": [120, 120], "max": [126, 128]})])
        reopened = self.store.tickets[tid]
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(reopened["reopen_count"], 1)
        self.assertEqual(len(reopened["resolutions"]), 1)
        self.assertEqual(len(reopened["candidates"]), 3)


class CustodyTest(unittest.TestCase):
    def setUp(self):
        self.store = ContextStore()
        self.store.submit_batch([
            register("Z", "site", "I区", geometry={"type": "box", "min": [0, 0], "max": [200, 200]}),
            register("L", "stratum", "第3层", "Z", occurred="2026-04-02T10:00:00+08:00"),
            register("M1", "feature", "M1", "L", occurred="2026-04-03T09:00:00+08:00"),
            register("D1", "find", "铜鼎01", "M1", occurred="2026-04-03T10:00:00+08:00"),
        ])

    def test_each_transfer_keeps_responsible_party(self):
        self.store.submit_batch([
            custody("c1", "D1", "送检", "张三", "田野队", "碳十四实验室",
                    "2026-04-05T09:00:00+08:00"),
            custody("c2", "D1", "暂存", "李四", "碳十四实验室", "工地暂存柜",
                    "2026-04-20T09:00:00+08:00", actor="检测机构"),
            custody("c3", "D1", "修复", "王五", "工地暂存柜", "修复室",
                    "2026-04-25T09:00:00+08:00", actor="修复师"),
            custody("c4", "D1", "入库", "赵六", "修复室", "遗址博物馆库房",
                    "2026-05-02T09:00:00+08:00"),
        ])
        chain = self.store.provenience("D1")["custody_chain"]
        self.assertEqual([c["action"] for c in chain], ["送检", "暂存", "修复", "入库"])
        self.assertEqual([c["responsible"] for c in chain], ["张三", "李四", "王五", "赵六"])
        self.assertEqual([c["to_party"] for c in chain[:2]], ["碳十四实验室", "工地暂存柜"])
        self.assertEqual(self.store.entity("D1")["status"], "已入库")
        self.assertEqual(self.store.entity("D1")["current_holder"], "遗址博物馆库房")

    def test_late_historical_transfer_inserted_by_occurred_time(self):
        # 先录入库（晚），再补录暂存（早）
        self.store.submit_batch([
            custody("c-late", "D1", "入库", "赵六", "修复室", "主库房",
                    "2026-05-02T09:00:00+08:00"),
            custody("c-early", "D1", "暂存", "李四", "实验室", "暂存柜",
                    "2026-04-20T09:00:00+08:00"),
        ])
        chain = self.store.provenience("D1")["custody_chain"]
        self.assertEqual([c["at"][:10] for c in chain], ["2026-04-20", "2026-05-02"])

    def test_transfer_blocked_while_in_review(self):
        self.store.submit_batch([
            register("D1b", "find", "铜鼎01", "M1",
                     occurred="2026-04-07T10:00:00+08:00",
                     geometry={"type": "point", "coordinates": [0, 0]})])
        result = self.store.submit_batch([
            custody("cx", "D1b", "入库", "赵六", "工地", "库房",
                    "2026-04-09T09:00:00+08:00")])
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["rejected"][0]["code"], "object_in_review")

    def test_transfer_requires_responsible(self):
        bad = {"event_id": "bad1", "type": "custody_transferred",
               "actor": "库房管理员", "occurred_at": "2026-04-05T09:00:00+08:00",
               "payload": {"object_id": "D1", "action": "入库",
                           "from_party": "工地", "to_party": "库房"}}
        result = self.store.submit_batch([bad])
        self.assertEqual(len(result["rejected"]), 1)


class EvidenceRecomputeTest(unittest.TestCase):
    def setUp(self):
        self.store = ContextStore()
        self.store.submit_batch([
            register("Z", "site", "I区", geometry={"type": "box", "min": [0, 0], "max": [200, 200]}),
            register("L", "stratum", "第3层", "Z", occurred="2026-04-02T10:00:00+08:00"),
            register("M1", "feature", "M1", "L", occurred="2026-04-03T09:00:00+08:00"),
        ])

    def _owner_evidence(self, eid, etype, hyp, label, conf, at):
        return self.store.add_evidence(
            "研究人员",
            {"id": eid, "subject_id": "M1", "attached_entity_id": "M1",
             "type": etype, "claim_kind": "owner",
             "hypothesis": hyp, "label": label, "confidence": conf},
            occurred_at=at)

    def test_new_evidence_can_flip_conclusion_without_deleting_old(self):
        self._owner_evidence("ev-type", "器物类型学", "H-B", "乙墓主", 0.95,
                             "2026-05-01T10:00:00+08:00")
        first = self.store.current_conclusions("M1")
        self.assertEqual(first["conclusions"][0]["winner"]["hypothesis"], "H-B")

        self._owner_evidence("ev-c14", "碳十四测年", "H-A", "甲墓主", 0.95,
                             "2026-05-10T10:00:00+08:00")
        self._owner_evidence("ev-bone", "人骨测年", "H-A", "甲墓主", 0.95,
                             "2026-05-11T10:00:00+08:00")
        second = self.store.current_conclusions("M1")
        self.assertEqual(second["conclusions"][0]["winner"]["hypothesis"], "H-A")
        # 旧证据仍在竞争者列表中
        contenders = {c["hypothesis"] for c in second["conclusions"][0]["contenders"]}
        self.assertIn("H-B", contenders)
        # 版本随证据集合变化，历史保留两个版本
        self.assertNotEqual(first["evidence_version"], second["evidence_version"])
        history = self.store.conclusion_history["M1"]
        self.assertGreaterEqual(len(history), 3)

    def test_voiding_triggers_recompute_but_evidence_record_remains(self):
        self._owner_evidence("ev-a", "碳十四测年", "H-A", "甲", 1.0,
                             "2026-05-01T10:00:00+08:00")
        self._owner_evidence("ev-b", "铭文释读", "H-B", "乙", 1.0,
                             "2026-05-02T10:00:00+08:00")
        self.store.submit_batch([{
            "event_id": "void1", "type": "evidence_voided", "actor": "研究人员",
            "occurred_at": "2026-05-03T10:00:00+08:00",
            "payload": {"evidence_id": "ev-b", "reason": "拓片伪作，释读撤销"}}])
        conclusion = self.store.current_conclusions("M1")["conclusions"][0]
        self.assertEqual(conclusion["winner"]["hypothesis"], "H-A")
        self.assertEqual(conclusion["evidence_count"], 1)
        self.assertEqual(self.store.evidence["ev-b"]["status"], "void")
        self.assertEqual(self.store.evidence["ev-b"]["void_reason"], "拓片伪作，释读撤销")

    def test_competing_date_and_owner_claims_coexist(self):
        self.store.add_evidence(
            "检测机构", {"id": "d1", "subject_id": "M1", "attached_entity_id": "M1",
                        "type": "碳十四测年", "claim_kind": "date",
                        "hypothesis": "战国晚期", "date_range_bp": [2350, 2220],
                        "confidence": 0.9},
            occurred_at="2026-05-01T10:00:00+08:00")
        self._owner_evidence("o1", "铭文释读", "H-A", "甲墓主", 0.9,
                             "2026-05-02T10:00:00+08:00")
        kinds = {c["claim_kind"] for c in self.store.current_conclusions("M1")["conclusions"]}
        self.assertEqual(kinds, {"date", "owner"})


class PublishFreezeTest(unittest.TestCase):
    def setUp(self):
        self.store = ContextStore()
        self.store.submit_batch([
            register("Z", "site", "I区", geometry={"type": "box", "min": [0, 0], "max": [200, 200]}),
            register("L", "stratum", "第3层", "Z", occurred="2026-04-02T10:00:00+08:00"),
            register("M1", "feature", "M1", "L", occurred="2026-04-03T09:00:00+08:00"),
            register("D1", "find", "铜鼎01", "M1", occurred="2026-04-03T10:00:00+08:00"),
        ])

    def test_publish_freezes_evidence_version_new_evidence_does_not_rewrite(self):
        self.store.add_evidence(
            "检测机构", {"id": "ev-a", "subject_id": "M1", "attached_entity_id": "D1",
                        "type": "碳十四测年", "claim_kind": "owner",
                        "hypothesis": "H-A", "label": "甲墓主", "confidence": 0.8},
            occurred_at="2026-05-01T10:00:00+08:00")
        self.store.publish("项目负责人", "rpt-2026-01", "M1发掘简报", ["M1"],
                           summary="阶段性结论：甲墓主",
                           discloses=["M1", "D1"],
                           occurred_at="2026-06-01T10:00:00+08:00")
        frozen = self.store.get_report("rpt-2026-01")
        frozen_winner = frozen["snapshot"]["subjects"][0]["conclusions"][0]["winner"]["label"]
        frozen_version = frozen["snapshot"]["subjects"][0]["evidence_version"]
        frozen_evidence_ids = {e["evidence_id"] for e in frozen["snapshot"]["adopted_evidence"]}
        self.assertEqual(frozen_winner, "甲墓主")
        self.assertEqual(frozen_evidence_ids, {"ev-a"})

        # 发布后新证据出现并重算当前结论：新测年与新铭文共同支持丙墓主
        self.store.add_evidence(
            "检测机构", {"id": "ev-b", "subject_id": "M1", "attached_entity_id": "D1",
                        "type": "人骨测年", "claim_kind": "owner",
                        "hypothesis": "H-C", "label": "丙墓主", "confidence": 1.0},
            occurred_at="2026-06-28T10:00:00+08:00")
        self.store.add_evidence(
            "研究人员", {"id": "ev-c", "subject_id": "M1", "attached_entity_id": "D1",
                        "type": "铭文释读", "claim_kind": "owner",
                        "hypothesis": "H-C", "label": "丙墓主", "confidence": 1.0},
            occurred_at="2026-07-01T10:00:00+08:00")
        current = self.store.current_conclusions("M1")
        self.assertEqual(current["conclusions"][0]["winner"]["label"], "丙墓主")
        self.assertFalse(current["conclusions"][0]["contested"])
        self.assertNotEqual(current["evidence_version"], frozen_version)

        # 旧报告冻结内容不变
        again = self.store.get_report("rpt-2026-01")
        self.assertEqual(
            again["snapshot"]["subjects"][0]["conclusions"][0]["winner"]["label"], "甲墓主")
        self.assertEqual(
            {e["evidence_id"] for e in again["snapshot"]["adopted_evidence"]}, {"ev-a"})
        self.assertEqual(again["frozen_by"], "项目负责人")

    def test_report_id_cannot_be_republished(self):
        self.store.publish("项目负责人", "rpt-x", "第一报", ["M1"],
                           occurred_at="2026-06-01T10:00:00+08:00")
        with self.assertRaises(DomainError):
            self.store.publish("项目负责人", "rpt-x", "重复第一报", ["M1"],
                               occurred_at="2026-06-02T10:00:00+08:00")

    def test_public_views_hide_undisclosed_and_coordinates(self):
        self.store.add_evidence(
            "检测机构", {"id": "ev-a", "subject_id": "M1", "attached_entity_id": "D1",
                        "type": "碳十四测年", "claim_kind": "owner",
                        "hypothesis": "H-A", "label": "甲墓主", "confidence": 0.8},
            occurred_at="2026-05-01T10:00:00+08:00")
        # 未公开的秘密陪葬坑 M2，带精确坐标
        self.store.submit_batch([
            register("M2", "feature", "M2", "L", occurred="2026-04-04T09:00:00+08:00",
                     geometry={"type": "point", "coordinates": [158.8, 99.2]},
                     name="未公开祭祀坑"),
        ])
        self.store.publish("项目负责人", "rpt-1", "公开简报", ["M1"],
                           discloses=["M1"], occurred_at="2026-06-01T10:00:00+08:00")

        public_codes = {e["code"] for e in self.store.public_entities()}
        self.assertIn("M1", public_codes)
        self.assertNotIn("M2", public_codes)
        self.assertNotIn("D1", public_codes)  # 未列入 discloses
        for ent in self.store.public_entities():
            self.assertNotIn("geometry", ent)

        pub_report = self.store.public_reports()[0]
        self.assertEqual(pub_report["conclusions"][0]["winner_label"], "甲墓主")
        self.assertNotIn("score", pub_report["conclusions"][0])
        full = self.store.get_report("rpt-1")
        self.assertEqual(
            full["snapshot"]["subjects"][0]["conclusions"][0]["winner"]["score"], 4.5)


class PersistenceTest(unittest.TestCase):
    def test_jsonl_reload_reconstructs_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            first = ContextStore(path)
            first.submit_batch([
                register("Z", "site", "I区",
                         geometry={"type": "box", "min": [0, 0], "max": [200, 200]}),
                register("L", "stratum", "第3层", "Z",
                         occurred="2026-04-02T10:00:00+08:00"),
                register("M1", "feature", "M1", "L",
                         occurred="2026-04-03T09:00:00+08:00"),
                custody("c1", "M1", "暂存", "张三", "工地", "暂存柜",
                        "2026-04-05T09:00:00+08:00"),
            ])
            reopened = ContextStore(path)
            self.assertEqual(len(reopened.events), 4)
            self.assertEqual(reopened.entity("M1")["status"], "暂存")
            self.assertEqual(reopened.entity("M1")["current_holder"], "暂存柜")
            self.assertEqual(len(reopened.provenience("M1")["custody_chain"]), 1)


if __name__ == "__main__":
    unittest.main()
