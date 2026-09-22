"""HTTP API：命令追加、冲突复核、全宗档案、公开查询与发布冻结。

鉴权约定（演示用明文请求头，便于离线设备与测试直接对接）：

* ``X-Role``  角色：public/surveyor/director/curator/lab/researcher/pi；
* ``X-User``  操作人真实姓名，非 ASCII 字符按 UTF-8 百分号编码
              （如 ``%E7%99%BD%E8%A1%A1`` 表示"白衡"）。
  所有写操作必填，写入事件并随责任链永久保存。
"""

import json
from urllib.parse import urlparse, parse_qs, unquote

from events import EventStore, now_ts
from domain import (ApiError, Projection, PERMISSIONS, ROLES,
                    validate_command, project as build_projection,
                    norm_ts)

INTERNAL_ROLES = {"surveyor", "director", "curator", "lab", "researcher", "pi"}
MAX_BODY = 16 * 1024 * 1024


class Application:
    """持有事件存储并处理路由；Handler 只负责 HTTP 编解码。"""

    def __init__(self, store: EventStore):
        self.store = store

    # ------------------------------------------------------------------ 工具

    def projection(self):
        return build_projection(self.store)

    # ------------------------------------------------------------------ 路由

    def handle(self, method, path, query, headers, body):
        """返回 (status, payload)。"""
        if method == "GET":
            if path == "/health":
                return 200, {"status": "ok", "service": "archaeology-context",
                             "name": "考古现场上下文档案", "events": len(self.store)}
            if path == "/contract":
                return 200, _load_contract()
        if not path.startswith(("/v1/", "/public/")):
            raise ApiError(404, "接口不存在")

        role = (headers.get("x-role") or "public").strip().lower()
        if role not in ROLES:
            raise ApiError(403, f"未知角色: {role}")
        user_raw = (headers.get("x-user") or "").strip()
        user = unquote(user_raw) if user_raw else ""

        if path.startswith("/public/"):
            if method == "GET":
                return self._public_get(path, query)
            raise ApiError(405, "公开接口仅支持 GET")

        if role == "public":
            raise ApiError(403, "内部接口需要项目角色，请设置 X-Role")

        if method == "GET":
            return self._internal_get(path, query, role)
        if method == "POST":
            return self._internal_post(path, body, role, user)
        raise ApiError(405, "仅支持 GET/POST")

    # ------------------------------------------------------------------ 写入

    def _internal_post(self, path, body, role, user):
        payload = _parse_json(body)
        if not user:
            raise ApiError(400, "写操作必须通过 X-User 标明责任人")
        actor = f"{user}（{ROLES[role]}）"

        if path == "/v1/events":
            result = self._append_one(payload, role, actor)
            return 201, result
        if path == "/v1/events/batch":
            return self._append_batch(payload, role, actor)
        if path.startswith("/v1/reviews/") and path.endswith("/resolve"):
            ticket_id = path[len("/v1/reviews/"):-len("/resolve")]
            return self._resolve_review(ticket_id, payload, role, actor)
        raise ApiError(404, "接口不存在")

    def _append_one(self, item, role, actor):
        if not isinstance(item, dict) or "type" not in item:
            raise ApiError(400, "事件需包含 type")
        kind = item["type"]
        data = item.get("data") or {}
        validate_command(kind, data, role)
        data, kind = self._prepare_command(kind, data)
        event, duplicated = self.store.append(
            kind, data, actor=actor,
            occurred_at=norm_ts(item["occurred_at"]) if item.get("occurred_at")
            else None,
            client_event_id=item.get("client_event_id"),
            device=item.get("device"), note=item.get("note"))
        proj = self.projection()
        return {
            "ok": True, "duplicated": duplicated,
            "seq": event["seq"], "hash": event["hash"],
            "occurred_at": event["payload"]["occurred_at"],
            "recorded_at": event["payload"]["recorded_at"],
            "offline_lag": _lag_seconds(event),
            "active_reviews": proj.tickets_list(active_only=True),
        }

    def _append_batch(self, payload, role, actor):
        """离线设备补录：逐条按原发生时间落入日志，重复 client_event_id 幂等。"""
        items = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            raise ApiError(400, "需要 events 数组")
        device_default = payload.get("device") if isinstance(payload, dict) else None
        results = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or "type" not in item:
                results.append({"index": index, "ok": False,
                                "error": "事件需包含 type"})
                continue
            kind = item["type"]
            data = item.get("data") or {}
            try:
                validate_command(kind, data, role)
                data, kind = self._prepare_command(kind, data)
                event, duplicated = self.store.append(
                    kind, data, actor=actor,
                    occurred_at=norm_ts(item["occurred_at"])
                    if item.get("occurred_at") else None,
                    client_event_id=item.get("client_event_id"),
                    device=item.get("device") or device_default,
                    note=item.get("note"))
                results.append({"index": index, "ok": True,
                                "duplicated": duplicated, "seq": event["seq"],
                                "hash": event["hash"],
                                "occurred_at": event["payload"]["occurred_at"]})
            except ApiError as exc:
                results.append({"index": index, "ok": False,
                                "error": exc.message})
        proj = self.projection()
        accepted = [r for r in results if r["ok"]]
        return 207 if accepted and len(accepted) != len(items) else (
            201 if accepted else 400), {
            "received": len(items), "accepted": len(accepted),
            "merged_by_occurred_at": True,
            "results": results,
            "active_reviews": proj.tickets_list(active_only=True),
        }

    def _prepare_command(self, kind, data):
        """发布命令在追加前固化结论快照（哈希链保护，此后不可改写）。"""
        if kind == "ReportPublished":
            proj = self.projection()
            data = dict(data)
            data["snapshots"] = [
                proj._frozen_snapshot(t[0], t[1]) for t in data["targets"]]
        return data, kind

    def _resolve_review(self, ticket_id, payload, role, actor):
        validate_command("ReviewResolved",
                         {"ticket_id": ticket_id,
                          "resolution": payload.get("resolution")}, role)
        reason = payload.get("reason", "")
        proj_before = self.projection()
        if ticket_id not in proj_before.tickets:
            raise ApiError(404, f"复核单不存在: {ticket_id}")
        event, _ = self.store.append(
            "ReviewResolved",
            {"ticket_id": ticket_id,
             "resolution": payload["resolution"], "reason": reason},
            actor=actor)
        ticket = next(t for t in self.projection().tickets_list()
                      if t["ticket_id"] == ticket_id)
        return 201, {"ok": True, "seq": event["seq"], "ticket": ticket}

    # ------------------------------------------------------------------ 内部读

    def _internal_get(self, path, query, role):
        proj = self.projection()
        if path == "/v1/areas":
            return 200, {"areas": list(proj.areas.values())}
        if path == "/v1/strata":
            return 200, {"strata": list(proj.strata.values())}
        if path == "/v1/features":
            return 200, {"features": list(proj.features.values())}
        if path == "/v1/artifacts":
            return 200, {"artifacts": list(proj.artifacts.values())}
        if path == "/v1/samples":
            return 200, {"samples": list(proj.samples.values())}
        if path == "/v1/reviews":
            active = query.get("active", [""])[0] in ("1", "true", "yes")
            return 200, {"reviews": proj.tickets_list(active_only=active)}
        if path == "/v1/reports":
            return 200, {"reports": list(proj.reports.values())}
        if path.startswith("/v1/reports/"):
            rid = path[len("/v1/reports/"):]
            report = proj.reports.get(rid)
            if not report:
                raise ApiError(404, "报告不存在")
            return 200, report

        prefix = "/v1/artifacts/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            aid, _, sub = rest.partition("/")
            if sub == "dossier":
                dossier = proj.dossier(aid)
                if not dossier:
                    raise ApiError(404, "出土物不存在")
                return 200, dossier
            if sub == "conclusion":
                if aid not in proj.artifacts:
                    raise ApiError(404, "出土物不存在")
                return 200, proj.conclusion("artifact", aid)

        prefix = "/v1/features/"
        if path.startswith(prefix):
            fid, _, sub = path[len(prefix):].partition("/")
            if fid not in proj.features:
                raise ApiError(404, "遗迹不存在")
            if sub == "conclusion":
                return 200, proj.conclusion("feature", fid)

        if path == "/v1/events":
            return 200, {"events": proj_events_public(self.store, query)}
        if path == "/v1/verify":
            count = self.store.verify_chain()
            return 200, {"ok": True, "events": count,
                         "message": "哈希链完整，事件流未被篡改"}
        raise ApiError(404, "接口不存在")

    # ------------------------------------------------------------------ 公开读

    def _public_get(self, path, query):
        proj = self.projection()
        if path == "/public/artifacts":
            code = query.get("code", [None])[0]
            items = proj.public_artifacts()
            if code:
                items = [a for a in items if a["code"] == code]
            return 200, {"artifacts": items}
        if path.startswith("/public/artifacts/"):
            aid = path[len("/public/artifacts/"):]
            view = proj.public_artifact(aid)
            if not view:
                # 未公开对象与不存在对象返回完全相同的 404，不泄露其存在
                raise ApiError(404, "未找到已公开的出土物")
            return 200, view
        if path == "/public/reports":
            return 200, {"reports": proj.public_reports()}
        if path.startswith("/public/reports/"):
            rid = path[len("/public/reports/"):]
            report = proj.reports.get(rid)
            if not report:
                raise ApiError(404, "报告不存在")
            return 200, {"report_id": report["report_id"],
                         "title": report["title"],
                         "published_at": report["published"]["occurred_at"],
                         "summary": report["note"],
                         "targets": [
                             {"code": s.get("code"),
                              "feature_type": (s.get("feature") or {})
                              .get("feature_type"),
                              "dating": s["dating"],
                              "owner": {"adopted_owner": s["owner"]["adopted_owner"]}}
                             for s in report["snapshots"]]}
        raise ApiError(404, "接口不存在")


# ----------------------------------------------------------------------------
# 辅助

def _load_contract():
    from pathlib import Path
    return json.loads(Path(__file__).with_name("domain_contract.json")
                      .read_text(encoding="utf-8"))


def _parse_json(body):
    if not body:
        raise ApiError(400, "缺少请求体")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "请求体不是合法 JSON")


def _lag_seconds(event):
    from datetime import datetime, timezone
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    occurred = datetime.strptime(event["payload"]["occurred_at"], fmt).replace(
        tzinfo=timezone.utc)
    recorded = datetime.strptime(event["payload"]["recorded_at"], fmt).replace(
        tzinfo=timezone.utc)
    return int((recorded - occurred).total_seconds())


def proj_events_public(store, query):
    """审计导出：支持 ?from=&to= 按序号截取。"""
    events = store.events()
    frm = int(query.get("from", ["1"])[0])
    to = int(query.get("to", [str(len(events))])[0])
    return [{"seq": e["seq"], "hash": e["hash"], "prev_hash": e["prev_hash"],
             "client_event_id": e.get("client_event_id"),
             "payload": e["payload"]}
            for e in events[frm - 1:to]]
