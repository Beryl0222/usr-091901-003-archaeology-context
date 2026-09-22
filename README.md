# 考古现场上下文档案

上召窑秦陵式多队伍、多年度考古项目的现场上下文后端。发掘区、地层、遗迹单位、
出土物与样品之间保持**不可断裂的空间与先后关系**；野外设备离线补录按原发生时间
合并；重复编号与位置冲突进入人工复核而非自动覆盖；送检、暂存、修复、入库的每次
交接都保留责任人；互竞的年代与墓主观点可以并存；正式发布只冻结所采用的证据版本，
新证据重算当前结论而旧报告永不改写。

## 设计原则：一切事实只追加

系统只有一份事实来源：**哈希链事件日志**（`data/events.jsonl`）。

* 每条事件含 `seq`（记录序）、`occurred_at`（业务发生时间）、`recorded_at`
  （服务器接收时间）、`client_event_id`（设备幂等键）、`actor`（责任人）；
* 每条事件的哈希包含前一条哈希，任何篡改在 `GET /v1/verify` 或重启加载时暴露；
* 没有 UPDATE/DELETE：编号更正是 `ArtifactCorrected`，观点变更是
  `ViewpointAdopted`，复核仲裁是 `ReviewResolved`，全部留痕；
* 查询投影在每次请求前**按 `occurred_at` 重放全部事件**（同刻按 `seq` 兜底），
  因此离线带回的旧事件自动回到时间轴上的正确位置；
* 发布快照在 `ReportPublished` 提交时即固化进事件载荷——哪怕之后补录一条
  2018 年的事件，2021 年报告里冻结的结论也逐字节不变。

## 文件结构

| 文件 | 职责 |
|---|---|
| `events.py` | 只追加哈希链事件存储（内存 + JSONL 落盘、幂等、防篡改） |
| `domain.py` | 领域投影：空间链、冲突复核、责任链、互竞观点、结论重算、发布冻结、公开脱敏 |
| `api.py` | HTTP 路由、角色鉴权、单条/批量补录、复核处置、全宗与公开查询 |
| `service.py` | 服务入口（`--check` / `--seed-demo` / `--reset`） |
| `demo.py` | 上召窑秦陵完整业务故事演示数据（2017–2024，50 条事件） |
| `domain_contract.json` | 领域契约：角色、事件类型、状态与不可破坏原则 |
| `test_service.py` | 基础服务契约测试 |
| `test_archaeology.py` | 领域原则端到端测试（21 例） |

纯 Python 标准库，无第三方依赖。

## 快速开始

```bash
python3 service.py --check           # 校验契约与哈希链
python3 service.py --seed-demo       # 写入上召窑秦陵演示数据（可重复执行，幂等）
python3 service.py --port 8000       # 启动服务
python3 -m unittest -v               # 全部测试
```

## 鉴权

请求头：`X-Role`（public/surveyor/director/curator/lab/researcher/pi），
写操作还需 `X-User`（责任人姓名，非 ASCII 用 UTF-8 百分号编码）。

## API 一览

### 写入（全部以追加事件表达）

* `POST /v1/events` — 追加单条事件；`occurred_at` 可回填，`client_event_id` 幂等
* `POST /v1/events/batch` — 离线设备批量补录，逐条返回结果（207=部分失败）
* `POST /v1/reviews/{ticket_id}/resolve` — 人工处置复核单（confirm/reassign/dismiss）

事件类型：`ProjectCreated` `AreaOpened` `StratumRecorded` `FeatureRecorded`
`ArtifactRegistered` `ArtifactCorrected` `SampleTaken` `CustodyTransferred`
`RadiocarbonResult` `TypologyClassified` `InscriptionInterpreted` `ClaimProposed`
`ViewpointAdopted` `ReportPublished` `DiscoveryDisclosed` `ReviewResolved`。

### 内部查询（项目角色）

* `GET /v1/artifacts/{id}/dossier` — **器物全宗**：出土环境空间链、更正史、
  交接责任链、样品与检测、全部证据、当前重算结论、所涉报告、复核标记
* `GET /v1/artifacts/{id}/conclusion`、`GET /v1/features/{id}/conclusion`
  — 当前年代/墓主结论（互竞区间、候选墓主、所采用观点、证据版本号）
* `GET /v1/reviews[?active=1]` — 复核单（重号、坐标重合、层位矛盾、断链）
* `GET /v1/reports`、`GET /v1/reports/{id}` — 报告及其冻结快照
* `GET /v1/areas|strata|features|artifacts|samples` — 实体列表
* `GET /v1/events?from=&to=` — 审计导出原始哈希链
* `GET /v1/verify` — 哈希链完整性校验

### 公开查询（无需角色）

* `GET /public/artifacts[?code=]`、`GET /public/artifacts/{id}`
* `GET /public/reports`、`GET /public/reports/{id}`

未披露发现返回与"不存在"完全相同的 404；主陵、祭祀坑等未公开线在公开层不可见；
精确坐标仅在 `DiscoveryDisclosed(reveal_coordinates=true)` 后才公开。

## 冲突如何进入复核（而非覆盖）

投影每次重放时以内容寻址方式（相关标识哈希）确定性地生成复核单：

* 两件不同器物使用同一业务编号（如两条 `M2-01`）→ `duplicate_code`，两条记录并存；
* 同一内部 ID 被再次登记 → `duplicate_identity`，原记录不动；
* 同发掘区内坐标完全重合 → `coordinate_conflict`，可由领队确认"原位并置"或更正；
* 地层层序撞号 → `stratum_order_conflict`；
* 器物找不到有效遗迹、遗迹地层跨区、样品无归属 → `broken_context`。

冲突随更正自动消除（状态记为"已消除"），或经 `ReviewResolved` 人工处置；
复核单永不删除，全部处置保留责任人与理由。

## 演示故事（`--seed-demo`）

外兆沟、内园墙、主陵、陪葬墓、祭祀坑 2017–2020 分期揭露 → 2019 离线平板补录
造成重号与坐标重合并进入复核 → 碳十四 + 类型学 + 铭文"高"支持**公子高说**，
2021 年简报发布并冻结 → 2023 年牙齿测年（前 205—前 195，与旧区间不相交）与
铭文新释读"嘉"支持竞争观点**宗室贵族嘉** → 2024 年项目负责人改用新说并发布
正式报告；2021 年简报的冻结快照保持原样，主陵与祭祀坑始终不在公开层出现。
