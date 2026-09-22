"""上召窑秦陵演示数据。

``python3 service.py --seed-demo`` 会把一组带稳定幂等键的事件写入日志，
可重复执行（重跑只跳过已存在事件）。故事线：

2017—2020 外兆沟、内园墙、主陵、陪葬墓、祭祀坑由不同队伍分区分期揭露；
2019 野外平板离线补录造成重复编号与坐标重合，进入复核后人工处置；
2019—2021 碳十四、类型学与铭文证据支持"陪葬墓 M2 墓主为公子高"，
           项目负责人采用该观点并发布 2021 年简报（证据版本冻结）；
2023     新采人骨样品与铭文新释读支持竞争观点"宗室贵族嘉"，年代区间失去交集；
2024     项目负责人改用新观点并发布正式报告；2021 简报快照保持原样。
"""

from events import EventStore
from domain import Projection

DIRECTOR = "秦凤（发掘领队）"
SURVEYOR = "梁测（测绘员）"
CURATOR = "王玉兰（库房管理员）"
LAB = "陈守正（检测机构）"
RESEARCHER = "苏释（研究人员）"
PI = "白衡（项目负责人）"

# (client_event_id, type, data, actor, occurred_at, recorded_at, device, note)
EVENTS = [
    # -- 分区分期揭露 ----------------------------------------------------
    ("demo-001", "ProjectCreated",
     {"project_id": "proj-szy", "name": "上召窑秦陵考古项目", "public": True},
     PI, "2017-03-01T08:00:00Z", None, None, "项目立项"),
    ("demo-002", "AreaOpened",
     {"area_id": "area-wzg", "project_id": "proj-szy", "code": "WZG",
      "name": "外兆沟发掘区"},
     DIRECTOR, "2017-04-02T08:30:00Z", None, None, "一期：外兆沟"),
    ("demo-003", "AreaOpened",
     {"area_id": "area-nyq", "project_id": "proj-szy", "code": "NYQ",
      "name": "内园墙发掘区"},
     DIRECTOR, "2018-03-15T08:30:00Z", None, None, "二期：内园墙"),
    ("demo-004", "AreaOpened",
     {"area_id": "area-zl", "project_id": "proj-szy", "code": "ZL",
      "name": "主陵区"},
     DIRECTOR, "2019-04-10T08:30:00Z", None, None, "三期：主陵"),
    ("demo-005", "AreaOpened",
     {"area_id": "area-pz", "project_id": "proj-szy", "code": "PZ",
      "name": "陪葬墓区"},
     DIRECTOR, "2019-04-12T08:30:00Z", None, None, "三期：陪葬墓"),
    ("demo-006", "AreaOpened",
     {"area_id": "area-js", "project_id": "proj-szy", "code": "JS",
      "name": "祭祀坑区"},
     DIRECTOR, "2020-09-01T08:30:00Z", None, None, "四期：祭祀坑"),

    ("demo-010", "StratumRecorded",
     {"stratum_id": "str-zl-3", "area_id": "area-zl", "code": "ZL③",
      "sequence_no": 3, "period_hint": "秦代文化层"},
     SURVEYOR, "2019-04-20T10:00:00Z", None, None, None),
    ("demo-011", "StratumRecorded",
     {"stratum_id": "str-pz-2", "area_id": "area-pz", "code": "PZ②",
      "sequence_no": 2, "period_hint": "战国晚期—秦"},
     SURVEYOR, "2019-04-22T10:00:00Z", None, None, None),
    ("demo-012", "StratumRecorded",
     {"stratum_id": "str-js-1", "area_id": "area-js", "code": "JS①",
      "sequence_no": 1, "period_hint": "战国—秦"},
     SURVEYOR, "2020-09-05T10:00:00Z", None, None, None),

    ("demo-020", "FeatureRecorded",
     {"feature_id": "feat-m1", "area_id": "area-zl",
      "stratum_id": "str-zl-3", "code": "M1", "feature_type": "陵墓",
      "name": "主陵墓室", "centroid": {"x": 100.0, "y": 100.0}},
     SURVEYOR, "2019-05-02T09:00:00Z", None, None, "主陵未公开"),
    ("demo-021", "FeatureRecorded",
     {"feature_id": "feat-m2", "area_id": "area-pz",
      "stratum_id": "str-pz-2", "code": "M2", "feature_type": "墓葬",
      "name": "二号陪葬墓", "centroid": {"x": 512.5, "y": 308.2}},
     SURVEYOR, "2019-05-08T09:00:00Z", None, None, None),
    ("demo-022", "FeatureRecorded",
     {"feature_id": "feat-h5", "area_id": "area-js",
      "stratum_id": "str-js-1", "code": "H5", "feature_type": "祭祀坑",
      "name": "五号祭祀坑", "centroid": {"x": 640.1, "y": 210.4}},
     SURVEYOR, "2020-09-10T09:00:00Z", None, None, None),

    # -- M2 出土物 -------------------------------------------------------
    ("demo-030", "ArtifactRegistered",
     {"artifact_id": "art-ding", "code": "M2-01", "feature_id": "feat-m2",
      "coordinates": {"x": 512.40, "y": 308.10, "z": -5.20, "precision": 0.05},
      "summary": "秦式铜鼎，出土于M2椁室西侧"},
     SURVEYOR, "2019-06-01T14:20:00Z", None, "D-TOTAL-01", None),
    ("demo-031", "ArtifactRegistered",
     {"artifact_id": "art-zhong", "code": "M2-02", "feature_id": "feat-m2",
      "coordinates": {"x": 512.90, "y": 308.40, "z": -5.35, "precision": 0.05},
      "summary": "青铜锺，肩部有铭文二字"},
     SURVEYOR, "2019-06-01T15:05:00Z", None, "D-TOTAL-01", None),

    # -- 现场暂存 --------------------------------------------------------
    ("demo-032", "CustodyTransferred",
     {"ref_type": "artifact", "ref_id": "art-ding", "action": "暂存",
      "to_party": "工地临时库房", "handler": "王玉兰"},
     CURATOR, "2019-06-02T09:00:00Z", None, None, None),
    ("demo-033", "CustodyTransferred",
     {"ref_type": "artifact", "ref_id": "art-zhong", "action": "暂存",
      "to_party": "工地临时库房", "handler": "王玉兰"},
     CURATOR, "2019-06-02T09:10:00Z", None, None, None),

    # -- 送检/接收（发生在离线设备同步之前） ------------------------------
    ("demo-040", "CustodyTransferred",
     {"ref_type": "artifact", "ref_id": "art-ding", "action": "送检",
      "to_party": "省文保中心修复室", "handler": "王玉兰",
      "tracking_no": "WB-2019-0231"},
     CURATOR, "2019-06-05T10:00:00Z", None, None, "锈蚀状况评估"),
    ("demo-041", "CustodyTransferred",
     {"ref_type": "artifact", "ref_id": "art-ding", "action": "接收",
      "to_party": "省文保中心修复室", "handler": "陈守正"},
     LAB, "2019-06-06T11:30:00Z", None, None, None),

    # -- 离线平板补录：发生于 6月3日，6月8日回营同步。
    #    其中 M2-01 与既有器物重号且坐标重合；同一事件因信号重试提交两次
    #    （相同 client_event_id，第二次幂等忽略）。
    ("demo-050", "ArtifactRegistered",
     {"artifact_id": "art-offline-dup", "code": "M2-01",
      "feature_id": "feat-m2",
      "coordinates": {"x": 512.40, "y": 308.10, "z": -5.20, "precision": 0.1},
      "summary": "离线补录器物（与M2-01重号）"},
     SURVEYOR, "2019-06-03T11:00:00Z", "2019-06-08T18:40:00Z",
     "D-FIELD-03", "离线补录"),
    ("demo-050", "ArtifactRegistered",
     {"artifact_id": "art-offline-dup", "code": "M2-01",
      "feature_id": "feat-m2",
      "coordinates": {"x": 512.40, "y": 308.10, "z": -5.20, "precision": 0.1},
      "summary": "离线补录器物（与M2-01重号）"},
     SURVEYOR, "2019-06-03T11:00:00Z", "2019-06-08T18:40:02Z",
     "D-FIELD-03", "信号重试，幂等"),
    ("demo-051", "ArtifactRegistered",
     {"artifact_id": "art-taopian", "code": "M2-03",
      "feature_id": "feat-m2",
      "coordinates": {"x": 513.20, "y": 309.00, "z": -5.30, "precision": 0.1},
      "summary": "夹砂陶片，椁室西北角采集"},
     SURVEYOR, "2019-06-03T11:20:00Z", "2019-06-08T18:40:05Z",
     "D-FIELD-03", "离线补录"),

    # -- 复核处置：重号器物确认独立，改挂新编号（原记录全部保留）；
    #    坐标重合经人工核实为两器原位并置，结论留痕 ----------------------
    ("demo-052", "ArtifactCorrected",
     {"artifact_id": "art-offline-dup", "code": "M2-04",
      "reason": "复核确认与M2-01为不同器物，按补录顺序赋新号"},
     SURVEYOR, "2019-06-09T09:00:00Z", None, None, "复核后更正编号"),

    # -- 采样与碳十四 ----------------------------------------------------
    ("demo-060", "SampleTaken",
     {"sample_id": "sam-bone-m2", "code": "C14-M2-01",
      "feature_id": "feat-m2", "material": "人骨（墓主肢骨）"},
     DIRECTOR, "2019-06-04T10:00:00Z", None, None, None),
    ("demo-061", "CustodyTransferred",
     {"ref_type": "sample", "ref_id": "sam-bone-m2", "action": "送检",
      "to_party": "加速器质谱实验室", "handler": "王玉兰"},
     CURATOR, "2019-06-10T09:00:00Z", None, None, None),
    ("demo-062", "CustodyTransferred",
     {"ref_type": "sample", "ref_id": "sam-bone-m2", "action": "接收",
      "to_party": "加速器质谱实验室", "handler": "陈守正"},
     LAB, "2019-06-12T14:00:00Z", None, None, None),
    ("demo-063", "RadiocarbonResult",
     {"result_id": "res-rc-m2-01", "sample_id": "sam-bone-m2",
      "lab": "北京大学加速器质谱实验室", "lab_code": "BA-193456",
      "cal_range": [-250, -210]},
     LAB, "2019-09-02T10:00:00Z", None, None, "校正年代：公元前250—前210年"),

    ("demo-064", "SampleTaken",
     {"sample_id": "sam-ding-residue", "code": "C14-M2-02",
      "artifact_id": "art-ding", "material": "鼎腹残留土样"},
     DIRECTOR, "2019-06-04T10:30:00Z", None, None, None),

    # -- 类型学与铭文 ----------------------------------------------------
    ("demo-070", "TypologyClassified",
     {"classification_id": "typ-ding-01", "artifact_id": "art-ding",
      "typology": "秦式浅腹球蹄足铜鼎", "period_range": [-230, -206]},
     RESEARCHER, "2019-11-10T10:00:00Z", None, None,
     "战国晚期至秦统一后形制"),
    ("demo-071", "InscriptionInterpreted",
     {"inscription_id": "ins-zhong-gao", "artifact_id": "art-zhong",
      "reading": "高", "implies_owner": "公子高（秦始皇之子）",
      "period_range": [-221, -207]},
     RESEARCHER, "2019-12-05T10:00:00Z", None, None,
     "初释：锺铭“高”字，器主为公子高"),

    # -- 修复与入库 ------------------------------------------------------
    ("demo-080", "CustodyTransferred",
     {"ref_type": "artifact", "ref_id": "art-ding", "action": "修复",
      "to_party": "省文保中心修复室", "handler": "李修文"},
     CURATOR, "2020-01-08T09:00:00Z", None, None, "去锈补配"),
    ("demo-081", "CustodyTransferred",
     {"ref_type": "artifact", "ref_id": "art-ding", "action": "入库",
      "to_party": "考古院总库房", "handler": "赵守库",
      "tracking_no": "KC-2020-0117"},
     CURATOR, "2020-04-16T15:00:00Z", None, None, None),
    ("demo-082", "CustodyTransferred",
     {"ref_type": "artifact", "ref_id": "art-zhong", "action": "入库",
      "to_party": "考古院总库房", "handler": "赵守库",
      "tracking_no": "KC-2020-0118"},
     CURATOR, "2020-04-16T15:10:00Z", None, None, None),

    # -- 祭祀坑 H5（未公开线） -------------------------------------------
    ("demo-090", "ArtifactRegistered",
     {"artifact_id": "art-taowen", "code": "H5-01", "feature_id": "feat-h5",
      "coordinates": {"x": 640.00, "y": 210.00, "z": -2.80, "precision": 0.05},
      "summary": "陶文陶片，戳印“丽邑”残字"},
     SURVEYOR, "2020-09-20T11:00:00Z", None, "D-TOTAL-01", None),
    ("demo-091", "SampleTaken",
     {"sample_id": "sam-char-h5", "code": "C14-H5-01",
      "feature_id": "feat-h5", "material": "炭样"},
     DIRECTOR, "2020-09-21T10:00:00Z", None, None, None),
    ("demo-092", "CustodyTransferred",
     {"ref_type": "sample", "ref_id": "sam-char-h5", "action": "送检",
      "to_party": "加速器质谱实验室", "handler": "王玉兰"},
     CURATOR, "2020-10-09T09:00:00Z", None, None, None),
    ("demo-093", "RadiocarbonResult",
     {"result_id": "res-rc-h5-01", "sample_id": "sam-char-h5",
      "lab": "北京大学加速器质谱实验室", "lab_code": "BA-205771",
      "cal_range": [-230, -200]},
     LAB, "2021-01-15T10:00:00Z", None, None, None),

    # -- 互竞观点（2019 起）与采用 ---------------------------------------
    ("demo-100", "ClaimProposed",
     {"claim_id": "claim-date-qin", "claim_kind": "dating",
      "target_type": "feature", "target_id": "feat-m2",
      "title": "M2下葬于秦统一之后",
      "range": [-220, -207],
      "reasoning": "铜鼎形制与锺铭“高”均指向统一后，碳十四区间覆盖这一范围。",
      "evidence_ids": ["res-rc-m2-01", "typ-ding-01", "ins-zhong-gao"]},
     RESEARCHER, "2020-02-10T10:00:00Z", None, None, None),
    ("demo-101", "ClaimProposed",
     {"claim_id": "claim-date-warring", "claim_kind": "dating",
      "target_type": "feature", "target_id": "feat-m2",
      "title": "M2下葬于战国晚期秦王政世",
      "range": [-240, -222],
      "reasoning": "碳十四校正区间偏早段，鼎式尚有战国遗风。",
      "evidence_ids": ["res-rc-m2-01", "typ-ding-01"]},
     RESEARCHER, "2020-02-12T10:00:00Z", None, None, None),
    ("demo-102", "ClaimProposed",
     {"claim_id": "claim-owner-gao", "claim_kind": "owner",
      "target_type": "feature", "target_id": "feat-m2",
      "title": "M2墓主为公子高",
      "proposed_owner": "公子高（秦始皇之子）",
      "reasoning": "青铜锺肩铭“高”字为器主自名，陪葬规格与皇子身份相称。",
      "evidence_ids": ["ins-zhong-gao", "res-rc-m2-01"]},
     RESEARCHER, "2020-02-15T10:00:00Z", None, None, None),
    ("demo-110", "ViewpointAdopted",
     {"claim_id": "claim-date-qin", "reason": "综合测年与类型学，采用秦统一后说"},
     PI, "2021-02-20T10:00:00Z", None, None, None),
    ("demo-111", "ViewpointAdopted",
     {"claim_id": "claim-owner-gao", "reason": "铭文证据直接，采用公子高说"},
     PI, "2021-02-20T10:05:00Z", None, None, None),

    # -- 2021 简报发布（快照在追加时固化）与有限披露 ----------------------
    # ReportPublished 的 snapshots 由 seed() 在写入前生成
    ("demo-120", "ReportPublished",
     {"report_id": "R-2021-01",
      "title": "上召窑秦陵陪葬墓M2发掘简报",
      "note": "采用公子高说；测年采用秦统一后区间。",
      "targets": [["feature", "feat-m2"],
                  ["artifact", "art-ding"],
                  ["artifact", "art-zhong"]]},
     PI, "2021-03-01T09:00:00Z", None, None, "正式发布：冻结所采用证据版本"),
    ("demo-121", "DiscoveryDisclosed",
     {"target_type": "feature", "target_id": "feat-m2",
      "public_name": "上召窑二号陪葬墓",
      "public_summary": "秦代高等级陪葬墓，出土铜鼎、青铜锺等器物；墓主身份仍在研究。",
      "reveal_coordinates": False},
     PI, "2021-03-10T09:00:00Z", None, None,
     "公开层：不披露精确坐标；主陵、祭祀坑未披露"),

    # -- 2023 新证据：晚段人骨测年 + 铭文新释读 → 竞争观点 ----------------
    ("demo-130", "SampleTaken",
     {"sample_id": "sam-bone-m2-b", "code": "C14-M2-03",
      "feature_id": "feat-m2", "material": "人骨（墓主牙齿）"},
     DIRECTOR, "2023-05-18T10:00:00Z", None, None, "复检采样"),
    ("demo-131", "CustodyTransferred",
     {"ref_type": "sample", "ref_id": "sam-bone-m2-b", "action": "送检",
      "to_party": "加速器质谱实验室", "handler": "王玉兰"},
     CURATOR, "2023-05-22T09:00:00Z", None, None, None),
    ("demo-132", "RadiocarbonResult",
     {"result_id": "res-rc-m2-03", "sample_id": "sam-bone-m2-b",
      "lab": "北京大学加速器质谱实验室", "lab_code": "BA-231108",
      "cal_range": [-205, -195]},
     LAB, "2023-08-30T10:00:00Z", None, None,
     "新校正区间：公元前205—前195年，与前210年旧区间不相交"),
    ("demo-133", "InscriptionInterpreted",
     {"inscription_id": "ins-zhong-jia", "artifact_id": "art-zhong",
      "reading": "嘉", "implies_owner": "秦宗室贵族“嘉”",
      "period_range": [-206, -195]},
     RESEARCHER, "2023-09-15T10:00:00Z", None, None,
     "新释读：细审首字非“高”而为“嘉”，器主另为宗室贵族嘉"),
    ("demo-134", "ClaimProposed",
     {"claim_id": "claim-owner-jia", "claim_kind": "owner",
      "target_type": "feature", "target_id": "feat-m2",
      "title": "M2墓主为宗室贵族嘉",
      "proposed_owner": "秦宗室贵族“嘉”",
      "reasoning": "新释读为“嘉”；牙齿测年晚至前205—前195年，公子高已殁，不可能入葬。",
      "evidence_ids": ["ins-zhong-jia", "res-rc-m2-03"]},
     RESEARCHER, "2023-10-20T10:00:00Z", None, None, None),

    # -- 2024 项目负责人改用新观点并发布正式报告；旧报告不改 --------------
    ("demo-140", "ViewpointAdopted",
     {"claim_id": "claim-owner-jia",
      "reason": "新测年晚于公子高卒年，铭文复核成立，改用宗室贵族嘉说；旧简报存档不改"},
     PI, "2024-01-12T10:00:00Z", None, None, None),
    ("demo-141", "ReportPublished",
     {"report_id": "R-2024-02",
      "title": "上召窑秦陵陪葬墓M2发掘报告",
      "note": "依据新测年与铭文复核，采用宗室贵族嘉说。",
      "targets": [["feature", "feat-m2"], ["artifact", "art-zhong"]]},
     PI, "2024-03-08T09:00:00Z", None, None,
     "正式发布：冻结新的证据版本；R-2021-01原样留存"),
]

