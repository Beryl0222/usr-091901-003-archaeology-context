"""HTTP API：角色鉴权、离线 /sync 合并、复核裁定、发布与公众脱敏查询。

鉴权采用 `Authorization: Bearer <token>`；无令牌视为公众，只能访问 /public。
令牌映射见 TOKENS，部署时可由环境变量 ARCH_TOKENS（JSON）覆盖。
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from domain import DomainError
from store import json_safe

# token -> (角色组, 显示身份)。演示用默认令牌。
DEFAULT_TOKENS = {
    "field-surveyor": ("field", "测绘员"),
    "field-lead": ("field", "发掘领队"),
    "custody-store": ("custody", "库房管理员"),
    "custody-lab": ("custody", "检测机构"),
    "custody-restorer": ("custody", "修复师"),
    "research-scholar": ("research", "研究人员"),
    "director": ("director", "项目负责人"),
}

EVENT_ROLES = {
    "entity_registered": ("field",),
    "custody_transferred": ("custody",),
    "evidence_added": ("custody", "research"),
    "evidence_voided": ("research", "director"),
    "review_resolved": ("director",),
    "report_published": ("director",),
}


def make_handler(store, tokens=None, contract=None, health=None):
    tokens = tokens or DEFAULT_TOKENS
    health = health or {"status": "ok"}
    KNOWN_GET = (
        "/v1/entities", "/v1/tickets", "/v1/reports", "/v1/events",
    )

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "ArchContext/1.1"

        # ---------- 基础 ----------

        def _send_json(self, payload, status=200):
            body = json.dumps(json_safe(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, message, status, code="error"):
            self._send_json({"error": message, "code": code}, status)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                raise DomainError("请求体为空")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DomainError(f"JSON 解析失败: {exc}") from exc
            if not isinstance(data, dict):
                raise DomainError("请求体必须为 JSON 对象")
            return data

        def _principal(self):
            """返回 (角色组, 身份名)；无令牌返回 ('public', '公众')。"""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
                if token in tokens:
                    return tokens[token]
                raise DomainError("令牌无效", "bad_token")
            return ("public", "公众")

        def _require_group(self, allowed, principal):
            group, name = principal
            if group == "public":
                raise DomainError("需要授权令牌", "unauthorized")
            if allowed and group not in allowed:
                raise DomainError(f"身份 {name} 无权执行该操作（需要 { '/'.join(allowed) }）",
                                  "forbidden")
            return group, name

        def log_message(self, *_args):
            return

        # ---------- 路由 ----------

        def do_GET(self):
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            is_detail = (lambda segs, prefix:
                         len(segs) == 3 and segs[:2] == prefix)
            segs = path.strip("/").split("/")
            known = (path in KNOWN_GET
                     or is_detail(segs, ["v1", "entities"])
                     or is_detail(segs, ["v1", "provenience"])
                     or is_detail(segs, ["v1", "conclusions"])
                     or is_detail(segs, ["v1", "reports"]))
            try:
                if path == "/health":
                    return self._send_json(dict(health))
                if path == "/contract" and contract is not None:
                    return self._send_json(contract)
                if path == "/public/entities":
                    return self._send_json({"entities": store.public_entities()})
                if path == "/public/reports":
                    return self._send_json({"reports": store.public_reports()})
                if not known:
                    return self._send_error("未知路径", 404, "not_found")

                principal = self._principal()
                self._require_group(("field", "custody", "research", "director"), principal)

                if path == "/v1/entities":
                    return self._send_json({"entities": store.list_entities(
                        kind=(query.get("kind") or [None])[0],
                        status=(query.get("status") or [None])[0])})
                if is_detail(segs, ["v1", "entities"]):
                    return self._send_json(store.entity(segs[2]))
                if is_detail(segs, ["v1", "provenience"]):
                    return self._send_json(store.provenience(segs[2]))
                if is_detail(segs, ["v1", "conclusions"]):
                    return self._send_json(store.current_conclusions(segs[2]))
                if path == "/v1/tickets":
                    return self._send_json({"tickets": store.list_tickets(
                        status=(query.get("status") or [None])[0])})
                if path == "/v1/reports":
                    return self._send_json({"reports": store.list_reports()})
                if is_detail(segs, ["v1", "reports"]):
                    return self._send_json(store.get_report(segs[2]))
                if path == "/v1/events":
                    self._require_group(("director",), principal)
                    return self._send_json({"events": store.events, "count": len(store.events)})
            except DomainError as exc:
                self._domain_error(exc)

        def do_POST(self):
            parts = urlsplit(self.path)
            path = parts.path
            try:
                if path == "/v1/events/sync":
                    return self._sync()
                if path == "/v1/reviews":
                    return self._review()
                if path == "/v1/reports":
                    return self._publish()
                if path == "/v1/evidence":
                    return self._add_evidence()
                self._send_error("未知路径", 404, "not_found")
            except DomainError as exc:
                self._domain_error(exc)

        def _domain_error(self, exc):
            status = {
                "unauthorized": 401, "bad_token": 401,
                "forbidden": 403,
                "not_found": 404,
            }.get(exc.code, 400)
            self._send_error(str(exc), status, exc.code)

        # ---------- 写接口 ----------

        def _sync(self):
            principal = self._principal()
            data = self._read_json()
            events = data.get("events")
            if not isinstance(events, list) or not events:
                raise DomainError("events 必须为非空列表")
            group, name = principal
            # 逐事件鉴权；越权事件整批拒绝（不写入任何内容）
            for item in events:
                etype = item.get("type")
                allowed = EVENT_ROLES.get(etype)
                if allowed is None:
                    raise DomainError(f"未知事件类型 {etype!r}", "unknown_event")
                self._require_group(allowed, principal)
                item.setdefault("actor", name)
            result = store.submit_batch(
                events,
                batch_id=data.get("batch_id"),
                default_device=data.get("device_id"),
            )
            self._send_json(result, 207 if (result["rejected"] or result["conflicts"]) else 200)

        def _review(self):
            self._require_group(("director",), self._principal())
            data = self._read_json()
            for field in ("ticket_id", "resolution", "rationale"):
                if not data.get(field):
                    raise DomainError(f"缺少字段 {field}")
            result = store.resolve_ticket(
                actor=self._principal()[1],
                ticket_id=data["ticket_id"],
                resolution=data["resolution"],
                rationale=data["rationale"],
                chosen_entity_id=data.get("chosen_entity_id"),
                occurred_at=data.get("occurred_at"),
            )
            self._send_json(result)

        def _publish(self):
            self._require_group(("director",), self._principal())
            data = self._read_json()
            for field in ("report_id", "title", "subject_ids"):
                if not data.get(field):
                    raise DomainError(f"缺少字段 {field}")
            result = store.publish(
                actor=self._principal()[1],
                report_id=data["report_id"],
                title=data["title"],
                subject_ids=data["subject_ids"],
                summary=data.get("summary", ""),
                discloses=data.get("discloses", []),
                occurred_at=data.get("occurred_at"),
            )
            self._send_json(result)

        def _add_evidence(self):
            principal = self._principal()
            self._require_group(("custody", "research"), principal)
            data = self._read_json()
            data.setdefault("actor", principal[1])
            result = store.add_evidence(
                actor=data["actor"],
                payload=data.get("payload") if "payload" in data
                else {k: v for k, v in data.items() if k != "actor" and k != "occurred_at"},
                occurred_at=data.get("occurred_at"),
            )
            self._send_json(result)

    return ApiHandler
