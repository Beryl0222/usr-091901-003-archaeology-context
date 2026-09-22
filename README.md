# 考古现场上下文档案

连接发掘区、地层、遗迹单位、出土物、样品与研究观点，以**只增事件流（event sourcing）**保存现场上下文、流转责任与解释演变。配合上召窑秦陵这类多队伍、多年期、纸质与个人硬盘并存的考古项目，解决“某个墓主推断究竟依赖哪一层证据”无法追溯的问题。

## 领域原则（见 `domain_contract.json`）

1. **空间链不可断裂**：发掘区 → 地层 → 遗迹单位 → 出土物 → 样品；登记时校验父级存在、层级合法、几何落在父级范围内。
2. **离线按原发生时间合并**：事件携带 `occurred_at`（现场发生时间）与 `recorded_at`（补录时间）；日志按接收追加、投影按发生时间重放。设备以 `(device_id, device_seq)` 去重，事件 `event_id` 幂等——**重复提交安全，绝不覆盖**。
3. **冲突只进复核**：重复编号、同层位置抢占、挂接矛盾一律生成复核单（`待复核`），由项目负责人裁定（维持原记录／采用新记录／双记并存／驳回补录），不自动覆盖。离线晚到事件补登的父级若先缺失，父级补齐后复核单自动解除。
4. **交接留责**：送检、暂存、修复、入库、提取研究每次交接都记录交出方、接收方、责任人与时间，链按发生时间排序（晚补录的历史交接插回原位）。
5. **竞争证据并列**：碳十四区间、器物类型、铭文释读、人骨测年等作为证据并列留存，支持彼此竞争的年代/墓主假说；结论按当前有效证据**重算**（带版本哈希），撤销证据只标记 void，不删除。
6. **发布只冻结证据版本**：正式发布保存所采用证据及结论的不可变快照；之后新证据重算当前结论，旧报告一字不改。
7. **公众视图脱敏**：`/public/*` 无令牌可访问，隐藏精确坐标、未公开发现与未披露墓址；完整视图需角色令牌。项目负责人可由一件器物经 `/v1/provenience/{id}` 还原出土环境、登记来源（含离线设备与批次）、流转链、检测证据、解释变化与相关报告。

## 运行

```bash
python3 service.py --check                 # 配置与日志自检
python3 service.py --port 8000             # 启动，事件存于 data/events.jsonl
python3 service.py --data /path/log.jsonl  # 指定事件日志
python3 -m unittest -v                     # 全部测试
```

## 接口

| 方法 | 路径 | 角色 |
|---|---|---|
| GET | `/health` `/contract` | 公开 |
| GET | `/public/entities` `/public/reports` | 公众（已脱敏） |
| POST | `/v1/events/sync` | 田野/保管/研究（按事件类型鉴权），离线补录入口 |
| GET | `/v1/entities[?kind=&status=]` `/v1/entities/{id}` | 全部内部角色 |
| GET | `/v1/provenience/{id}` | 内部角色（器物全链路还原） |
| GET | `/v1/conclusions/{subject}` | 内部角色（当前重算结论与历史） |
| GET | `/v1/tickets[?status=open]` | 内部角色 |
| POST | `/v1/reviews` | 项目负责人（复核裁定） |
| POST | `/v1/evidence` | 保管/研究（追加证据，触发重算） |
| POST | `/v1/reports` | 项目负责人（发布冻结） |
| GET | `/v1/reports` `/v1/reports/{id}` | 内部角色（完整冻结快照） |
| GET | `/v1/events` | 项目负责人（审计日志） |

鉴权：`Authorization: Bearer <token>`，默认令牌 `field-surveyor`、`field-lead`、`custody-store`、`custody-lab`、`custody-restorer`、`research-scholar`、`director`；可用环境变量 `ARCH_TOKENS`（JSON）覆盖。

## 事件负载示例

离线补录一批发掘登记（注意 `occurred_at` 是真实发掘时刻，可早于提交多日）：

```json
POST /v1/events/sync
{"device_id": "tablet-T03", "events": [
  {"event_id": "e1", "type": "entity_registered", "actor": "测绘员甲",
   "occurred_at": "2026-04-02T09:10:00+08:00", "device_seq": 1,
   "payload": {"id": "zone-I", "kind": "site", "code": "I区", "region": "上召窑",
               "geometry": {"type": "box", "min": [0,0], "max": [200,200]}}},
  {"event_id": "e2", "type": "entity_registered", "actor": "测绘员甲",
   "occurred_at": "2026-04-02T09:40:00+08:00", "device_seq": 2,
   "payload": {"id": "L3", "kind": "stratum", "code": "第3层", "parent_id": "zone-I"}},
  {"event_id": "e3", "type": "entity_registered", "actor": "田野队员乙",
   "occurred_at": "2026-04-03T15:20:00+08:00", "device_seq": 3,
   "payload": {"id": "M1", "kind": "feature", "code": "M1", "parent_id": "L3",
               "geometry": {"type": "box", "min": [40,40], "max": [46,48]},
               "name": "陪葬墓M1"}},
  {"event_id": "e4", "type": "entity_registered", "actor": "田野队员乙",
   "occurred_at": "2026-04-03T16:05:00+08:00", "device_seq": 4,
   "payload": {"id": "M1-ding", "kind": "find", "code": "铜鼎01", "parent_id": "M1",
               "geometry": {"type": "point", "coordinates": [43.2,44.1]}}}
]}
```

响应区分 `accepted / duplicates / rejected / conflicts`；冲突项返回复核单而非被覆盖。