# 需要人工处置的复核单（坐标重合项）：用描述前缀在 seed 末尾定位
_POS_TICKET_HINT = "512.4"


def seed(store: EventStore):
    """写入演示事件，返回实际新增条数。幂等：重跑不产生重复事件。"""
    added = 0
    for cid, kind, raw_data, actor, occurred, recorded, device, note in EVENTS:
        data = dict(raw_data)
        if kind == "ReportPublished":
            proj = Projection().replay(store.events())
            data["snapshots"] = [
                proj._frozen_snapshot(t[0], t[1]) for t in data["targets"]]
        if cid == "demo-052":
            proj = Projection().replay(store.events())
            dup_ticket = next(
                (t for t in proj.tickets_list(active_only=True)
                 if t["kind"] == "duplicate_code"
                 and "art-offline-dup" in t["refs"]), None)
            if dup_ticket:
                data["ticket_id"] = dup_ticket["ticket_id"]
        event, duplicated = store.append(
            kind, data, actor=actor, occurred_at=occurred,
            recorded_at=recorded or occurred, client_event_id=cid,
            device=device, note=note)
        if not duplicated:
            added += 1

    # 坐标重合复核单：离线补录器物改名后重号自动消除，坐标项经人工核实确认
    if added:
        proj = Projection().replay(store.events())
        pos_ticket = next((t for t in proj.tickets_list(active_only=True)
                           if t["kind"] == "coordinate_conflict"
                           and _POS_TICKET_HINT in t["description"]), None)
        if pos_ticket:
            store.append(
                "ReviewResolved",
                {"ticket_id": pos_ticket["ticket_id"], "resolution": "confirm",
                 "reason": "经现场复核与照片比对，两器原位并置，坐标重合属实，维持原记录"},
                actor=DIRECTOR, occurred_at="2019-06-09T11:00:00Z",
                client_event_id="demo-053")
            added += 1
    return added
