"""端到端领域测试：通过 HTTP 接口验证全部不可破坏的业务原则。"""

import json
import threading
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from service import Handler
from events import EventStore
from api import Application
from domain import project


class ApiClient:
    def __init__(self, base_url):
        self.base = base_url

    def call(self, method, path, payload=None, role="pi", user="白衡",
             expect=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
            if payload is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if role is not None:
            headers["X-Role"] = role
        if user is not None:
            headers["X-User"] = quote(user)
        request = Request(self.base + path, data=data, headers=headers,
                          method=method)
        try:
            with urlopen(request, timeout=5) as response:
                body = json.load(response)
                if expect is not None:
                    raise AssertionError(f"预期 {expect} 错误，却成功: {body}")
                return response.status, body
        except HTTPError as error:
            body = json.load(error)
            if expect is not None:
                assert error.code == expect, f"预期 {expect}，实际 {error.code}: {body}"
                return error.code, body
            raise AssertionError(f"{method} {path} 返回 {error.code}: {body}")

    def post(self, path, payload, **kw):
        return self.call("POST", path, payload, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def event(self, etype, data, *, occurred_at=None, cid=None,
              role="surveyor", user="梁测", device=None, expect=None):
        item = {"type": etype, "data": data}
        if occurred_at:
            item["occurred_at"] = occurred_at
        if cid:
            item["client_event_id"] = cid
        if device:
            item["device"] = device
        return self.post("/v1/events", item, role=role, user=user, expect=expect)

    def batch(self, items, role="surveyor", user="梁测", device=None):
        return self.post("/v1/events/batch",
                         {"events": items, "device": device},
                         role=role, user=user)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.store = EventStore(Path(cls._tmp.name) / "events.jsonl")
        Handler.app = Application(cls.store)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls._tmp.cleanup()
        Handler.app = None

    def setUp(self):
        # 每个用例使用独立事件存储，避免互相干扰
        self._tmp2 = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._tmp2.name) / "events.jsonl")
        Handler.app = Application(self.store)

    def tearDown(self):
        self._tmp2.cleanup()

    # ------------------------------------------------------------------ 骨架

    def _excavate_m2(self):
        """登记 发掘区→地层→遗迹→器物 的完整空间链，返回各 ID。"""
        self.api.event("ProjectCreated", {"project_id": "p1", "name": "秦陵项目"},
                       role="pi", user="白衡", occurred_at="2019-03-01T00:00:00Z")
        self.api.event("AreaOpened",
                       {"area_id": "a1", "code": "PZ", "name": "陪葬墓区"},
                       role="director", user="秦凤", occurred_at="2019-04-01T00:00:00Z")
        self.api.event("StratumRecorded",
                       {"stratum_id": "s1", "area_id": "a1", "code": "PZ②",
                        "sequence_no": 2},
                       occurred_at="2019-04-02T00:00:00Z")
        self.api.event("FeatureRecorded",
                       {"feature_id": "f1", "area_id": "a1", "stratum_id": "s1",
                        "code": "M2", "feature_type": "墓葬",
                        "centroid": {"x": 512.5, "y": 308.2}},
                       occurred_at="2019-05-01T00:00:00Z")
        self.api.event("ArtifactRegistered",
                       {"artifact_id": "art1", "code": "M2-01",
                        "feature_id": "f1",
                        "coordinates": {"x": 512.4, "y": 308.1, "z": -5.2}},
                       cid="reg-art1", occurred_at="2019-06-01T00:00:00Z")
        return {"area": "a1", "stratum": "s1", "feature": "f1", "artifact": "art1"}

    # ------------------------------------------------------------- 1. 空间链

    def test_context_chain_unbroken(self):
        ids = self._excavate_m2()
        _, dossier = self.api.get(f"/v1/artifacts/{ids['artifact']}/dossier",
                                  role="pi")
        chain = dossier["context_chain"]
        self.assertTrue(chain["unbroken"])
        self.assertEqual(chain["area"]["code"], "PZ")
        self.assertEqual(chain["stratum"]["code"], "PZ②")
        self.assertEqual(chain["feature"]["code"], "M2")

    def test_artifact_without_feature_is_flagged_not_dropped(self):
        _, body = self.api.event(
            "ArtifactRegistered",
            {"artifact_id": "orphan", "code": "X-1", "feature_id": "missing",
             "coordinates": {"x": 1, "y": 2}},
            cid="orphan")
        kinds = {t["kind"] for t in body["active_reviews"]}
        self.assertIn("broken_context", kinds)
        # 器物记录本身仍然存在，没有被丢弃
        _, arts = self.api.get("/v1/artifacts", role="director")
        self.assertIn("orphan", {a["artifact_id"] for a in arts["artifacts"]})

    def test_feature_relink_is_forbidden(self):
        ids = self._excavate_m2()
        self.api.event("ArtifactCorrected",
                       {"artifact_id": ids["artifact"], "feature_id": "f2"},
                       role="pi", user="白衡",
                       expect=400)

    # ------------------------------------------------------ 2. 离线合并/幂等

    def test_offline_events_merge_by_occurred_time(self):
        ids = self._excavate_m2()
        path = f"/v1/artifacts/{ids['artifact']}/dossier"
        # 先补"晚"的入库，再补"早"的送检；重放必须按发生时间排序
        self.api.event("CustodyTransferred",
                       {"ref_type": "artifact", "ref_id": ids["artifact"],
                        "action": "入库", "to_party": "总库房", "handler": "赵守库"},
                       role="curator", user="王玉兰",
                       occurred_at="2020-04-16T00:00:00Z")
        self.api.event("CustodyTransferred",
                       {"ref_type": "artifact", "ref_id": ids["artifact"],
                        "action": "送检", "to_party": "修复室", "handler": "王玉兰"},
                       role="curator", user="王玉兰",
                       occurred_at="2019-06-05T00:00:00Z")
        _, dossier = self.api.get(path, role="pi")
        actions = [c["action"] for c in dossier["custody_chain"]]
        self.assertEqual(actions, ["送检", "入库"])

    def test_offline_batch_and_lag(self):
        ids = self._excavate_m2()
        _, body = self.api.batch([
            {"type": "CustodyTransferred",
             "data": {"ref_type": "artifact", "ref_id": ids["artifact"],
                      "action": "暂存", "to_party": "工地库房", "handler": "王玉兰"},
             "occurred_at": "2019-06-02T00:00:00Z",
             "client_event_id": "off-1"},
        ], role="curator", user="王玉兰", device="D-FIELD-03")
        self.assertEqual(body["accepted"], 1)
        self.assertTrue(body["merged_by_occurred_at"])
        self.assertEqual(body["results"][0]["occurred_at"], "2019-06-02T00:00:00Z")

    def test_idempotent_client_event_id(self):
        ids = self._excavate_m2()
        item = {"type": "CustodyTransferred",
                "data": {"ref_type": "artifact", "ref_id": ids["artifact"],
                         "action": "修复", "to_party": "修复室", "handler": "李修文"},
                "occurred_at": "2020-01-01T00:00:00Z",
                "client_event_id": "fix-1"}
        _, first = self.api.batch([item], role="curator", user="王玉兰")
        _, second = self.api.batch([dict(item)], role="curator", user="王玉兰")
        self.assertFalse(first["results"][0].get("duplicated"))
        self.assertTrue(second["results"][0]["duplicated"])
        self.assertEqual(first["results"][0]["seq"], second["results"][0]["seq"])
        _, dossier = self.api.get(f"/v1/artifacts/{ids['artifact']}/dossier",
                                  role="pi")
        self.assertEqual(len([c for c in dossier["custody_chain"]
                              if c["action"] == "修复"]), 1)

    def test_batch_partial_failure(self):
        ids = self._excavate_m2()
        status, body = self.api.batch([
            {"type": "CustodyTransferred",
             "data": {"ref_type": "artifact", "ref_id": ids["artifact"],
                      "action": "暂存", "handler": "王玉兰"},
             "occurred_at": "2019-06-02T00:00:00Z"},
            {"type": "NotAnEvent", "data": {}},
        ], role="curator", user="王玉兰")
        self.assertEqual(status, 207)
        self.assertTrue(body["results"][0]["ok"])
        self.assertFalse(body["results"][1]["ok"])

    # ----------------------------------------------------------- 3. 冲突复核

    def test_duplicate_code_enters_review_without_overwrite(self):
        ids = self._excavate_m2()
        _, body = self.api.event(
            "ArtifactRegistered",
            {"artifact_id": "art2", "code": "M2-01", "feature_id": "f1",
             "coordinates": {"x": 513.0, "y": 309.0, "z": -5.0}},
            cid="reg-art2", occurred_at="2019-06-03T00:00:00Z")
        dup = [t for t in body["active_reviews"] if t["kind"] == "duplicate_code"]
        self.assertEqual(len(dup), 1)
        self.assertIn("M2-01", dup[0]["description"])
        # 两条记录并存，无覆盖
        _, arts = self.api.get("/v1/artifacts", role="director")
        self.assertEqual(len(arts["artifacts"]), 2)

    def test_duplicate_identity_enters_review(self):
        ids = self._excavate_m2()
        _, body = self.api.event(
            "ArtifactRegistered",
            {"artifact_id": "art1", "code": "OTHER-9", "feature_id": "f1",
             "coordinates": {"x": 9, "y": 9}},
            cid="reg-art1-again", occurred_at="2019-06-04T00:00:00Z")
        self.assertTrue(any(t["kind"] == "duplicate_identity"
                            for t in body["active_reviews"]))
        # 原记录的编号未被覆盖
        _, arts = self.api.get("/v1/artifacts", role="director")
        art1 = next(a for a in arts["artifacts"] if a["artifact_id"] == "art1")
        self.assertEqual(art1["code"], "M2-01")

    def test_coordinate_conflict_and_resolution(self):
        ids = self._excavate_m2()
        _, body = self.api.event(
            "ArtifactRegistered",
            {"artifact_id": "art2", "code": "M2-02", "feature_id": "f1",
             "coordinates": {"x": 512.4, "y": 308.1, "z": -5.2}},
            cid="reg-art2", occurred_at="2019-06-03T00:00:00Z")
        ticket = next(t for t in body["active_reviews"]
                      if t["kind"] == "coordinate_conflict")
        # 处置必须记录责任人
        _, resolved = self.post_resolve(ticket["ticket_id"],
                                        "confirm", "现场复核确认两器并置")
        self.assertEqual(resolved["ticket"]["status"], "已处置-confirm")
        self.assertEqual(resolved["ticket"]["resolution"]["actor"], "秦凤（发掘领队）")
        _, active = self.api.get("/v1/reviews?active=1", role="director")
        self.assertNotIn(ticket["ticket_id"],
                         {t["ticket_id"] for t in active["reviews"]})
        # 已处置复核单仍可在全量列表中追溯
        _, all_tickets = self.api.get("/v1/reviews", role="director")
        self.assertIn(ticket["ticket_id"],
                      {t["ticket_id"] for t in all_tickets["reviews"]})

    def post_resolve(self, ticket_id, resolution, reason):
        return self.api.post(f"/v1/reviews/{ticket_id}/resolve",
                             {"resolution": resolution, "reason": reason},
                             role="director", user="秦凤")

    # ------------------------------------------------------------- 4. 责任链

    def test_custody_chain_keeps_every_handler(self):
        ids = self._excavate_m2()
        for action, party, handler, at, who in [
            ("暂存", "工地临时库房", "王玉兰", "2019-06-02T00:00:00Z", "王玉兰"),
            ("送检", "省文保中心", "王玉兰", "2019-06-05T00:00:00Z", "王玉兰"),
            ("接收", "省文保中心", "陈守正", "2019-06-06T00:00:00Z", "陈守正"),
            ("修复", "省文保中心", "李修文", "2020-01-08T00:00:00Z", "王玉兰"),
            ("入库", "考古院总库房", "赵守库", "2020-04-16T00:00:00Z", "王玉兰"),
        ]:
            self.api.event("CustodyTransferred",
                           {"ref_type": "artifact", "ref_id": ids["artifact"],
                            "action": action, "to_party": party,
                            "handler": handler},
                           role="curator", user=who, occurred_at=at)
        _, dossier = self.api.get(f"/v1/artifacts/{ids['artifact']}/dossier",
                                  role="pi")
        chain = dossier["custody_chain"]
        self.assertEqual([c["action"] for c in chain],
                         ["暂存", "送检", "接收", "修复", "入库"])
        self.assertEqual([c["handler"] for c in chain],
                         ["王玉兰", "王玉兰", "陈守正", "李修文", "赵守库"])

    # ------------------------------------------------- 5. 互竞观点与结论重算

    def _evidence_for_m2(self, ids):
        self.api.event("SampleTaken",
                       {"sample_id": "sam1", "code": "C14-1",
                        "artifact_id": ids["artifact"], "material": "炭样"},
                       role="director", user="秦凤",
                       occurred_at="2019-06-04T00:00:00Z")
        self.api.event("RadiocarbonResult",
                       {"result_id": "rc1", "sample_id": "sam1",
                        "lab": "AMS实验室", "lab_code": "BA-1",
                        "cal_range": [-250, -210]},
                       role="lab", user="陈守正",
                       occurred_at="2019-09-01T00:00:00Z")
        self.api.event("TypologyClassified",
                       {"classification_id": "tp1", "artifact_id": ids["artifact"],
                        "typology": "秦式铜鼎", "period_range": [-230, -206]},
                       role="researcher", user="苏释",
                       occurred_at="2019-11-01T00:00:00Z")

    def test_competing_dating_and_owner_views(self):
        ids = self._excavate_m2()
        self._evidence_for_m2(ids)
        # 两份互斥铭文释读
        self.api.event("InscriptionInterpreted",
                       {"inscription_id": "ins-gao", "artifact_id": ids["artifact"],
                        "reading": "高", "implies_owner": "公子高",
                        "period_range": [-221, -207]},
                       role="researcher", user="苏释",
                       occurred_at="2019-12-01T00:00:00Z")
        self.api.event("ClaimProposed",
                       {"claim_id": "c-gao", "claim_kind": "owner",
                        "target_type": "feature", "target_id": "f1",
                        "title": "公子高说", "proposed_owner": "公子高",
                        "evidence_ids": ["ins-gao"]},
                       role="researcher", user="苏释",
                       occurred_at="2020-02-01T00:00:00Z")
        _, conclusion = self.api.get("/v1/features/f1/conclusion", role="pi")
        owners = {c["proposed_owner"] for c in conclusion["owner"]["candidates"]}
        self.assertIn("公子高", owners)
        # 三类证据区间有交集
        self.assertEqual(conclusion["dating"]["consensus_range"], [-221, -210])
        self.assertEqual(len(conclusion["dating"]["ranges"]), 3)

        # 采用公子高说
        self.api.event("ViewpointAdopted",
                       {"claim_id": "c-gao", "reason": "铭文直接"},
                       role="pi", user="白衡",
                       occurred_at="2021-02-20T00:00:00Z")
        _, conclusion = self.api.get("/v1/features/f1/conclusion", role="pi")
        self.assertEqual(conclusion["owner"]["adopted_owner"], "公子高")

        # 新证据：晚段测年 + 新释读，区间失去交集；两说并存
        self.api.event("SampleTaken",
                       {"sample_id": "sam2", "code": "C14-2",
                        "feature_id": "f1", "material": "人骨"},
                       role="director", user="秦凤",
                       occurred_at="2023-05-01T00:00:00Z")
        self.api.event("RadiocarbonResult",
                       {"result_id": "rc2", "sample_id": "sam2",
                        "lab": "AMS实验室", "lab_code": "BA-2",
                        "cal_range": [-205, -195]},
                       role="lab", user="陈守正",
                       occurred_at="2023-08-01T00:00:00Z")
        self.api.event("InscriptionInterpreted",
                       {"inscription_id": "ins-jia", "artifact_id": ids["artifact"],
                        "reading": "嘉", "implies_owner": "宗室贵族嘉"},
                       role="researcher", user="苏释",
                       occurred_at="2023-09-01T00:00:00Z")
        self.api.event("ClaimProposed",
                       {"claim_id": "c-jia", "claim_kind": "owner",
                        "target_type": "feature", "target_id": "f1",
                        "title": "宗室贵族嘉说", "proposed_owner": "宗室贵族嘉",
                        "evidence_ids": ["ins-jia", "rc2"]},
                       role="researcher", user="苏释",
                       occurred_at="2023-10-01T00:00:00Z")
        _, conclusion = self.api.get("/v1/features/f1/conclusion", role="pi")
        self.assertIsNone(conclusion["dating"]["consensus_range"])
        self.assertIn("竞争", conclusion["dating"]["consensus_label"])
        owners = {c["proposed_owner"] for c in conclusion["owner"]["candidates"]}
        self.assertEqual(owners, {"公子高", "宗室贵族嘉"})
        old_version = conclusion["evidence_version"]

        # 改用新观点：当前结论重算，且证据版本发生变化
        self.api.event("ViewpointAdopted",
                       {"claim_id": "c-jia", "reason": "测年晚于公子高卒年"},
                       role="pi", user="白衡",
                       occurred_at="2024-01-01T00:00:00Z")
        _, conclusion = self.api.get("/v1/features/f1/conclusion", role="pi")
        self.assertEqual(conclusion["owner"]["adopted_owner"], "宗室贵族嘉")
        self.assertNotEqual(conclusion["evidence_version"], old_version)

    # ------------------------------------------------- 6. 发布冻结与再发布

    def test_publish_freezes_evidence_version(self):
        ids = self._excavate_m2()
        self._evidence_for_m2(ids)
        self.api.event("InscriptionInterpreted",
                       {"inscription_id": "ins-gao", "artifact_id": ids["artifact"],
                        "reading": "高", "implies_owner": "公子高",
                        "period_range": [-221, -207]},
                       role="researcher", user="苏释",
                       occurred_at="2019-12-01T00:00:00Z")
        self.api.event("ClaimProposed",
                       {"claim_id": "c-gao", "claim_kind": "owner",
                        "target_type": "feature", "target_id": "f1",
                        "title": "公子高说", "proposed_owner": "公子高",
                        "evidence_ids": ["ins-gao"]},
                       role="researcher", user="苏释",
                       occurred_at="2020-02-01T00:00:00Z")
        self.api.event("ViewpointAdopted", {"claim_id": "c-gao"},
                       role="pi", user="白衡",
                       occurred_at="2021-02-20T00:00:00Z")

        # 2021 发布
        _, pub = self.api.event(
            "ReportPublished",
            {"report_id": "R-2021", "title": "2021简报", "targets": [["feature", "f1"]]},
            role="pi", user="白衡", occurred_at="2021-03-01T00:00:00Z")
        version_2021 = pub["seq"]
        _, report_before = self.api.get("/v1/reports/R-2021", role="pi")
        snapshot_before = json.loads(json.dumps(report_before["snapshots"]))
        frozen_owner = snapshot_before[0]["owner"]["adopted_owner"]
        frozen_version = snapshot_before[0]["evidence_version"]

        # 新证据出现并重采观点
        self.api.event("SampleTaken",
                       {"sample_id": "sam2", "code": "C14-2",
                        "feature_id": "f1", "material": "人骨"},
                       role="director", user="秦凤",
                       occurred_at="2023-05-01T00:00:00Z")
        self.api.event("RadiocarbonResult",
                       {"result_id": "rc2", "sample_id": "sam2",
                        "lab": "AMS实验室", "cal_range": [-205, -195]},
                       role="lab", user="陈守正",
                       occurred_at="2023-08-01T00:00:00Z")
        self.api.event("InscriptionInterpreted",
                       {"inscription_id": "ins-jia", "artifact_id": ids["artifact"],
                        "reading": "嘉", "implies_owner": "宗室贵族嘉"},
                       role="researcher", user="苏释",
                       occurred_at="2023-09-01T00:00:00Z")
        self.api.event("ClaimProposed",
                       {"claim_id": "c-jia", "claim_kind": "owner",
                        "target_type": "feature", "target_id": "f1",
                        "title": "嘉说", "proposed_owner": "宗室贵族嘉"},
                       role="researcher", user="苏释",
                       occurred_at="2023-10-01T00:00:00Z")
        self.api.event("ViewpointAdopted", {"claim_id": "c-jia"},
                       role="pi", user="白衡",
                       occurred_at="2024-01-01T00:00:00Z")
        _, pub2 = self.api.event(
            "ReportPublished",
            {"report_id": "R-2024", "title": "2024报告", "targets": [["feature", "f1"]]},
            role="pi", user="白衡", occurred_at="2024-03-01T00:00:00Z")

        # 旧报告快照逐字节不变
        _, report_after = self.api.get("/v1/reports/R-2021", role="pi")
        self.assertEqual(report_after["snapshots"], snapshot_before)
        self.assertEqual(report_after["snapshots"][0]["owner"]["adopted_owner"],
                         frozen_owner)
        self.assertEqual(report_after["snapshots"][0]["evidence_version"],
                         frozen_version)
        # 新报告冻结新版本、新结论
        _, report_new = self.api.get("/v1/reports/R-2024", role="pi")
        self.assertEqual(report_new["snapshots"][0]["owner"]["adopted_owner"],
                         "宗室贵族嘉")
        self.assertNotEqual(report_new["snapshots"][0]["evidence_version"],
                            frozen_version)
        # 即使是离线补录一条更晚才同步的 2018 年事件，旧快照也不变
        self.api.event("StratumRecorded",
                       {"stratum_id": "s-old", "area_id": "a1", "code": "PZ①",
                        "sequence_no": 1},
                       cid="late-sync", occurred_at="2018-01-01T00:00:00Z")
        _, report_late = self.api.get("/v1/reports/R-2021", role="pi")
        self.assertEqual(report_late["snapshots"], snapshot_before)

    # ----------------------------------------------------------- 7. 公开层

    def test_public_hides_undisclosed_and_coordinates(self):
        ids = self._excavate_m2()
        # 未披露：公众查询 404，且与"不存在"不可区分
        self.api.get(f"/public/artifacts/{ids['artifact']}",
                     role="public", user=None, expect=404)
        _, listed = self.api.get("/public/artifacts", role="public", user=None)
        self.assertEqual(listed["artifacts"], [])

        # 发布但未披露 → 仅以发布信息可见？规则：发布即公开，但坐标默认隐藏
        self.api.event("ReportPublished",
                       {"report_id": "R-1", "title": "M2简报",
                        "targets": [["artifact", "art1"]]},
                       role="pi", user="白衡",
                       occurred_at="2021-03-01T00:00:00Z")
        _, view = self.api.get(f"/public/artifacts/{ids['artifact']}",
                               role="public", user=None)
        self.assertIsNone(view["coordinates"])
        self.assertIn("坐标", view["coordinates_note"])

        # 披露并明确不公开坐标
        self.api.event("DiscoveryDisclosed",
                       {"target_type": "feature", "target_id": "f1",
                        "public_name": "二号陪葬墓",
                        "public_summary": "秦代高等级墓葬",
                        "reveal_coordinates": False},
                       role="pi", user="白衡",
                       occurred_at="2021-03-10T00:00:00Z")
        _, view = self.api.get(f"/public/artifacts/{ids['artifact']}",
                               role="public", user=None)
        self.assertEqual(view["designation"], "二号陪葬墓")
        self.assertIsNone(view["coordinates"])

        # 改为可公开坐标后才出现
        # （披露是状态事实，允许再发布一条更晚的披露决定）
        self.api.event("DiscoveryDisclosed",
                       {"target_type": "feature", "target_id": "f1",
                        "public_name": "二号陪葬墓",
                        "public_summary": "秦代高等级墓葬",
                        "reveal_coordinates": True},
                       role="pi", user="白衡",
                       occurred_at="2025-01-01T00:00:00Z")
        _, view = self.api.get(f"/public/artifacts/{ids['artifact']}",
                               role="public", user=None)
        self.assertIsNotNone(view["coordinates"])

    def test_public_never_reveals_undisclosed_feature(self):
        ids = self._excavate_m2()
        # 祭祀坑：有器物、有测年，但从不披露、从不发布
        self.api.event("StratumRecorded",
                       {"stratum_id": "s2", "area_id": "a1", "code": "JS①",
                        "sequence_no": 1},
                       occurred_at="2020-09-01T00:00:00Z")
        self.api.event("FeatureRecorded",
                       {"feature_id": "f2", "area_id": "a1", "stratum_id": "s2",
                        "code": "H5", "feature_type": "祭祀坑"},
                       occurred_at="2020-09-02T00:00:00Z")
        self.api.event("ArtifactRegistered",
                       {"artifact_id": "art-h5", "code": "H5-01",
                        "feature_id": "f2",
                        "coordinates": {"x": 640, "y": 210, "z": -2.8}},
                       occurred_at="2020-09-03T00:00:00Z")
        self.api.get("/public/artifacts/art-h5", role="public", user=None,
                     expect=404)
        _, listed = self.api.get("/public/artifacts", role="public", user=None)
        self.assertNotIn("H5-01", {a["code"] for a in listed["artifacts"]})

    # ----------------------------------------------------------- 8. 权限

    def test_permissions(self):
        # 公众不能写
        self.api.post("/v1/events",
                      {"type": "AreaOpened", "data": {"area_id": "x", "code": "X"}},
                      role="public", user=None, expect=403)
        # 写操作必须标明责任人
        self.api.post("/v1/events",
                      {"type": "AreaOpened", "data": {"area_id": "x", "code": "X"}},
                      role="surveyor", user=None, expect=400)
        # 测绘员无权提出研究观点
        self.api.post("/v1/events",
                      {"type": "ClaimProposed",
                       "data": {"claim_id": "c", "claim_kind": "owner",
                                "target_type": "feature", "target_id": "f",
                                "title": "t"}},
                      role="surveyor", user="梁测", expect=403)
        # 公众不能访问内部接口
        self.api.get("/v1/artifacts", role="public", user=None, expect=403)

    # ----------------------------------------------------------- 9. 哈希链

    def test_hash_chain_verify_and_tamper_detection(self):
        self._excavate_m2()
        _, body = self.api.get("/v1/verify", role="pi")
        self.assertTrue(body["ok"])
        # 落盘文件中器物编号被改动一个字节 → 重载时哈希校验失败
        path = self.store._path
        raw = path.read_bytes()
        tampered = bytearray(raw)
        pos = tampered.rfind(b"M2-01")
        self.assertGreater(pos, 0)
        tampered[pos] = ord("X")
        path.write_bytes(bytes(tampered))
        with self.assertRaises(ValueError):
            EventStore(path)

    def test_persistence_across_restart(self):
        ids = self._excavate_m2()
        path = self.store._path
        reopened = EventStore(path)
        proj = project(reopened)
        self.assertIn(ids["artifact"], proj.artifacts)
        self.assertEqual(len(reopened), len(self.store))
        self.assertEqual(reopened.verify_chain(), len(self.store))


if __name__ == "__main__":
    unittest.main()
