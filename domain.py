"""考古现场上下文档案的领域规则（纯函数，无 IO）。

约定：
- 空间层级为 发掘区 site -> 地层 stratum -> 遗迹单位 feature -> 出土物 find -> 样品 sample。
- 样品也可直接挂在遗迹单位下（人骨、土壤等）。
- 几何坐标可为 point 或 box（米制工地坐标）；仅用于内部复核，不向公众输出。
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

CONTRACT_PATH = "domain_contract.json"

ENTITY_KINDS = ("site", "stratum", "feature", "find", "sample")
KIND_CN = {
    "site": "发掘区",
    "stratum": "地层",
    "feature": "遗迹单位",
    "find": "出土物",
    "sample": "样品",
}
# 允许的父级种类：不可断裂的空间链
ALLOWED_PARENTS = {
    "site": (None,),
    "stratum": ("site",),
    "feature": ("stratum",),
    "find": ("feature", "stratum"),
    "sample": ("find", "feature"),
}

EVIDENCE_WEIGHTS = {
    "碳十四测年": 5,
    "人骨测年": 5,
    "地层关系": 4,
    "铭文释读": 3,
    "器物类型学": 2,
    "陶文拓片": 2,
}

CONFLICT_KINDS = ("duplicate_code", "position_overlap", "context_mismatch")
RESOLUTIONS = ("维持原记录", "采用新记录", "双记并存", "驳回补录")
CUSTODY_ACTIONS = ("送检", "暂存", "修复", "入库", "提取研究")

POSITION_TOLERANCE_M = 0.5  # 点与点视为同位置的阈值

CST = timezone(timedelta(hours=8))


class DomainError(ValueError):
    """请求内容违反领域约束。"""

    def __init__(self, message, code="invalid_payload"):
        super().__init__(message)
        self.code = code


def parse_ts(value):
    """解析 ISO8601；朴素时间按东八区处理，返回带时区 datetime。"""
    if value is None:
        raise DomainError("缺少发生时间 occurred_at")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str):
        raise DomainError("时间格式应为 ISO8601 字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DomainError(f"无法解析时间 {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(CST).isoformat()


def require(payload, fields):
    """校验必填字段，返回缺失列表。"""
    missing = [f for f in fields if payload.get(f) in (None, "")]
    if missing:
        raise DomainError(f"缺少必填字段: {', '.join(missing)}")


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:12]


# ---------- 空间关系 ----------

def parent_allowed(kind, parent_kind):
    return parent_kind in ALLOWED_PARENTS[kind]


def _point_in_box(point, box):
    x, y = point["coordinates"]
    return box["min"][0] <= x <= box["max"][0] and box["min"][1] <= y <= box["max"][1]


def _boxes_overlap(a, b):
    return not (
        a["max"][0] < b["min"][0]
        or b["max"][0] < a["min"][0]
        or a["max"][1] < b["min"][1]
        or b["max"][1] < a["min"][1]
    )


def _point_distance(p, q):
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


def geometry_overlap(a, b, tolerance=POSITION_TOLERANCE_M):
    """两个几何是否抢占同一空间位置。"""
    if not a or not b:
        return False
    ta, tb = a.get("type"), b.get("type")
    if ta == "box" and tb == "box":
        return _boxes_overlap(a, b)
    if ta == "point" and tb == "point":
        return _point_distance(a["coordinates"], b["coordinates"]) <= tolerance
    point, box = (a, b) if ta == "point" else (b, a)
    if point.get("type") == "point" and box.get("type") == "box":
        return _point_in_box(point, box)
    return False


def contained_by(geometry, parent_geometry):
    """子级几何必须落在最近的 box 祖先之内；无盒状祖先时不做此校验。"""
    if not geometry or not parent_geometry or parent_geometry.get("type") != "box":
        return True
    if geometry.get("type") == "point":
        return _point_in_box(geometry, parent_geometry)
    if geometry.get("type") == "box":
        return (
            _point_in_box({"coordinates": geometry["min"]}, parent_geometry)
            and _point_in_box({"coordinates": geometry["max"]}, parent_geometry)
        )
    return True


def normalize_entity(payload):
    """规整登记事件载荷为实体草稿。"""
    require(payload, ["kind", "code"])
    kind = payload["kind"]
    if kind not in ENTITY_KINDS:
        raise DomainError(f"未知实体种类 {kind!r}")
    code = str(payload["code"]).strip()
    if not code:
        raise DomainError("编号不能为空")
    geometry = payload.get("geometry")
    if geometry is not None:
        geometry = _validate_geometry(geometry)
    return {
        "kind": kind,
        "code": code,
        "parent_id": payload.get("parent_id"),
        "geometry": geometry,
        "name": payload.get("name", ""),
        "public_note": payload.get("public_note", ""),
        "region": payload.get("region", ""),
        "attrs": payload.get("attrs", {}) or {},
    }


def _validate_geometry(geometry):
    gtype = geometry.get("type")
    if gtype == "point":
        coords = geometry.get("coordinates")
        if not (isinstance(coords, list) and len(coords) >= 2):
            raise DomainError("point 需要 coordinates: [x, y]")
        return {"type": "point", "coordinates": [float(coords[0]), float(coords[1])]}
    if gtype == "box":
        mn, mx = geometry.get("min"), geometry.get("max")
        if not (isinstance(mn, list) and isinstance(mx, list) and len(mn) >= 2 and len(mx) >= 2):
            raise DomainError("box 需要 min/max: [x, y]")
        if mn[0] >= mx[0] or mn[1] >= mx[1]:
            raise DomainError("box 的 min 必须小于 max")
        return {
            "type": "box",
            "min": [float(mn[0]), float(mn[1])],
            "max": [float(mx[0]), float(mx[1])],
        }
    raise DomainError(f"不支持的几何类型 {gtype!r}")


# ---------- 证据与竞争结论 ----------

def evidence_score(evidence):
    """单条证据对其支持假说的贡献分。置信度 0~1，缺省 0.8。"""
    base = EVIDENCE_WEIGHTS.get(evidence["type"], 1)
    confidence = evidence.get("confidence")
    confidence = 0.8 if confidence is None else min(1.0, max(0.0, float(confidence)))
    return round(base * (0.5 + 0.5 * confidence), 3)


def normalize_evidence(payload):
    require(payload, ["subject_id", "type", "claim_kind", "hypothesis"])
    if payload["type"] not in EVIDENCE_WEIGHTS:
        raise DomainError(f"未知证据类型 {payload['type']!r}")
    claim_kind = payload["claim_kind"]
    if claim_kind not in ("date", "owner"):
        raise DomainError("claim_kind 仅支持 date（年代）或 owner（墓主）")
    date_range = payload.get("date_range_bp")
    if date_range is not None:
        if not (isinstance(date_range, list) and len(date_range) == 2):
            raise DomainError("date_range_bp 应为 [早界BP, 晚界BP]")
        start_bp, end_bp = float(date_range[0]), float(date_range[1])
        if start_bp < end_bp:
            raise DomainError("BP 区间应为 [较大值(较早), 较小值(较晚)]")
        date_range = [start_bp, end_bp]
    confidence = payload.get("confidence", 0.8)
    return {
        "subject_id": payload["subject_id"],
        "attached_entity_id": payload.get("attached_entity_id") or payload["subject_id"],
        "type": payload["type"],
        "claim_kind": claim_kind,
        "hypothesis": str(payload["hypothesis"]).strip(),
        "label": payload.get("label", payload["hypothesis"]),
        "date_range_bp": date_range,
        "confidence": confidence,
        "interpreter": payload.get("interpreter", ""),
        "source_doc": payload.get("source_doc", ""),
        "note": payload.get("note", ""),
    }


def recompute_conclusions(active_evidence, subject_id, now_iso=None):
    """对同一主体（通常是一座墓）按当前有效证据重算年代/墓主结论。

    竞争假说并列保留：领先者份额不足 60% 或次优差距过小则标记 contested。
    """
    buckets = {}
    for ev in active_evidence:
        if ev.get("subject_id") != subject_id or ev.get("status") == "void":
            continue
        key = ev["claim_kind"]
        buckets.setdefault(key, []).append(ev)

    conclusions = []
    for claim_kind, evs in buckets.items():
        hyp = {}
        for ev in evs:
            slot = hyp.setdefault(
                ev["hypothesis"],
                {"hypothesis": ev["hypothesis"], "label": ev["label"], "score": 0.0,
                 "evidence_ids": [], "ranges_bp": [], "types": set()},
            )
            slot["score"] += evidence_score(ev)
            slot["evidence_ids"].append(ev["id"])
            slot["types"].add(ev["type"])
            if ev.get("date_range_bp"):
                slot["ranges_bp"].append(ev["date_range_bp"])
            slot["label"] = ev["label"] or slot["label"]

        ranked = sorted(hyp.values(), key=lambda s: s["score"], reverse=True)
        total = sum(s["score"] for s in ranked) or 1.0
        winner = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        contested = bool(
            len(ranked) > 1
            and (winner["score"] / total < 0.6 or second["score"] / winner["score"] >= 0.6)
        )
        merged_range = None
        if winner["ranges_bp"]:
            merged_range = [
                max(r[0] for r in winner["ranges_bp"]),
                min(r[1] for r in winner["ranges_bp"]),
            ]
        conclusions.append(
            {
                "subject_id": subject_id,
                "claim_kind": claim_kind,
                "claim_cn": "年代" if claim_kind == "date" else "墓主",
                "winner": {
                    "hypothesis": winner["hypothesis"],
                    "label": winner["label"],
                    "score": round(winner["score"], 3),
                    "share": round(winner["score"] / total, 3),
                    "evidence_ids": winner["evidence_ids"],
                    "date_range_bp": merged_range,
                    "types": sorted(winner["types"]),
                },
                "contenders": [
                    {
                        "hypothesis": s["hypothesis"],
                        "label": s["label"],
                        "score": round(s["score"], 3),
                        "share": round(s["score"] / total, 3),
                        "evidence_ids": s["evidence_ids"],
                        "types": sorted(s["types"]),
                    }
                    for s in ranked[1:]
                ],
                "contested": contested,
                "evidence_count": len(evs),
                "recomputed_at": now_iso,
            }
        )
    return conclusions


# ---------- 公众脱敏 ----------

def public_entity(entity):
    """公众视图：删除精确坐标，仅保留大区与已公开描述。"""
    return {
        "id": entity["id"],
        "kind": entity["kind"],
        "kind_cn": KIND_CN[entity["kind"]],
        "code": entity["code"],
        "name": entity.get("name", ""),
        "public_note": entity.get("public_note", ""),
        "region": entity.get("region", ""),
    }


def public_report(report):
    """公众版报告：只给结论标签与证据类别，不给数值区间、责任人与坐标。"""
    conclusions = []
    for subject in report["snapshot"]["subjects"]:
        for c in subject["conclusions"]:
            conclusions.append({
                "subject_id": subject["subject_id"],
                "claim_cn": c["claim_cn"],
                "winner_label": c["winner"]["label"],
                "contested": c["contested"],
                "evidence_types": c["winner"]["types"],
            })
    return {
        "report_id": report["report_id"],
        "title": report["title"],
        "published_at": report["frozen_at"],
        "summary": report.get("summary", ""),
        "conclusions": conclusions,
        "discloses": report.get("discloses", []),
    }
