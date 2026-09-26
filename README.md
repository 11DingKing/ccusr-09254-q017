# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 导师工作量账本

在导师确认实习记录之上，服务按事件重放生成导师工作量账本：只有最终有效的确认计入（重复确认与已撤销确认自动剔除），委托关系按结算规则的份额基点（bps）在委托人与受托人之间拆分归属，跨日记录按培养方案时区切分到对应结算月份。`/api/plans/{plan_version}/workload/preview` 提供实时预览，`POST /api/plans/{plan_version}/workload-batches/{batch_id}` 幂等签发后形成不可变结算批次；批次签发后的纠错通过 `POST .../adjustments` 追加调整分录，不删除历史。`GET .../reconcile` 在重启后按事件截止点重算并核对批次一致性，`GET /api/plans/{plan_version}/mentors/{mentor_id}/workload` 输出个人明细（支持 `batch_id` 查询批次口径），结算规则经 `PUT/GET /api/plans/{plan_version}/settlement-rules` 配置。

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及工作量账本的并发确认、跨月时区归属、委托撤销、签发并发、调整分录与重启核对；运行过程中不需要单独的数据库或网络服务。
