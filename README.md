# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 导师工作量结算账本

导师确认实习记录后，系统按“最终有效确认”派生导师工作量账本，并按月封存为不可变结算批次。所有原始事实仍是 append-only 事件流，账本与批次都通过确定性重放得到。

新增事件类型（沿用事件导入接口）：

- `mentor_delegate`：主带导师把确认权委托给受托导师，payload 含 `primary_mentor_id`、`delegate_mentor_id`；`revoked: true` 表示撤销授权，其委托确认随之作废并释放占用。
- `mentor_confirm`：payload 可带 `mentor_id`；委托确认带 `delegated: true`、`primary_mentor_id`，可用 `shares`（basis points，合计 10000）覆盖默认 60/40 拆分。
- `mentor_confirm_revoke`：相关导师撤销某条打卡的确认，旧确认留痕为失效并释放占用，随后的新确认可生效。

只有最终有效的确认计入：未知打卡、学生不符、委托未生效、份额非法、重复确认、确认撤销、委托撤销都会在 `invalid_confirmations` 中留痕。跨月打卡按培养方案时区切到各月分别拆账，秒数按份额精确切分（尾差补给排序后最后一位导师，合计不丢秒）。

接口（均位于 `/api/plans/{plan_version}`）：

- `PUT/GET settlement/rules`：配置/读取委托拆账份额（basis points）。
- `POST settlements/preview`：按 `period`（`YYYY-MM`）预览当月可结算分录、导师汇总、失效确认与已结算排除数。
- `POST settlements/{batch_id}`：签发（冻结）不可变批次，封存规则快照、事件截止点与 SHA-256 哈希链（`prev_hash` 指向上一批次）；重复签发幂等返回 200，并发签发以 `BEGIN IMMEDIATE` 串行化并由唯一约束兜底，冲突返回 409。
- `GET settlements` / `GET settlements/{batch_id}`：列出批次或读取批次（含分录、调整与 `hash_verified` 重启核对结果）。
- `POST settlements/{batch_id}/adjustments`：冻结后纠错只追加调整分录（`delta_seconds`、原因、操作人），历史原额与封存哈希不变；重复 `adjustment_id` 返回 409。
- `GET mentors/{mentor_id}/settlement`：导师个人明细，按批次汇总原额、调整、当前额与加权确认量。
