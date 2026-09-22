"""领域投影：从只追加事件流重建考古现场上下文。

投影在每次查询前按 ``occurred_at``（业务发生时间）重放全部事件，因此：

* 离线设备补录的旧事件自动回到时间轴上的正确位置，与实时记录按发生时间合并；
* 重复编号、位置冲突、层位矛盾等在重放中被确定性识别，生成内容寻址的复核单，
  任何一条原始记录都不会被自动覆盖；
* 发布报告在重放到发布事件时即时冻结结论快照，此后新证据只会改变"当前结论"，
  旧报告快照永不改写。
"""

import hashlib
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# 常量与角色

ARTIFACT = "artifact"
SAMPLE = "sample"
FEATURE = "feature"

ROLES = {
    "public": "公众",
    "surveyor": "测绘员",
    "director": "发掘领队",
    "curator": "库房管理员",
    "lab": "检测机构",
    "researcher": "研究人员",
    "pi": "项目负责人",
}

CUSTODY_ACTIONS = {"送检", "接收", "暂存", "修复", "入库", "出库", "返还"}


class ApiError(Exception):
    """命令形状或权限错误（追加事件之前拦截）。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ----------------------------------------------------------------------------
# 时间工具

def norm_ts(value):
    """把 ISO-8601 时间统一为 UTC 的 ``YYYY-MM-DDTHH:MM:SSZ``。"""
    if not value:
        raise ApiError(400, "缺少时间戳")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ApiError(400, f"无法解析时间戳: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def format_range(start, end):
    """把公元纪年整数区间格式化为中文显示。"""
    def one(year):
        if year < 0:
            return f"公元前{-year}"
        if year == 0:
            return "公元元年"
        return f"公元{year}"
    if start == end:
        return one(start)
    return f"{one(start)}—{one(end)}"


def _short_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


# ----------------------------------------------------------------------------
# 投影

class Projection:
    """事件流的完整只读重建结果。"""

    def __init__(self):
        self.projects = {}
        self.areas = {}
        self.strata = {}
        self.features = {}
        self.artifacts = {}
        self.samples = {}
        self.custody = {}          # (kind, id) -> [交接事件视图]
        self.evidence = []         # 检测/类型/铭文
        self.claims = []           # 研究观点（年代/墓主主张）
        self.adoptions = {}        # (target_kind, target_id, claim_kind) -> 事件视图
        self.tickets = {}          # 内容寻址复核单
        self.identity_clashes = {}  # (bucket, id) -> [重复登记摘要]
        self.reports = {}
        self.disclosures = {}      # (kind, id) -> 披露事件视图
        self.last_seq = 0

    # -- 重放入口 ---------------------------------------------------------

    def replay(self, events):
        ordered = sorted(events, key=lambda e: (_dt(e["payload"]["occurred_at"]),
                                                e["seq"]))
        for event in ordered:
            self._apply(event)
            self._reconcile_tickets(event["seq"])
        self.last_seq = max((e["seq"] for e in events), default=0)
        return self

    def _apply(self, event):
        seq = event["seq"]
        p = event["payload"]
        kind, data, actor = p["type"], p["data"], p["actor"]
        at = p["occurred_at"]
        handler = getattr(self, f"_on_{kind}", None)
        if handler:
            handler(seq, at, actor, data, event.get("client_event_id"),
                    p.get("device"), p.get("note"))

    def _ref(self, seq, at, actor, **extra):
        view = {"event_seq": seq, "occurred_at": at, "actor": actor}
        view.update(extra)
        return view

    # -- 项目与空间骨架 ---------------------------------------------------

    def _clash(self, bucket, ent_id, code, seq, at, actor, cid, device):
        """同一实体 ID 被再次登记：原记录不覆盖，重复登记进复核。"""
        self.identity_clashes.setdefault((bucket, ent_id), []).append(
            self._ref(seq, at, actor, code=code, device=device,
                      client_event_id=cid))

    def _on_ProjectCreated(self, seq, at, actor, data, cid, device, note):
        pid = data["project_id"]
        if pid in self.projects:
            self._clash("project", pid, data["name"], seq, at, actor, cid, device)
            return
        self.projects[pid] = {
            "project_id": pid, "name": data["name"],
            "public": bool(data.get("public", True)),
            "created": self._ref(seq, at, actor),
        }

    def _on_AreaOpened(self, seq, at, actor, data, cid, device, note):
        aid = data["area_id"]
        if aid in self.areas:
            self._clash("area", aid, data["code"], seq, at, actor, cid, device)
            return
        self.areas[aid] = {
            "area_id": aid, "project_id": data.get("project_id"),
            "code": data["code"], "name": data.get("name", data["code"]),
            "opened": self._ref(seq, at, actor, device=device),
        }

    def _on_StratumRecorded(self, seq, at, actor, data, cid, device, note):
        sid = data["stratum_id"]
        if sid in self.strata:
            self._clash("stratum", sid, data["code"], seq, at, actor, cid, device)
            return
        self.strata[sid] = {
            "stratum_id": sid, "area_id": data["area_id"],
            "code": data["code"], "sequence_no": int(data["sequence_no"]),
            "period_hint": data.get("period_hint"),
            "recorded": self._ref(seq, at, actor, device=device),
        }

    def _on_FeatureRecorded(self, seq, at, actor, data, cid, device, note):
        fid = data["feature_id"]
        if fid in self.features:
            self._clash("feature", fid, data["code"], seq, at, actor, cid, device)
            return
        self.features[fid] = {
            "feature_id": fid, "area_id": data["area_id"],
            "stratum_id": data.get("stratum_id"),
            "code": data["code"], "feature_type": data.get("feature_type", "遗迹"),
            "name": data.get("name"),
            "centroid": data.get("centroid"),
            "recorded": self._ref(seq, at, actor, device=device),
        }

    # -- 出土物与样品 -----------------------------------------------------

    def _on_ArtifactRegistered(self, seq, at, actor, data, cid, device, note):
        aid = data["artifact_id"]
        if aid in self.artifacts:
            self._clash("artifact", aid, data["code"], seq, at, actor, cid, device)
            return
        self.artifacts[aid] = {
            "artifact_id": aid, "code": data["code"],
            "feature_id": data["feature_id"],
            "coordinates": _norm_coords(data.get("coordinates")),
            "summary": data.get("summary", ""),
            "registration": self._ref(seq, at, actor, device=device,
                                      client_event_id=cid, note=note),
            "history": [self._ref(seq, at, actor, action="登记",
                                  code=data["code"],
                                  coordinates=_norm_coords(data.get("coordinates")),
                                  device=device)],
        }

    def _on_ArtifactCorrected(self, seq, at, actor, data, cid, device, note):
        art = self.artifacts.get(data["artifact_id"])
        if not art:
            return
        change = {"action": "更正"}
        if "code" in data and data["code"] != art["code"]:
            change["code"] = {"from": art["code"], "to": data["code"]}
            art["code"] = data["code"]
        if "coordinates" in data:
            new_coords = _norm_coords(data["coordinates"])
            if new_coords != art["coordinates"]:
                change["coordinates"] = {"from": art["coordinates"], "to": new_coords}
                art["coordinates"] = new_coords
        if "summary" in data and data["summary"] != art.get("summary"):
            change["summary"] = {"from": art.get("summary", ""), "to": data["summary"]}
            art["summary"] = data["summary"]
        change.update(self._ref(seq, at, actor, reason=data.get("reason"),
                                ticket_id=data.get("ticket_id"), device=device))
        art["history"].append(change)

    def _on_SampleTaken(self, seq, at, actor, data, cid, device, note):
        sid = data["sample_id"]
        if sid in self.samples:
            self._clash("sample", sid, data["code"], seq, at, actor, cid, device)
            return
        self.samples[sid] = {
            "sample_id": sid, "code": data["code"],
            "artifact_id": data.get("artifact_id"),
            "feature_id": data.get("feature_id"),
            "material": data.get("material", ""),
            "taken": self._ref(seq, at, actor, device=device),
        }

    # -- 交接责任链 -------------------------------------------------------

    def _on_CustodyTransferred(self, seq, at, actor, data, cid, device, note):
        key = (data["ref_type"], data["ref_id"])
        action = data["action"]
        if action not in CUSTODY_ACTIONS:
            raise ApiError(400, f"未知交接动作: {action}")
        self.custody.setdefault(key, []).append(self._ref(
            seq, at, actor, action=action, to_party=data.get("to_party", ""),
            handler=data.get("handler", ""), tracking_no=data.get("tracking_no"),
            note=note))

    # -- 证据：碳十四 / 类型学 / 铭文 ------------------------------------

    def _on_RadiocarbonResult(self, seq, at, actor, data, cid, device, note):
        sample = self.samples.get(data["sample_id"])
        target = None
        if sample:
            target = (ARTIFACT, sample["artifact_id"]) if sample.get("artifact_id") \
                else (FEATURE, sample.get("feature_id"))
        self.evidence.append({
            "evidence_id": data["result_id"], "kind": "carbon14",
            "sample_id": data["sample_id"], "target": target,
            "lab": data.get("lab", ""), "lab_code": data.get("lab_code", ""),
            "range": _norm_range(data.get("cal_range")),
            "detail": f"{data.get('lab','')} {data.get('lab_code','')}".strip(),
            "ref": self._ref(seq, at, actor),
        })

    def _on_TypologyClassified(self, seq, at, actor, data, cid, device, note):
        self.evidence.append({
            "evidence_id": data["classification_id"], "kind": "typology",
            "artifact_id": data["artifact_id"],
            "target": (ARTIFACT, data["artifact_id"]),
            "typology": data.get("typology", ""),
            "range": _norm_range(data.get("period_range")),
            "detail": data.get("typology", ""),
            "ref": self._ref(seq, at, actor),
        })

    def _on_InscriptionInterpreted(self, seq, at, actor, data, cid, device, note):
        self.evidence.append({
            "evidence_id": data["inscription_id"], "kind": "inscription",
            "artifact_id": data["artifact_id"],
            "target": (ARTIFACT, data["artifact_id"]),
            "reading": data.get("reading", ""),
            "implies_owner": data.get("implies_owner"),
            "range": _norm_range(data.get("period_range")),
            "detail": data.get("reading", ""),
            "ref": self._ref(seq, at, actor),
        })

    # -- 互竞研究观点 -----------------------------------------------------

    def _on_ClaimProposed(self, seq, at, actor, data, cid, device, note):
        target = (data["target_type"], data["target_id"])
        self.claims.append({
            "claim_id": data["claim_id"], "kind": data["claim_kind"],
            "target": target,
            "proposed_owner": data.get("proposed_owner"),
            "range": _norm_range(data.get("range")),
            "title": data.get("title", ""), "reasoning": data.get("reasoning", ""),
            "evidence_ids": list(data.get("evidence_ids", [])),
            "ref": self._ref(seq, at, actor),
        })

    def _on_ViewpointAdopted(self, seq, at, actor, data, cid, device, note):
        claim = self._claim(data["claim_id"])
        if not claim:
            raise ApiError(400, f"观点不存在: {data['claim_id']}")
        key = (claim["target"][0], claim["target"][1], claim["kind"])
        self.adoptions[key] = self._ref(
            seq, at, actor, claim_id=claim["claim_id"],
            reason=data.get("reason", ""))

    # -- 发布冻结 / 公开披露 ---------------------------------------------

    def _on_ReportPublished(self, seq, at, actor, data, cid, device, note):
        rid = data["report_id"]
        if rid in self.reports:
            raise ApiError(400, f"报告编号已存在: {rid}")
        targets = [(t[0], t[1]) for t in data["targets"]]
        # 发布快照在提交时即由 API 固化进事件载荷（哈希链保护），
        # 重放只原样读取；新证据永远无法改写已冻结的证据版本。
        snapshots = data.get("snapshots") or [
            self._frozen_snapshot(kind, tid) for kind, tid in targets]
        self.reports[rid] = {
            "report_id": rid, "title": data["title"],
            "note": data.get("note", ""),
            "targets": targets, "snapshots": snapshots,
            "published": self._ref(seq, at, actor),
        }

    def _on_DiscoveryDisclosed(self, seq, at, actor, data, cid, device, note):
        key = (data["target_type"], data["target_id"])
        self.disclosures[key] = self._ref(
            seq, at, actor, public_name=data.get("public_name", ""),
            public_summary=data.get("public_summary", ""),
            reveal_coordinates=bool(data.get("reveal_coordinates", False)))

    def _on_ReviewResolved(self, seq, at, actor, data, cid, device, note):
        ticket = self.tickets.get(data["ticket_id"])
        resolution = data.get("resolution", "dismiss")
        entry = self._ref(seq, at, actor, resolution=resolution,
                          reason=data.get("reason", ""))
        if ticket is None:
            # 兜底：处置对象缺失也保留留痕，不会静默丢弃。
            ticket = {"ticket_id": data["ticket_id"], "kind": "unknown",
                      "active": False, "refs": [], "history": []}
            self.tickets[data["ticket_id"]] = ticket
        ticket["manual_resolution"] = entry
        ticket["active"] = False

    # -- 冲突复核 ---------------------------------------------------------

    def _reconcile_tickets(self, current_seq):
        found = {}

        def raise_ticket(key, kind, description, refs, severity="冲突"):
            found[key] = (kind, description, refs, severity)

        # 同一实体 ID 被重复登记（第二条及以后全部留痕，无一被覆盖）
        label_map = {"project": "项目", "area": "发掘区", "stratum": "地层",
                     "feature": "遗迹单位", "artifact": "出土物", "sample": "样品"}
        for (bucket, ent_id), clashes in self.identity_clashes.items():
            label = label_map.get(bucket, bucket)
            codes = "、".join(sorted({c["code"] for c in clashes}))
            key = "id:" + _short_hash(f"{bucket}|{ent_id}|{codes}")
            raise_ticket(key, "duplicate_identity",
                         f"{label}内部标识 {ent_id} 被重复登记（编号 {codes}），"
                         f"{len(clashes)} 条重复记录均保留待核，未覆盖原记录",
                         [ent_id])

        # 重复编号：器物、样品、遗迹、地层、发掘区各自编码不得重号
        for bucket, label in ((self.artifacts, "出土物"), (self.samples, "样品"),
                              (self.features, "遗迹单位"), (self.strata, "地层"),
                              (self.areas, "发掘区")):
            by_code = {}
            for ent in bucket.values():
                by_code.setdefault(ent["code"], []).append(ent)
            for code, ents in by_code.items():
                if len(ents) > 1:
                    ids = sorted(e[next(k for k in ("artifact_id", "sample_id",
                                                    "feature_id", "stratum_id",
                                                    "area_id") if k in e)]
                                 for e in ents)
                    key = "dup:" + _short_hash(f"{label}|{code}|{','.join(ids)}")
                    raise_ticket(key, "duplicate_code",
                                 f"{label}编号重复：{code}（{len(ents)} 条记录并存，均未覆盖）",
                                 ids)

        # 位置冲突：同一发掘区内坐标完全重合的不同器物
        coords = {}
        for art in self.artifacts.values():
            c = art.get("coordinates")
            if not c:
                continue
            feature = self.features.get(art["feature_id"])
            area = feature["area_id"] if feature else "?"
            bucket_key = (area, c.get("x"), c.get("y"), c.get("z"))
            coords.setdefault(bucket_key, []).append(art["artifact_id"])
        for (area, x, y, z), ids in coords.items():
            if len(ids) > 1:
                ids_s = sorted(ids)
                key = "pos:" + _short_hash(f"{area}|{x}|{y}|{z}|{','.join(ids_s)}")
                loc = f"({x}, {y}" + (f", {z}" if z is not None else "") + ")"
                raise_ticket(key, "coordinate_conflict",
                             f"发掘区 {area} 内 {loc} 被多个出土物占用：{', '.join(ids_s)}",
                             ids_s)

        # 遗迹形心重合
        centroids = {}
        for feat in self.features.values():
            c = feat.get("centroid")
            if not c:
                continue
            centroids.setdefault((feat["area_id"], c.get("x"), c.get("y")), []
                                 ).append(feat["feature_id"])
        for (area, x, y), ids in centroids.items():
            if len(ids) > 1:
                ids_s = sorted(ids)
                key = "fpos:" + _short_hash(f"{area}|{x}|{y}|{','.join(ids_s)}")
                raise_ticket(key, "coordinate_conflict",
                             f"发掘区 {area} 内遗迹形心 ({x}, {y}) 重合：{', '.join(ids_s)}",
                             ids_s)

        # 同区地层层序撞号
        seq_buckets = {}
        for st in self.strata.values():
            seq_buckets.setdefault((st["area_id"], st["sequence_no"]), []
                                   ).append(st["stratum_id"])
        for (area, no), ids in seq_buckets.items():
            if len(ids) > 1:
                ids_s = sorted(ids)
                key = "strat:" + _short_hash(f"{area}|{no}|{','.join(ids_s)}")
                raise_ticket(key, "stratum_order_conflict",
                             f"发掘区 {area} 层位序号 {no} 被多个地层占用", ids_s)

        # 空间链不得断裂：器物→遗迹→地层→发掘区
        for aid, art in self.artifacts.items():
            feature = self.features.get(art["feature_id"])
            if not feature:
                key = "link:" + _short_hash(f"artifact-missing-feature|{aid}")
                raise_ticket(key, "broken_context",
                             f"出土物 {art['code']} 缺少有效遗迹单位，空间上下文不完整",
                             [aid])
                continue
            stratum = self.strata.get(feature.get("stratum_id"))
            if not stratum or stratum["area_id"] != feature["area_id"]:
                key = "link:" + _short_hash(f"feature-stratum|{feature['feature_id']}")
                raise_ticket(key, "broken_context",
                             f"遗迹 {feature['code']} 的地层与发掘区关系断裂或跨区",
                             [feature["feature_id"]])
        for sid, sample in self.samples.items():
            if not sample.get("artifact_id") and not sample.get("feature_id"):
                key = "link:" + _short_hash(f"sample-dangling|{sid}")
                raise_ticket(key, "broken_context",
                             f"样品 {sample['code']} 未关联任何器物或遗迹", [sid])

        # 更新复核单状态
        for key, (kind, description, refs, severity) in found.items():
            ticket = self.tickets.get(key)
            if ticket is None:
                self.tickets[key] = {
                    "ticket_id": key, "kind": kind, "severity": severity,
                    "description": description, "refs": refs,
                    "first_seen_event": current_seq, "active": True,
                    "history": [],
                }
            else:
                ticket["active"] = not ticket.get("manual_resolution")
                ticket["last_seen_event"] = current_seq
        for key, ticket in self.tickets.items():
            if key not in found and ticket.get("active") \
                    and not ticket.get("manual_resolution"):
                ticket["active"] = False
                ticket["fixed_event"] = current_seq

    # -- 查询支撑 ---------------------------------------------------------

    def _claim(self, claim_id):
        return next((c for c in self.claims if c["claim_id"] == claim_id), None)

    def _evidence_for(self, kind, target_id):
        """归并到某目标（器物/遗迹）的全部证据。

        遗迹的证据包含：直接采自遗迹的样品检测结果，以及遗迹内出土器物上的
        类型学/铭文证据与器物样品的碳十四结果。
        """
        artifact_ids = set()
        sample_ids = set()
        if kind == FEATURE:
            for art in self.artifacts.values():
                if art["feature_id"] == target_id:
                    artifact_ids.add(art["artifact_id"])
            for sample in self.samples.values():
                if sample.get("feature_id") == target_id:
                    sample_ids.add(sample["sample_id"])
        result = []
        for ev in self.evidence:
            if ev.get("target") == (kind, target_id):
                result.append(ev)
                continue
            if kind != FEATURE:
                continue
            if ev["kind"] == "carbon14":
                sample = self.samples.get(ev.get("sample_id"))
                if sample and (sample.get("feature_id") == target_id
                               or sample.get("artifact_id") in artifact_ids):
                    result.append(ev)
            elif ev.get("target") and ev["target"][0] == ARTIFACT \
                    and ev["target"][1] in artifact_ids:
                result.append(ev)
        return result

    def conclusion(self, kind, target_id):
        """重算某目标的当前年代/墓主结论（不触碰任何已发布快照）。"""
        evidence = self._evidence_for(kind, target_id)
        ranges = [(e, e["range"]) for e in evidence if e.get("range")]
        intersection = None
        if ranges:
            intersection = [max(r[1][0] for r in ranges),
                            min(r[1][1] for r in ranges)]
            if intersection[0] > intersection[1]:
                intersection = None  # 互竞区间不相交

        claims = [c for c in self.claims if c["target"] == (kind, target_id)]
        inscription_owners = [
            {"source": e["evidence_id"], "owner": e["implies_owner"],
             "reading": e["reading"], "event_seq": e["ref"]["event_seq"]}
            for e in evidence if e["kind"] == "inscription" and e.get("implies_owner")
        ]
        adopted = {}
        for claim_kind in ("dating", "owner"):
            adopted[claim_kind] = self.adoptions.get((kind, target_id, claim_kind))

        # 证据版本同时取决于证据、观点与采用决定：改用观点即产生新版本
        version_basis = sorted({e["ref"]["event_seq"] for e in evidence}
                               | {c["ref"]["event_seq"] for c in claims}
                               | {v["event_seq"] for v in adopted.values() if v})
        version = hashlib.sha1(
            ",".join(str(s) for s in version_basis).encode()).hexdigest()[:12]

        owner_candidates = {}
        for cand in inscription_owners:
            owner_candidates.setdefault(cand["owner"], []).append(
                f"铭文 {cand['reading']}（{cand['source']}）")
        for claim in claims:
            if claim["kind"] == "owner" and claim.get("proposed_owner"):
                owner_candidates.setdefault(claim["proposed_owner"], []).append(
                    f"观点 {claim['claim_id']}：{claim['title']}")

        return {
            "target": {"type": kind, "id": target_id},
            "recomputed_at_seq": self.last_seq,
            "evidence_version": version,
            "dating": {
                "evidence": [_evidence_view(e) for e in evidence],
                "ranges": [{"evidence_id": e["evidence_id"], "kind": e["kind"],
                            "range": r, "label": format_range(*r),
                            "detail": e["detail"]} for e, r in ranges],
                "consensus_range": intersection,
                "consensus_label": format_range(*intersection) if intersection
                else "证据区间相互竞争，暂无交集",
                "adopted_claim": adopted["dating"]["claim_id"]
                if adopted["dating"] else None,
            },
            "owner": {
                "candidates": [{"proposed_owner": k, "support": v}
                               for k, v in sorted(owner_candidates.items())],
                "adopted_claim": adopted["owner"]["claim_id"]
                if adopted["owner"] else None,
                "adopted_owner": self._owner_of_adopted(kind, target_id, adopted["owner"]),
            },
            "claims": [_claim_view(c) for c in claims],
        }

    def _owner_of_adopted(self, kind, target_id, adopted_view):
        if not adopted_view:
            return None
        claim = self._claim(adopted_view["claim_id"])
        return claim.get("proposed_owner") if claim else None

    def _frozen_snapshot(self, kind, target_id):
        """发布时刻的结论快照（写入报告记录后不再变化）。"""
        conclusion = self.conclusion(kind, target_id)
        code, feature_info = None, None
        if kind == ARTIFACT:
            art = self.artifacts.get(target_id)
            if art:
                code = art["code"]
                feature = self.features.get(art["feature_id"])
                if feature:
                    feature_info = {"feature_id": feature["feature_id"],
                                    "code": feature["code"],
                                    "feature_type": feature["feature_type"]}
        elif kind == FEATURE:
            feat = self.features.get(target_id)
            if feat:
                code = feat["code"]
        return {
            "target": {"type": kind, "id": target_id},
            "code": code, "feature": feature_info,
            "evidence_version": conclusion["evidence_version"],
            "frozen_at_seq": self.last_seq,
            "dating": {
                "consensus_label": conclusion["dating"]["consensus_label"],
                "consensus_range": conclusion["dating"]["consensus_range"],
                "adopted_claim": conclusion["dating"]["adopted_claim"],
            },
            "owner": {
                "adopted_owner": conclusion["owner"]["adopted_owner"],
                "adopted_claim": conclusion["owner"]["adopted_claim"],
                "candidates": conclusion["owner"]["candidates"],
            },
        }

    # -- 器物全宗 ---------------------------------------------------------

    def dossier(self, artifact_id):
        art = self.artifacts.get(artifact_id)
        if not art:
            return None
        feature = self.features.get(art["feature_id"])
        stratum = self.strata.get(feature["stratum_id"]) if feature else None
        area = self.areas.get(stratum["area_id"]) if stratum else \
            (self.areas.get(feature["area_id"]) if feature else None)
        related_samples = [s for s in self.samples.values()
                           if s.get("artifact_id") == artifact_id]
        custody = self.custody.get((ARTIFACT, artifact_id), [])
        sample_chain = []
        for sample in related_samples:
            sample_chain.append({
                "sample": _sample_view(sample),
                "custody": self.custody.get((SAMPLE, sample["sample_id"]), []),
                "results": [_evidence_view(e) for e in self.evidence
                            if e["kind"] == "carbon14"
                            and e.get("sample_id") == sample["sample_id"]],
            })
        reports = []
        for report in self.reports.values():
            for snap in report["snapshots"]:
                hit = snap["target"] == {"type": ARTIFACT, "id": artifact_id}
                if not hit and feature and snap["target"] == {
                        "type": FEATURE, "id": feature["feature_id"]}:
                    hit = True
                if hit:
                    reports.append({"report_id": report["report_id"],
                                    "title": report["title"],
                                    "published": report["published"],
                                    "snapshot": snap})
        return {
            "artifact": _artifact_view(art),
            "context_chain": {
                "feature": _feature_view(feature) if feature else None,
                "stratum": _stratum_view(stratum) if stratum else None,
                "area": _area_view(area) if area else None,
                "unbroken": bool(feature and stratum and area
                                  and stratum["area_id"] == feature["area_id"]),
            },
            "edit_history": art["history"],
            "custody_chain": custody,
            "samples": sample_chain,
            "evidence": [_evidence_view(e)
                         for e in self._evidence_for(ARTIFACT, artifact_id)],
            "current_conclusion": self.conclusion(ARTIFACT, artifact_id),
            "reports": reports,
            "review_flags": [_ticket_view(t) for t in self._tickets_for(artifact_id)],
        }

    def _tickets_for(self, artifact_id):
        return [t for t in self.tickets.values() if artifact_id in t.get("refs", [])]

    # -- 公开视图 ---------------------------------------------------------

    def public_artifact(self, artifact_id):
        art = self.artifacts.get(artifact_id)
        if not art:
            return None
        feature = self.features.get(art["feature_id"])
        disclosure = self.disclosures.get((FEATURE, art["feature_id"])) if feature \
            else None
        published_snaps = []
        for report in self.reports.values():
            for snap in report["snapshots"]:
                if snap["target"] == {"type": ARTIFACT, "id": artifact_id}:
                    published_snaps.append((report, snap))
                if feature and snap["target"] == {"type": FEATURE,
                                                   "id": feature["feature_id"]}:
                    published_snaps.append((report, snap))
        if not disclosure and not published_snaps:
            return None  # 未公开发现：不承认其存在
        reveal = bool(disclosure and disclosure.get("reveal_coordinates"))
        # 同一报告可能同时包含器物与所属遗迹快照，按报告去重合并
        reports = {}
        for report, snap in published_snaps:
            entry = reports.setdefault(report["report_id"], {
                "report_id": report["report_id"], "title": report["title"],
                "dating": snap["dating"],
                "owner": {"adopted_owner": snap["owner"]["adopted_owner"]}})
            if not entry["owner"]["adopted_owner"] and snap["owner"]["adopted_owner"]:
                entry["owner"]["adopted_owner"] = snap["owner"]["adopted_owner"]
        return {
            "code": art["code"],
            "designation": (disclosure or {}).get("public_name")
            or (feature["code"] if feature else art["code"]),
            "summary": (disclosure or {}).get("public_summary", "")
            if disclosure else "",
            "feature_type": feature["feature_type"] if feature else None,
            "coordinates": art["coordinates"] if reveal else None,
            "coordinates_note": "精确坐标暂不公开" if not reveal else None,
            "published": list(reports.values()),
        }

    def public_artifacts(self):
        out = []
        for aid in sorted(self.artifacts):
            view = self.public_artifact(aid)
            if view:
                out.append(view)
        return out

    def public_reports(self):
        return [{"report_id": r["report_id"], "title": r["title"],
                 "published_at": r["published"]["occurred_at"],
                 "summary": r["note"]} for r in self.reports.values()]

    # -- 列表视图 ---------------------------------------------------------

    def tickets_list(self, active_only=False):
        items = sorted(self.tickets.values(),
                       key=lambda t: t.get("first_seen_event", 0))
        if active_only:
            items = [t for t in items if t.get("active")]
        return [_ticket_view(t) for t in items]


# ----------------------------------------------------------------------------
# 视图与规范化

def _norm_coords(coords):
    if not coords:
        return None
    return {"x": float(coords["x"]), "y": float(coords["y"]),
            "z": float(coords["z"]) if coords.get("z") is not None else None,
            "precision": coords.get("precision")}


def _norm_range(rng):
    if not rng:
        return None
    return [int(rng[0]), int(rng[1])]


def _ref_view(ref):
    if not ref:
        return None
    return {k: v for k, v in ref.items()}


def _area_view(a):
    return None if not a else {
        "area_id": a["area_id"], "code": a["code"], "name": a["name"],
        "opened": _ref_view(a["opened"])}


def _stratum_view(s):
    return None if not s else {
        "stratum_id": s["stratum_id"], "area_id": s["area_id"], "code": s["code"],
        "sequence_no": s["sequence_no"], "period_hint": s.get("period_hint"),
        "recorded": _ref_view(s["recorded"])}


def _feature_view(f):
    return None if not f else {
        "feature_id": f["feature_id"], "area_id": f["area_id"],
        "stratum_id": f.get("stratum_id"), "code": f["code"],
        "feature_type": f["feature_type"], "name": f.get("name"),
        "centroid": f.get("centroid"), "recorded": _ref_view(f["recorded"])}


def _artifact_view(a):
    return {"artifact_id": a["artifact_id"], "code": a["code"],
            "feature_id": a["feature_id"], "coordinates": a["coordinates"],
            "summary": a.get("summary", ""),
            "registration": _ref_view(a["registration"])}


def _sample_view(s):
    return {"sample_id": s["sample_id"], "code": s["code"],
            "artifact_id": s.get("artifact_id"),
            "feature_id": s.get("feature_id"), "material": s["material"],
            "taken": _ref_view(s["taken"])}


def _evidence_view(e):
    return {"evidence_id": e["evidence_id"], "kind": e["kind"],
            "range": e.get("range"),
            "range_label": format_range(*e["range"]) if e.get("range") else None,
            "detail": e.get("detail"), "typology": e.get("typology"),
            "reading": e.get("reading"),
            "implies_owner": e.get("implies_owner"),
            "lab": e.get("lab"), "lab_code": e.get("lab_code"),
            "sample_id": e.get("sample_id"),
            "recorded": _ref_view(e["ref"])}


def _claim_view(c):
    return {"claim_id": c["claim_id"], "kind": c["kind"],
            "target": c["target"], "title": c["title"],
            "proposed_owner": c.get("proposed_owner"), "range": c.get("range"),
            "range_label": format_range(*c["range"]) if c.get("range") else None,
            "reasoning": c.get("reasoning"), "evidence_ids": c["evidence_ids"],
            "proposed_by": _ref_view(c["ref"])}


def _ticket_view(t):
    status = "待复核" if t.get("active") else (
        "已处置-" + t["manual_resolution"]["resolution"]
        if t.get("manual_resolution") else "已消除")
    view = {"ticket_id": t["ticket_id"], "kind": t["kind"],
            "severity": t.get("severity", "冲突"),
            "description": t["description"], "refs": t.get("refs", []),
            "status": status, "active": bool(t.get("active")),
            "first_seen_event": t.get("first_seen_event")}
    if t.get("manual_resolution"):
        view["resolution"] = _ref_view(t["manual_resolution"])
    if t.get("fixed_event"):
        view["fixed_event"] = t["fixed_event"]
    return view


# ----------------------------------------------------------------------------
# 命令白名单：在追加事件之前做形状校验（语义冲突由投影进复核，不在这里拦）

REQUIRED = {
    "ProjectCreated": ["project_id", "name"],
    "AreaOpened": ["area_id", "code"],
    "StratumRecorded": ["stratum_id", "area_id", "code", "sequence_no"],
    "FeatureRecorded": ["feature_id", "area_id", "code"],
    "ArtifactRegistered": ["artifact_id", "code", "feature_id"],
    "ArtifactCorrected": ["artifact_id"],
    "SampleTaken": ["sample_id", "code"],
    "CustodyTransferred": ["ref_type", "ref_id", "action", "handler"],
    "RadiocarbonResult": ["result_id", "sample_id"],
    "TypologyClassified": ["classification_id", "artifact_id"],
    "InscriptionInterpreted": ["inscription_id", "artifact_id", "reading"],
    "ClaimProposed": ["claim_id", "claim_kind", "target_type", "target_id", "title"],
    "ViewpointAdopted": ["claim_id"],
    "ReportPublished": ["report_id", "title", "targets"],
    "DiscoveryDisclosed": ["target_type", "target_id"],
    "ReviewResolved": ["ticket_id", "resolution"],
}

# 各命令允许的角色
PERMISSIONS = {
    "ProjectCreated": {"director", "pi"},
    "AreaOpened": {"surveyor", "director", "pi"},
    "StratumRecorded": {"surveyor", "director", "pi"},
    "FeatureRecorded": {"surveyor", "director", "pi"},
    "ArtifactRegistered": {"surveyor", "director", "pi"},
    "ArtifactCorrected": {"surveyor", "director", "pi"},
    "SampleTaken": {"director", "lab", "pi"},
    "CustodyTransferred": {"curator", "lab", "director", "pi"},
    "RadiocarbonResult": {"lab", "researcher", "pi"},
    "TypologyClassified": {"researcher", "pi"},
    "InscriptionInterpreted": {"researcher", "pi"},
    "ClaimProposed": {"researcher", "director", "pi"},
    "ViewpointAdopted": {"pi", "director"},
    "ReportPublished": {"pi", "director"},
    "DiscoveryDisclosed": {"pi", "director"},
    "ReviewResolved": {"director", "pi", "surveyor"},
}

# 不可变字段：更正器物不得改挂遗迹（空间链不可断裂，只能新登记/走复核）
IMMUTABLE_ON_CORRECT = {"artifact_id", "feature_id"}


def validate_command(kind, data, role):
    """形状与权限校验；通过后由 API 层追加事件。"""
    if kind not in REQUIRED:
        raise ApiError(400, f"未知事件类型: {kind}")
    if role not in PERMISSIONS[kind]:
        raise ApiError(403, f"{ROLES.get(role, role)} 无权执行 {kind}")
    if not isinstance(data, dict):
        raise ApiError(400, "data 必须是对象")
    for field in REQUIRED[kind]:
        if data.get(field) is None:
            raise ApiError(400, f"缺少必填字段: {field}")
    if kind == "ArtifactCorrected":
        for field in data:
            if field in IMMUTABLE_ON_CORRECT:
                raise ApiError(400, f"更正不得修改 {field}：空间上下文不可断裂")
    if kind == "CustodyTransferred" and data["action"] not in CUSTODY_ACTIONS:
        raise ApiError(400, f"未知交接动作: {data['action']}")
    if kind == "ClaimProposed" and data["claim_kind"] not in ("dating", "owner"):
        raise ApiError(400, "claim_kind 必须是 dating 或 owner")
    if kind == "ReportPublished":
        targets = data["targets"]
        if not isinstance(targets, list) or not targets:
            raise ApiError(400, "targets 必须是非空数组")
        for target in targets:
            if not isinstance(target, list) or len(target) != 2 \
                    or target[0] not in (ARTIFACT, FEATURE):
                raise ApiError(400, "targets 元素必须是 [artifact|feature, id]")


def project(store):
    """从事件存储重建当前投影。"""
    return Projection().replay(store.events())
