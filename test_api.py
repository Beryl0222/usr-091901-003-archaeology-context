"""HTTP API 测试：角色鉴权、离线合并响应、复核发布接口与公众脱敏。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from api import make_handler
from store import ContextStore

BASE_EVENTS = [
    {"event_id": "e-z", "type": "entity_registered", "actor": "测绘员甲",
     "occurred_at": "2026-04-02T08:00:00+08:00",
     "payload": {"id": "Z1", "kind": "site", "code": "I区", "region": "上召窑",
                 "geometry": {"type": "box", "min": [0, 0], "max": [200, 200]}}},
    {"event_id": "e-l", "type": "entity_registered", "actor": "测绘员甲",
     "occurred_at": "2026-04-02T10:00:00+08:00",
     "payload": {"id": "L3", "kind": "stratum", "code": "第3层", "parent_id": "Z1"}},
    {"event_id": "e-m", "type": "entity_registered", "actor": "田野队员乙",
     "occurred_at": "2026-04-03T09:00:00+08:00",
     "payload": {"id": "M1", "kind": "feature", "code": "M1", "parent_id": "L3",
                 "name": "陪葬墓M1",
                 "geometry": {"type": "box", "min": [40, 40], "max": [46, 48]}}},
    {"event_id": "e-d", "type": "entity_registered", "actor": "田野队员乙",
     "occurred_at": "2026-04-03T10:00:00+08:00",
     "payload": {"id": "D1", "kind": "find", "code": "铜鼎01", "parent_id": "M1",
                 "geometry": {"type": "point", "coordinates": [43.2, 44.1]}}},
]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = ContextStore()
        handler = make_handler(cls.store)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, token=None, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            return exc.code, payload

    # ---------- 鉴权 ----------

    def test_internal_routes_require_token(self):
        status, payload = self.call("GET", "/v1/entities")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unauthorized")

    def test_bad_token_rejected(self):
        status, payload = self.call("GET", "/v1/entities", token="nope")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "bad_token")

    def test_role_boundary_field_cannot_transfer_custody(self):
        status, payload = self.call("POST", "/v1/events/sync", token="field-surveyor", body={
            "events": [{"type": "custody_transferred",
                        "occurred_at": "2026-04-05T09:00:00+08:00",
                        "payload": {"object_id": "D1", "action": "入库",
                                    "from_party": "工地", "to_party": "库房",
                                    "responsible": "张三"}}]})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "forbidden")

    def test_only_director_can_review_and_publish(self):
        status, _ = self.call("POST", "/v1/reviews", token="research-scholar",
                              body={"ticket_id": "x", "resolution": "维持原记录", "rationale": "r"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/v1/reports", token="research-scholar",
                              body={"report_id": "r", "title": "t", "subject_ids": ["M1"]})
        self.assertEqual(status, 403)

    # ---------- 主业务链路 ----------

    def test_full_lifecycle_over_http(self):
        # 1) 田野端登记
        status, synced = self.call("POST", "/v1/events/sync", token="field-lead",
                                   body={"device_id": "tab-2", "events": BASE_EVENTS})
        self.assertEqual(status, 200)
        self.assertEqual(len(synced["accepted"]), 4)
        self.assertEqual(synced["duplicates"], [])

        # 2) 离线设备重发：幂等
        status, again = self.call("POST", "/v1/events/sync", token="field-lead",
                                  body={"device_id": "tab-2", "events": BASE_EVENTS})
        self.assertEqual(status, 200)
        self.assertEqual(len(again["accepted"]), 0)
        self.assertEqual(len(again["duplicates"]), 4)

        # 3) 重复编号 -> 207 + 复核单，不覆盖
        dup_event = {"event_id": "e-dup", "type": "entity_registered",
                     "occurred_at": "2026-04-09T09:00:00+08:00",
                     "payload": {"id": "M1b", "kind": "feature", "code": "M1",
                                 "parent_id": "L3",
                                 "geometry": {"type": "box", "min": [90, 90], "max": [96, 98]}}}
        status, dup_res = self.call("POST", "/v1/events/sync", token="field-surveyor",
                                    body={"events": [dup_event]})
        self.assertEqual(status, 207)
        self.assertEqual(dup_res["conflicts"][0]["kind"], "duplicate_code")

        status, tickets = self.call("GET", "/v1/tickets?status=open", token="director")
        self.assertEqual(status, 200)
        ticket = next(t for t in tickets["tickets"] if t["kind"] == "duplicate_code")
        self.assertEqual(len(ticket["candidates"]), 2)

        # 4) 冲突期间交接被拒
        status, blocked = self.call("POST", "/v1/events/sync", token="custody-store", body={
            "events": [{"type": "custody_transferred",
                        "occurred_at": "2026-04-10T09:00:00+08:00",
                        "payload": {"object_id": "M1b", "action": "暂存",
                                    "from_party": "工地", "to_party": "库房",
                                    "responsible": "张三"}}]})
        self.assertEqual(status, 207)
        self.assertEqual(blocked["rejected"][0]["code"], "object_in_review")

        # 5) 负责人裁定维持原记录
        status, review = self.call("POST", "/v1/reviews", token="director", body={
            "ticket_id": ticket["id"], "resolution": "维持原记录",
            "rationale": "新记录编号笔误，实为M2",
            "occurred_at": "2026-04-11T09:00:00+08:00"})
        self.assertEqual(status, 200)
        status, ent = self.call("GET", "/v1/entities/M1b", token="director")
        self.assertEqual(ent["status"], "驳回")

        # 6) 保管链：送检->入库，全部带责任人
        status, transfer = self.call("POST", "/v1/events/sync", token="custody-lab", body={
            "events": [
                {"event_id": "c1", "type": "custody_transferred",
                 "occurred_at": "2026-04-12T09:00:00+08:00",
                 "payload": {"object_id": "D1", "action": "送检",
                             "from_party": "田野队", "to_party": "碳十四实验室",
                             "responsible": "张三"}},
                {"event_id": "c2", "type": "custody_transferred",
                 "occurred_at": "2026-05-02T09:00:00+08:00",
                 "payload": {"object_id": "D1", "action": "入库",
                             "from_party": "修复室", "to_party": "遗址博物馆库房",
                             "responsible": "赵六"}}]})
        self.assertEqual(status, 200)
        self.assertEqual(len(transfer["accepted"]), 2)

        # 7) 竞争证据：铭文支持乙（强释读），碳十四支持甲；势均力敌 => contested
        for token, ev in [
            ("custody-lab", {"id": "ev-c14", "type": "碳十四测年", "claim_kind": "owner",
                             "hypothesis": "H-A", "label": "甲墓主", "confidence": 0.95,
                             "subject_id": "M1", "attached_entity_id": "D1"}),
            ("research-scholar", {"id": "ev-ins", "type": "铭文释读", "claim_kind": "owner",
                                  "hypothesis": "H-B", "label": "乙墓主", "confidence": 0.95,
                                  "subject_id": "M1", "attached_entity_id": "D1"}),
        ]:
            status, _ = self.call("POST", "/v1/evidence", token=token,
                                  body={"occurred_at": "2026-05-10T10:00:00+08:00",
                                        "payload": ev})
            self.assertEqual(status, 200)
        status, conclusions = self.call("GET", "/v1/conclusions/M1", token="research-scholar")
        self.assertEqual(status, 200)
        owner = next(c for c in conclusions["conclusions"] if c["claim_kind"] == "owner")
        self.assertEqual(owner["winner"]["label"], "甲墓主")
        self.assertTrue(owner["contested"])

        # 8) 发布冻结（披露 M1、D1）
        status, _published = self.call("POST", "/v1/reports", token="director", body={
            "report_id": "rpt-2026-01", "title": "M1发掘简报",
            "subject_ids": ["M1"], "discloses": ["M1", "D1"],
            "summary": "甲墓主（存争议）",
            "occurred_at": "2026-06-01T10:00:00+08:00"})
        self.assertEqual(status, 200)
        status, report = self.call("GET", "/v1/reports/rpt-2026-01", token="director")
        frozen_version = report["snapshot"]["subjects"][0]["evidence_version"]

        # 9) 新测年与新释读共同支持丙墓主，当前结论重算翻转
        for token, ev in [
            ("custody-lab", {"id": "ev-bone", "type": "人骨测年", "claim_kind": "owner",
                             "hypothesis": "H-C", "label": "丙墓主", "confidence": 1.0,
                             "subject_id": "M1", "attached_entity_id": "D1"}),
            ("research-scholar", {"id": "ev-new", "type": "铭文释读", "claim_kind": "owner",
                                  "hypothesis": "H-C", "label": "丙墓主", "confidence": 1.0,
                                  "subject_id": "M1", "attached_entity_id": "D1"}),
        ]:
            status, _ = self.call("POST", "/v1/evidence", token=token,
                                  body={"occurred_at": "2026-07-01T10:00:00+08:00",
                                        "payload": ev})
            self.assertEqual(status, 200)
        status, nowc = self.call("GET", "/v1/conclusions/M1", token="director")
        self.assertEqual(
            next(c for c in nowc["conclusions"] if c["claim_kind"] == "owner")
            ["winner"]["label"], "丙墓主")

        # 10) 旧报告不被改写
        status, frozen = self.call("GET", "/v1/reports/rpt-2026-01", token="director")
        self.assertEqual(
            frozen["snapshot"]["subjects"][0]["conclusions"][0]["winner"]["label"], "甲墓主")
        self.assertEqual(frozen["snapshot"]["subjects"][0]["evidence_version"], frozen_version)

        # 11) 器物全链路还原
        status, prov = self.call("GET", "/v1/provenience/D1", token="director")
        self.assertEqual(status, 200)
        self.assertEqual([c["code"] for c in prov["spatial_context"]],
                         ["铜鼎01", "M1", "第3层", "I区"])
        self.assertEqual([c["responsible"] for c in prov["custody_chain"]], ["张三", "赵六"])
        self.assertEqual({e["id"] for e in prov["evidence"]},
                         {"ev-c14", "ev-ins", "ev-bone", "ev-new"})
        self.assertEqual({r["report_id"] for r in prov["reports"]}, {"rpt-2026-01"})
        self.assertEqual(prov["registration"]["by"], "田野队员乙")

    # ---------- 公众脱敏 ----------

    def test_public_endpoints_redact(self):
        status, entities = self.call("GET", "/public/entities")
        self.assertEqual(status, 200)
        codes = {e["code"] for e in entities["entities"]}
        self.assertIn("M1", codes)      # 报告披露
        self.assertIn("铜鼎01", codes)
        self.assertNotIn("M1b", codes)  # 被驳回/未披露
        for ent in entities["entities"]:
            self.assertNotIn("geometry", ent)
            self.assertNotIn("parent_id", ent)

        status, reports = self.call("GET", "/public/reports")
        self.assertEqual(status, 200)
        report = next(r for r in reports["reports"] if r["report_id"] == "rpt-2026-01")
        self.assertEqual(report["conclusions"][0]["winner_label"], "甲墓主")
        self.assertNotIn("adopted_evidence", report)
        for key in report["conclusions"][0]:
            self.assertNotIn(key, ("score", "share", "evidence_ids", "date_range_bp"))

    def test_unknown_route_404_without_auth(self):
        status, payload = self.call("GET", "/nonsense")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
