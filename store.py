"""事件溯源存储：只增事件流、离线按发生时间合并、冲突复核与结论重算。

所有可持久状态都由事件日志重放得到；事件只增不改，更正一律追加新事件
（对同一编号提交修正登记 -> 生成复核单 -> 复核采用新记录）。
日志按接收顺序追加（审计事实），投影按 (occurred_at, 接收序号) 重放（业务时序），
因此野外设备晚补录的事件仍能落回其真实发生时刻。
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from domain import (
    CUSTODY_ACTIONS,
    DomainError,
    KIND_CN,
    RESOLUTIONS,
    contained_by,
    geometry_overlap,
    iso,
    normalize_entity,
    normalize_evidence,
    parent_allowed,
    parse_ts,
    payload_hash,
    public_report,
    public_entity,
    recompute_conclusions,
)

TICKET_PREFIX = {
    "duplicate_code": "rf-dup",
    "position_overlap": "rf-pos",
    "context_mismatch": "rf-ctx",
}

CUSTODY_STATE = {
    "送检": "送检中", "暂存": "暂存", "修复": "修复中",
    "入库": "已入库", "提取研究": "研究中",
}


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ContextStore:
    def __init__(self, path=None):
        self._lock = threading.RLock()
        self.path = Path(path) if path else None
        self.events = []          # 日志顺序（接收顺序）
        self._seq = 0
        self._known_event_ids = set()
        self._known_device_seqs = set()
        self._reset_projection()
        if self.path and self.path.exists():
            self._load()

    # ---------- 持久化 ----------

    def _reset_projection(self):
        self.entities = {}
        self.tickets = {}
        self.custody = {}
        self.evidence = {}
        self.conclusions = {}
        self.conclusion_history = {}
        self.reports = {}

    def _append_log(self, event):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()

    def _load(self):
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            self.events.append(event)
            self._known_event_ids.add(event["event_id"])
            if event.get("device_id") is not None and event.get("device_seq") is not None:
                self._known_device_seqs.add((event["device_id"], event["device_seq"]))
        self._rebuild()

    def _rebuild(self):
        """按发生时间全量重放，派生全部当前状态（幂等、确定性）。"""
        self._reset_projection()
        ordered = sorted(
            enumerate(self.events), key=lambda pair: (pair[1]["occurred_at"], pair[0])
        )
        for _idx, event in ordered:
            self._apply(event)
        self._reconcile()

    # ---------- 写入：离线批量合并 ----------

    def submit_batch(self, items, batch_id=None, default_device=None):
        """合并一批（可能来自离线设备）事件。

        逐项处理：幂等重复跳过、违规项拒绝、合规项追加；每追加一项即重放，
        使同批内“先登记后交接”等先后关系成立。重复编号/位置冲突自动生成
        复核单进入人工复核，绝不自动覆盖。
        """
        if not isinstance(items, list) or not items:
            raise DomainError("events 必须为非空列表")
        result = {"accepted": [], "duplicates": [], "rejected": [], "conflicts": []}
        batch_id = batch_id or new_id("batch")
        before_tickets = set(self.tickets)
        with self._lock:
            for item in items:
                outcome = self._ingest_one(item, batch_id, default_device)
                if outcome:
                    result[outcome["outcome"]].append(outcome["detail"])
                self._rebuild()
            result["conflicts"] = [
                self._ticket_view(tid)
                for tid, t in self.tickets.items()
                if tid not in before_tickets and t["status"] == "open"
            ]
            result["batch_id"] = batch_id
        return result

    def _ingest_one(self, item, batch_id, default_device):
        if not isinstance(item, dict):
            return {"outcome": "rejected",
                    "detail": {"error": "事件必须为对象", "code": "invalid_payload"}}
        event_id = item.get("event_id") or new_id("ev")
        if event_id in self._known_event_ids:
            return {"outcome": "duplicates",
                    "detail": {"event_id": event_id, "reason": "event_id 已存在，按幂等跳过"}}
        device_id = item.get("device_id", default_device)
        device_seq = item.get("device_seq")
        if device_id is not None and device_seq is not None \
                and (device_id, device_seq) in self._known_device_seqs:
            return {"outcome": "duplicates",
                    "detail": {"event_id": event_id, "device_id": device_id,
                               "device_seq": device_seq, "reason": "设备事件号重复，按幂等跳过"}}
        etype = item.get("type")
        try:
            occurred = iso(parse_ts(item.get("occurred_at")))
            actor = str(item.get("actor") or "").strip()
            if not actor:
                raise DomainError("缺少责任人 actor")
            payload = item.get("payload") or {}
            self._validate_shape(etype, payload)
        except DomainError as exc:
            return {"outcome": "rejected",
                    "detail": {"event_id": event_id, "type": etype,
                               "error": str(exc), "code": exc.code}}

        self._seq += 1
        event = {
            "event_id": event_id,
            "type": etype,
            "actor": actor,
            "occurred_at": occurred,
            "recorded_at": iso(parse_ts(item["recorded_at"])) if item.get("recorded_at") else occurred,
            "ingest_seq": self._seq,
            "device_id": device_id,
            "device_seq": device_seq,
            "batch_id": batch_id,
            "payload": payload,
        }
        try:
            # 在当前投影上试算：空间链断裂等硬性违规即时拒绝
            self._apply(event, dry_run=True)
        except DomainError as exc:
            self._seq -= 1
            return {"outcome": "rejected",
                    "detail": {"event_id": event_id, "type": etype,
                               "error": str(exc), "code": exc.code}}
        self.events.append(event)
        self._known_event_ids.add(event_id)
        if device_id is not None and device_seq is not None:
            self._known_device_seqs.add((device_id, device_seq))
        self._append_log(event)
        return {"outcome": "accepted",
                "detail": {"event_id": event_id, "type": etype, "occurred_at": occurred}}

    def _validate_shape(self, etype, payload):
        if etype not in (
            "entity_registered", "custody_transferred", "evidence_added",
            "evidence_voided", "review_resolved", "report_published",
        ):
            raise DomainError(f"未知事件类型 {etype!r}", "unknown_event")
        if not isinstance(payload, dict):
            raise DomainError("payload 必须为对象")

    # ---------- 重放处理器 ----------

    def _apply(self, event, dry_run=False):
        return {
            "entity_registered": self._apply_registration,
            "custody_transferred": self._apply_custody,
            "evidence_added": self._apply_evidence_added,
            "evidence_voided": self._apply_evidence_voided,
            "review_resolved": self._apply_review,
            "report_published": self._apply_publish,
        }[event["type"]](event, dry_run)

    def _apply_registration(self, event, dry_run):
        draft = normalize_entity(event["payload"])
        entity_id = event["payload"].get("id") or f"ent-{event['event_id'][:13]}"
        if entity_id in self.entities:
            raise DomainError(f"实体 id {entity_id} 重复", "duplicate_id")

        problems = []  # (kind, reason, other_entity_id)
        parent = None
        if draft["kind"] != "site":
            pid = draft["parent_id"]
            if not pid:
                problems.append(("context_mismatch", f"{KIND_CN[draft['kind']]} 缺少父级单位", None))
            else:
                parent = self.entities.get(pid)
                if parent is None:
                    problems.append(("context_mismatch", f"父级单位 {pid} 尚未登记（可能离线晚到，补齐后自动解除）", None))
                elif parent.get("dismissed"):
                    problems.append(("context_mismatch",
                                     f"父级单位 {parent['code']} 已被复核驳回，不能挂接", pid))
                elif not parent_allowed(draft["kind"], parent["kind"]):
                    problems.append(("context_mismatch",
                                     f"{KIND_CN[draft['kind']]} 不能挂接在{KIND_CN[parent['kind']]}之下", pid))
                elif not self._within_ancestor_box(draft["geometry"], parent):
                    problems.append(("position_overlap",
                                     f"几何位置超出祖先发掘区/地层范围", pid))

        for other in self.entities.values():
            if other["id"] == entity_id or other.get("dismissed"):
                continue
            if other["kind"] == draft["kind"] and other["code"] == draft["code"]:
                problems.append(("duplicate_code",
                                 f"{KIND_CN[draft['kind']]}编号 {draft['code']} 与 {other['code']} 重复",
                                 other["id"]))
            if draft["geometry"] and other["kind"] == draft["kind"] \
                    and other.get("parent_id") == draft["parent_id"] \
                    and geometry_overlap(draft["geometry"], other.get("geometry")):
                problems.append(("position_overlap",
                                 f"与同层{KIND_CN[other['kind']]} {other['code']} 位置冲突",
                                 other["id"]))
                break

        if dry_run:
            # 硬性违规：父级被驳回/层级非法/越界必须拒绝；
            # “父级尚未登记”可能只是离线晚到，仍接收并挂复核单。
            for kind, _reason, other_id in problems:
                if kind == "context_mismatch" and (other_id is not None or not draft["parent_id"]):
                    raise DomainError(next(r for k, r, _ in problems if k == kind))
            return

        self.entities[entity_id] = {
            "id": entity_id,
            "kind": draft["kind"],
            "code": draft["code"],
            "name": draft["name"],
            "parent_id": draft["parent_id"],
            "geometry": draft["geometry"],
            "region": draft["region"],
            "public_note": draft["public_note"],
            "public": bool(event["payload"].get("public", False)),
            "attrs": draft["attrs"],
            "registered_event": event["event_id"],
            "registered_by": event["actor"],
            "registered_at": event["occurred_at"],
            "dismissed": False,
            "custody_state": None,
            "current_holder": None,
            "aliases": [],
            "ticket_ids": [],
        }
        new_candidate = {
            "entity_id": entity_id, "event_id": event["event_id"],
            "code": draft["code"], "occurred_at": event["occurred_at"],
        }
        for kind, reason, other_id in problems:
            self._open_ticket(kind, reason, draft["kind"], event, new_candidate,
                              self.entities.get(other_id))

    def _open_ticket(self, kind, reason, entity_kind, event, candidate, other):
        if kind == "duplicate_code":
            tid = f"{TICKET_PREFIX[kind]}-{entity_kind}-{candidate['code']}"
        else:
            tid = f"{TICKET_PREFIX[kind]}-{candidate['entity_id']}"
        ticket = self.tickets.get(tid)
        if ticket is None:
            ticket = {
                "id": tid, "kind": kind, "entity_kind": entity_kind,
                "status": "open", "reason": reason,
                "candidates": [], "created_at": event["occurred_at"],
                "resolutions": [], "reopen_count": 0,
            }
            self.tickets[tid] = ticket
        # 已裁定后又出现新的竞争记录 -> 复核单重开，旧裁定保留在案
        if ticket["status"] == "resolved":
            ticket["status"] = "open"
            ticket["reopen_count"] += 1
        for cand in [candidate] + ([self._candidate_of(other)] if other else []):
            if cand and not any(c["event_id"] == cand["event_id"] for c in ticket["candidates"]):
                ticket["candidates"].append(cand)
        ticket["candidates"].sort(key=lambda c: c["occurred_at"])
        ticket["reason"] = reason
        return ticket

    @staticmethod
    def _candidate_of(entity):
        return {"entity_id": entity["id"], "event_id": entity["registered_event"],
                "code": entity["code"], "occurred_at": entity["registered_at"]}

    def _within_ancestor_box(self, geometry, parent):
        """沿父链上溯，几何必须落在最近的盒状祖先（发掘区/墓葬范围）之内。"""
        cur = parent
        seen = set()
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])
            if cur.get("geometry") and cur["geometry"].get("type") == "box":
                return contained_by(geometry, cur["geometry"])
            pid = cur.get("parent_id")
            cur = self.entities.get(pid) if pid else None
        return True

    def _reconcile(self):
        """全量重放后的一致性校正：派生票单归属、实体状态，自动解除已补齐的晚到父级。"""
        # 1) 父级缺失类票单：若现在父级已存在且合法，系统自动解除（非人工冲突）
        for ticket in self.tickets.values():
            if ticket["kind"] != "context_mismatch" or ticket["status"] != "open":
                continue
            still_bad = False
            for cand in ticket["candidates"]:
                ent = self.entities.get(cand["entity_id"])
                if ent is None:
                    still_bad = True
                    break
                pid = ent.get("parent_id")
                parent = self.entities.get(pid) if pid else None
                if not pid or parent is None or parent.get("dismissed") \
                        or not parent_allowed(ent["kind"], parent["kind"]) \
                        or not self._within_ancestor_box(ent.get("geometry"), parent):
                    still_bad = True
                    break
            if not still_bad:
                ticket["status"] = "auto_closed"
                ticket["resolutions"].append({
                    "resolution": "父级已补齐，自动解除",
                    "reviewer": "system", "rationale": ticket["reason"],
                    "resolved_at": ticket["candidates"][-1]["occurred_at"],
                })

        # 2) 由未决票单反推每个实体的 open_tickets
        open_map = {}
        for tid, ticket in self.tickets.items():
            if ticket["status"] != "open":
                continue
            for cand in ticket["candidates"]:
                open_map.setdefault(cand["entity_id"], []).append(tid)

        for ent in self.entities.values():
            tickets_here = open_map.get(ent["id"], [])
            ent["open_tickets"] = sorted(tickets_here)
            if ent["dismissed"]:
                ent["status"] = "驳回"
            elif tickets_here:
                ent["status"] = "待复核"
            elif ent["custody_state"]:
                ent["status"] = ent["custody_state"]
            else:
                ent["status"] = "已确认"

    def _apply_review(self, event, dry_run):
        p = event["payload"]
        for field in ("ticket_id", "resolution", "rationale"):
            if not p.get(field):
                raise DomainError(f"复核事件缺少 {field}")
        if p["resolution"] not in RESOLUTIONS:
            raise DomainError(f"未知裁定方式 {p['resolution']}")
        ticket = self.tickets.get(p["ticket_id"])
        if ticket is None:
            raise DomainError(f"复核单 {p['ticket_id']} 不存在", "ticket_not_found")
        if dry_run:
            return
        ticket["resolutions"].append({
            "resolution": p["resolution"],
            "reviewer": event["actor"],
            "rationale": p["rationale"],
            "chosen_entity_id": p.get("chosen_entity_id"),
            "resolved_event": event["event_id"],
            "resolved_at": event["occurred_at"],
        })
        ticket["status"] = "resolved"

        candidates = list(ticket["candidates"])
        older = candidates[0]["entity_id"]
        newer = [c["entity_id"] for c in candidates[1:]]
        chosen = p.get("chosen_entity_id")

        def dismiss(eid):
            ent = self.entities.get(eid)
            if ent:
                ent["dismissed"] = True

        if p["resolution"] in ("维持原记录", "驳回补录"):
            for eid in newer:
                dismiss(eid)
        elif p["resolution"] == "采用新记录":
            keep = chosen or (newer[-1] if newer else older)
            if keep not in [c["entity_id"] for c in candidates]:
                raise DomainError("chosen_entity_id 不在复核候选中")
            for c in candidates:
                if c["entity_id"] != keep:
                    dismiss(c["entity_id"])
        else:  # 双记并存：互记别名，皆为有效记录
            codes = sorted({c["code"] for c in candidates})
            for c in candidates:
                ent = self.entities.get(c["entity_id"])
                if ent:
                    ent["aliases"] = sorted(set(ent["aliases"]) | {x for x in codes if x != ent["code"]})

    def _apply_custody(self, event, dry_run):
        p = event["payload"]
        for field in ("object_id", "action", "from_party", "to_party", "responsible"):
            if not p.get(field):
                raise DomainError(f"交接事件缺少 {field}")
        if p["action"] not in CUSTODY_ACTIONS:
            raise DomainError(f"未知交接动作 {p['action']}")
        obj = self.entities.get(p["object_id"])
        if obj is None:
            raise DomainError(f"交接对象 {p['object_id']} 不存在", "object_not_found")
        # 被驳回或未决冲突的对象不得流转；晚于交接发生的新冲突不影响已发生的交接
        if obj.get("dismissed"):
            raise DomainError(f"对象 {obj['code']} 已被复核驳回，不能交接", "object_dismissed")
        open_tickets = [
            tid for tid, t in self.tickets.items()
            if t["status"] == "open"
            and any(c["entity_id"] == obj["id"] for c in t["candidates"])
        ]
        if open_tickets:
            raise DomainError(f"对象 {obj['code']} 存在未决复核单 {open_tickets}，先复核再交接",
                              "object_in_review")
        chain = self.custody.setdefault(p["object_id"], [])
        if any(e["event_id"] == event["event_id"] for e in chain):
            return
        if dry_run:
            return
        chain.append({
            "action": p["action"],
            "from_party": p["from_party"],
            "to_party": p["to_party"],
            "responsible": p["responsible"],
            "note": p.get("note", ""),
            "at": event["occurred_at"],
            "event_id": event["event_id"],
            "actor": event["actor"],
        })
        chain.sort(key=lambda e: (e["at"], e["event_id"]))
        obj["custody_state"] = CUSTODY_STATE[p["action"]]
        obj["current_holder"] = p["to_party"]

    def _apply_evidence_added(self, event, dry_run):
        ev = normalize_evidence(event["payload"])
        if ev["attached_entity_id"] not in self.entities:
            raise DomainError(f"证据依附实体 {ev['attached_entity_id']} 不存在", "entity_not_found")
        if ev["subject_id"] not in self.entities:
            raise DomainError(f"证据论断主体 {ev['subject_id']} 不存在", "entity_not_found")
        evidence_id = event["payload"].get("id") or f"evd-{event['event_id'][:12]}"
        if evidence_id in self.evidence:
            raise DomainError(f"证据 id {evidence_id} 重复", "duplicate_id")
        if dry_run:
            return
        rec = dict(ev)
        rec.update({
            "id": evidence_id,
            "status": "active",
            "added_event": event["event_id"],
            "added_by": event["actor"],
            "added_at": event["occurred_at"],
        })
        self.evidence[evidence_id] = rec
        self._recompute_subject(ev["subject_id"], event)

    def _apply_evidence_voided(self, event, dry_run):
        p = event["payload"]
        if not p.get("evidence_id"):
            raise DomainError("撤销事件缺少 evidence_id")
        rec = self.evidence.get(p["evidence_id"])
        if rec is None:
            raise DomainError(f"证据 {p['evidence_id']} 不存在", "evidence_not_found")
        if rec["status"] == "void":
            raise DomainError("证据已撤销，不能重复撤销")
        if dry_run:
            return
        rec["status"] = "void"
        rec["void_event"] = event["event_id"]
        rec["void_by"] = event["actor"]
        rec["void_reason"] = p.get("reason", "")
        rec["void_at"] = event["occurred_at"]
        self._recompute_subject(rec["subject_id"], event)

    def _active_evidence_for(self, subject_id):
        return [e for e in self.evidence.values()
                if e["subject_id"] == subject_id and e["status"] == "active"]

    def _evidence_version(self, subject_id):
        active = self._active_evidence_for(subject_id)
        material = sorted(
            (e["id"], e["type"], e["claim_kind"], e["hypothesis"],
             str(e.get("date_range_bp")), str(e.get("confidence")))
            for e in active
        )
        return payload_hash(material), active

    def _recompute_subject(self, subject_id, trigger_event):
        version, active = self._evidence_version(subject_id)
        conclusions = recompute_conclusions(active, subject_id, now_iso=trigger_event["occurred_at"])
        self.conclusions[subject_id] = conclusions
        self.conclusion_history.setdefault(subject_id, []).append({
            "version": version,
            "trigger_event": trigger_event["event_id"],
            "trigger_kind": trigger_event["type"],
            "at": trigger_event["occurred_at"],
            "conclusions": conclusions,
        })

    def _apply_publish(self, event, dry_run):
        p = event["payload"]
        for field in ("report_id", "title", "subject_ids"):
            if not p.get(field):
                raise DomainError(f"发布事件缺少 {field}")
        if p["report_id"] in self.reports:
            raise DomainError(f"报告 {p['report_id']} 已存在，正式发布不可重复", "report_exists")
        subjects = p["subject_ids"]
        if not isinstance(subjects, list) or not subjects:
            raise DomainError("subject_ids 必须为非空列表")
        for sid in subjects:
            if sid not in self.entities:
                raise DomainError(f"发布主体 {sid} 不存在", "entity_not_found")
        discloses = p.get("discloses", [])
        for did in discloses:
            if did not in self.entities:
                raise DomainError(f"披露清单含未知实体 {did}", "entity_not_found")
        if dry_run:
            return
        snapshot_subjects, adopted = [], []
        for sid in subjects:
            version, active = self._evidence_version(sid)
            snapshot_subjects.append({
                "subject_id": sid,
                "evidence_version": version,
                "conclusions": self.conclusions.get(sid, []),
            })
            for e in active:
                adopted.append({
                    "evidence_id": e["id"], "type": e["type"],
                    "claim_kind": e["claim_kind"], "hypothesis": e["hypothesis"],
                    "confidence": e.get("confidence"),
                    "date_range_bp": e.get("date_range_bp"),
                    "added_event": e["added_event"],
                })
        self.reports[p["report_id"]] = {
            "report_id": p["report_id"],
            "title": p["title"],
            "summary": p.get("summary", ""),
            "subject_ids": list(subjects),
            "discloses": list(discloses),
            "snapshot": {
                "subjects": snapshot_subjects,
                "adopted_evidence": sorted(adopted, key=lambda e: e["evidence_id"]),
                "frozen_by_event": event["event_id"],
            },
            "frozen_at": event["occurred_at"],
            "frozen_by": event["actor"],
        }

    # ---------- 便捷写入 ----------

    @staticmethod
    def _require_ok(result):
        if result["rejected"]:
            raise DomainError(result["rejected"][0]["error"],
                              result["rejected"][0].get("code", "rejected"))
        return result

    def resolve_ticket(self, actor, ticket_id, resolution, rationale,
                       chosen_entity_id=None, occurred_at=None):
        ts = iso(parse_ts(occurred_at)) if occurred_at else iso(datetime.now(timezone.utc))
        return self._require_ok(self.submit_batch([{
            "type": "review_resolved", "actor": actor, "occurred_at": ts,
            "payload": {"ticket_id": ticket_id, "resolution": resolution,
                        "rationale": rationale, "chosen_entity_id": chosen_entity_id},
        }]))

    def add_evidence(self, actor, payload, occurred_at=None):
        ts = iso(parse_ts(occurred_at)) if occurred_at else iso(datetime.now(timezone.utc))
        return self._require_ok(self.submit_batch([{"type": "evidence_added", "actor": actor,
                                   "occurred_at": ts, "payload": payload}]))

    def publish(self, actor, report_id, title, subject_ids, summary="",
                discloses=None, occurred_at=None):
        ts = iso(parse_ts(occurred_at)) if occurred_at else iso(datetime.now(timezone.utc))
        return self._require_ok(self.submit_batch([{
            "type": "report_published", "actor": actor, "occurred_at": ts,
            "payload": {"report_id": report_id, "title": title,
                        "subject_ids": subject_ids, "summary": summary,
                        "discloses": discloses or []},
        }]))

    # ---------- 查询 ----------

    def _ticket_view(self, tid):
        t = self.tickets[tid]
        return {
            "id": t["id"], "kind": t["kind"], "reason": t["reason"],
            "entity_kind": t["entity_kind"], "status": t["status"],
            "created_at": t["created_at"], "reopen_count": t["reopen_count"],
            "candidates": [dict(c) for c in t["candidates"]],
            "resolutions": list(t["resolutions"]),
        }

    def list_tickets(self, status=None):
        with self._lock:
            return [self._ticket_view(tid) for tid, t in self.tickets.items()
                    if status is None or t["status"] == status]

    def _entity_view(self, ent):
        view = {k: ent.get(k) for k in (
            "id", "kind", "code", "name", "parent_id", "geometry", "region",
            "public_note", "public", "attrs", "status", "registered_by",
            "registered_at", "open_tickets", "aliases", "current_holder",
            "custody_state", "dismissed")}
        view["kind_cn"] = KIND_CN[ent["kind"]]
        return view

    def list_entities(self, kind=None, status=None):
        with self._lock:
            out = []
            for ent in self.entities.values():
                if kind and ent["kind"] != kind:
                    continue
                if status and ent["status"] != status:
                    continue
                out.append(self._entity_view(ent))
            return sorted(out, key=lambda e: (e["kind"], e["code"]))

    def ancestors_chain(self, entity_id):
        chain, seen = [], set()
        cur = entity_id
        while cur and cur not in seen:
            seen.add(cur)
            ent = self.entities.get(cur)
            if ent is None:
                chain.append({"id": cur, "missing": True})
                break
            chain.append(self._entity_view(ent))
            cur = ent.get("parent_id")
        return chain

    def _disclosed_ids(self):
        ids = {eid for eid, e in self.entities.items() if e.get("public")}
        for report in self.reports.values():
            ids.update(report["discloses"])
        return ids

    def provenience(self, object_id):
        """由一件器物还原：出土环境、登记来源、流转责任、检测证据、解释变化与发布引用。"""
        with self._lock:
            ent = self.entities.get(object_id)
            if ent is None:
                raise DomainError(f"实体 {object_id} 不存在", "not_found")
            chain = self.ancestors_chain(object_id)
            ancestor_ids = {c["id"] for c in chain if "id" in c}
            sample_ids = {eid for eid, e in self.entities.items()
                          if e.get("parent_id") == object_id and e["kind"] == "sample"}
            attached = {object_id} | sample_ids
            evidence = [dict(e) for e in self.evidence.values()
                        if e["attached_entity_id"] in attached or e["subject_id"] == object_id]
            evidence_ids = {e["id"] for e in evidence}
            context_evidence = [dict(e) for e in self.evidence.values()
                                if e["subject_id"] in ancestor_ids and e["id"] not in evidence_ids]
            referenced_reports = [
                {"report_id": r["report_id"], "title": r["title"], "frozen_at": r["frozen_at"]}
                for r in self.reports.values()
                if object_id in r["subject_ids"] or object_id in r["discloses"]
                or ancestor_ids & set(r["subject_ids"])
            ]
            reg_event = next((e for e in self.events
                              if e["event_id"] == ent["registered_event"]), None)
            return {
                "object": self._entity_view(ent),
                "spatial_context": chain,
                "samples": [self._entity_view(self.entities[sid]) for sid in sorted(sample_ids)],
                "registration": {
                    "event_id": ent["registered_event"],
                    "by": ent["registered_by"],
                    "occurred_at": reg_event["occurred_at"] if reg_event else None,
                    "recorded_at": reg_event["recorded_at"] if reg_event else None,
                    "device_id": reg_event["device_id"] if reg_event else None,
                    "device_seq": reg_event["device_seq"] if reg_event else None,
                    "batch_id": reg_event["batch_id"] if reg_event else None,
                },
                "custody_chain": list(self.custody.get(object_id, [])),
                "evidence": evidence,
                "context_evidence": context_evidence,
                "conclusion_history": self.conclusion_history.get(object_id, []),
                "related_conclusions": {
                    sid: self.conclusion_history.get(sid, [])
                    for sid in sorted(ancestor_ids)
                    if sid in self.conclusion_history and sid != object_id
                },
                "reports": referenced_reports,
            }

    def current_conclusions(self, subject_id):
        with self._lock:
            if subject_id not in self.entities:
                raise DomainError(f"主体 {subject_id} 不存在", "not_found")
            version, _ = self._evidence_version(subject_id)
            return {"subject_id": subject_id, "evidence_version": version,
                    "conclusions": self.conclusions.get(subject_id, []),
                    "history": self.conclusion_history.get(subject_id, [])}

    def get_report(self, report_id):
        with self._lock:
            report = self.reports.get(report_id)
            if report is None:
                raise DomainError(f"报告 {report_id} 不存在", "not_found")
            return json_safe(report)

    def list_reports(self):
        with self._lock:
            return [{"report_id": r["report_id"], "title": r["title"],
                     "frozen_at": r["frozen_at"], "frozen_by": r["frozen_by"],
                     "subject_ids": r["subject_ids"], "discloses": r["discloses"]}
                    for r in sorted(self.reports.values(), key=lambda r: r["frozen_at"])]

    def entity(self, entity_id):
        with self._lock:
            ent = self.entities.get(entity_id)
            if ent is None:
                raise DomainError(f"实体 {entity_id} 不存在", "not_found")
            return self._entity_view(ent)

    def public_entities(self):
        with self._lock:
            disclosed = self._disclosed_ids()
            return [public_entity(self._entity_view(e))
                    for eid, e in self.entities.items() if eid in disclosed]

    def public_reports(self):
        with self._lock:
            return [public_report(r) for r in
                    sorted(self.reports.values(), key=lambda r: r["frozen_at"])]


def json_safe(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
