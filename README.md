# 离线服务身份退役协调器

在 Windows 原生 Python 3.12 上、仅用标准库与 CPU 实现的离线服务身份退役
协调器。服务绑定多条凭据并定义每类操作的最少可用身份数；身份具有租户、
代数、授权动作与退役阶段。系统用 SQLite 持久化全部状态与命令回执，
所有 `finalize` / 迁移批次都在**同一个事务内**以当前时间与状态验证全部
受影响服务（变更前后服务并集）的覆盖，并在同一事务保存变更与回执。

## 1. 运行方式

要求：Windows 原生 Python 3.12（实测 3.12.8 + SQLite 3.45.3），无第三方依赖。

```bash
# 场景回放：读 cases.json，写 result.json
python -m solution --input cases.json --output result.json

# 自动化测试（26 个用例，含 3 个独立进程真实终止用例）
python -m unittest discover -s tests -v
```

单步模式（供崩溃/并发测试的子进程使用，也可手工调用）：

```bash
python -m solution --db state.db --one-step step.json \
    --sync-point MIGRATE_BATCH --sync-dir <标记目录> --sync-token <令牌>
```

`step.json` 形如 `{"bootstrap": {...}, "command": {...}, "at": 100}`；
不带 `--sync-point` 时同步点退化为空操作。

## 2. 目录结构

```
solution/
  __init__.py
  __main__.py     CLI 入口（--input/--output、--one-step）
  errors.py       DomainError 与稳定错误码
  store.py        SQLite DDL、连接管理、只读快照 Snapshot
  domain.py       纯函数：凭据资格、去重覆盖、服务并集检查、边界计算
  engine.py       事务骨架、幂等、五个业务命令
  syncpoints.py   跨进程标记文件同步点（仅测试启用）
  runner.py       cases.json 顺序回放
  oracle.py       独立判定器：独立数据结构重写业务规则，不导入被测模块
tests/
  helpers.py      临时目录、子进程启动、重启视角只读查询
  test_cases.py   cases.json 手写 expect 断言 + oracle 交叉判定
  test_domain.py  去重席位、单退役/联合退役、闭区间、代数撤销等显式断言
  test_engine.py  幂等、整批并集、取消保留迁移、迁移链、边界、截止门槛
  test_concurrency.py  独立进程 + 真实 TerminateProcess 的崩溃与线性化测试
cases.json        8 个可重放场景（含题目点名的四类样例）
result.json       最近一次回放的完整回执
```

## 3. 输入结构（cases.json）

顶层 `{"schema_version": 1, "scenarios": [...]}`，每个场景：

```json
{
  "name": "场景名",
  "description": "人工说明",
  "bootstrap": {
    "tenants":     [{"tenant_id": "T1", "name": "tenant-1"}],
    "services":    [{"service_id": "S1", "tenant_id": "T1",
                     "min_seats": 2,
                     "min_for_actions": {"read": 2, "write": 1}}],
    "identities":  [{"identity_id": "I1", "tenant_id": "T1",
                     "generation": 1, "actions": ["read", "write"]}],
    "credentials": [{"credential_id": "C1", "identity_id": "I1",
                     "generation": 1,
                     "valid_from": 0, "valid_until": 500}],
    "bindings":    [{"credential_id": "C1", "service_id": "S1"}]
  },
  "commands": [
    {"name": "步骤名", "at": 100,
     "command": {"type": "prepare", "tenant_id": "T1",
                 "request_id": "req-1",
                 "params": {"tenant_id": "T1", "plan_id": "PA",
                            "deadline": 200,
                            "identity_ids": ["I1"]}},
     "expect": {"status": "OK", "replayed": false}}
  ]
}
```

时间全部是整数逻辑时钟；凭据时窗为**闭区间** `[valid_from, valid_until]`，
即时刻恰好等于 `valid_until` 仍合格，下一时刻起失效。

### 命令类型与 params

| type | params 关键字段 | 语义 |
|---|---|---|
| `prepare` | `plan_id, deadline, identity_ids[]` | 固定候选身份与旧凭据存续截止；身份进入 DECOMMISSIONING |
| `migrate` | `plan_id, migration_id, moves[]{credential_id, from_service_id, to_service_id}` | 分批迁移，仅限同租户；事务内对源/目标服务并集做覆盖检查 |
| `finalize` | `plan_id` | 要求 `now >= deadline`；在"候选身份视为已退役"投影下验证并集，通过后身份转 RETIRED、计划 FINALIZED |
| `cancel` | `plan_id` | 计划 CANCELLED、候选恢复 ACTIVE；**不回滚**已提交迁移 |
| `rotate_generation` | `identity_id, new_generation` | 旧代数进入撤销名单（永不复活），身份当前代数更新 |
| `reader_coverage` | `service_id` | 只读：返回 `now` 到下一已知凭据边界的覆盖证据 |

迁移的语义是"从源服务移除、加入目标服务"。同一凭据可跨批次沿
`SA → SH → SB` 链式迁移；从已离开的旧位置再次迁移会被拒绝。

## 4. 核心规则

1. **覆盖按合格身份去重计数**。同一身份的两张凭据只贡献一个席位；
   席位先满足 `min_seats`，再逐动作满足 `min_for_actions`。
2. **凭据资格**同时要求：身份未 RETIRED、凭据代数等于身份当前代数、
   该代数不在撤销名单、`now ∈ [valid_from, effective_until]`。
   `effective_until = min(valid_until, 计划 deadline)`（仅当身份挂在
   未终结计划上）；计划取消后截断自动解除。
3. **已撤销代数不会复活**：轮换只能到严格更大的代数；旧代数即使
   再发放同代数凭据也因代数不匹配/撤销名单而不合格。
4. **整批检查对象是服务并集**：迁移批次的 `from/to` 全部服务、
   finalize 中候选凭据当前绑定的全部服务，逐一验证，任一不达标整批失败。
5. **事务原子性**：`BEGIN IMMEDIATE` 后加载快照、验证、写业务表、
   写命令回执，最后单次 `COMMIT`；业务失败只写 FAILED 回执、不改业务表。
6. **幂等**：`(tenant_id, request_id)` 为主键。同 ID 同参数返回第一次
   持久化结果（`replayed=true`，失败也一样）；同 ID 异参/异类型返回
   `DUPLICATE_REQUEST`，首次结果行不可覆盖。request_id 仅在租户内唯一，
   不同租户可复用同一 ID。
7. **读者边界**：对当前仍绑定本服务的凭据，取严格晚于 `now` 的
   `min(valid_from 候选, effective_until+1 候选)`；没有则 `null`。

## 5. 错误码

| 错误码 | 触发条件 |
|---|---|
| `UNKNOWN_TENANT` / `UNKNOWN_IDENTITY` / `UNKNOWN_CREDENTIAL` / `UNKNOWN_SERVICE` / `UNKNOWN_PLAN` | 引用资源不存在 |
| `TENANT_MISMATCH` | 身份/计划不属于命令指定租户 |
| `MIGRATION_TENANT_VIOLATION` | 迁移涉及两个租户，或凭据/服务不在命令租户内 |
| `GENERATION_NOT_NEW` | 新代数不严格大于当前代数（含回退到已撤销代数） |
| `GENERATION_REVOKED` | 目标代数已在撤销名单中 |
| `IDENTITY_BUSY` | 身份已退役，或已有未终结（PREPARED/ACTIVE）计划 |
| `EMPTY_CANDIDATES` / `EMPTY_BATCH` | prepare 无候选 / 迁移批次为空 |
| `PLAN_NOT_OPEN` | 计划已 FINALIZED/CANCELLED，不接受迁移/finalize/cancel |
| `DEADLINE_NOT_REACHED` | finalize 时刻早于计划 deadline |
| `COVERAGE_SEAT_GAP` | 去重席位数低于某服务 `min_seats`（优先于动作缺口上报） |
| `COVERAGE_ACTION_GAP` | 席位达标但某动作合格身份数低于 `min_for_actions` |
| `DUPLICATE_REQUEST` | 租户内 request_id 已用于不同参数/类型的请求 |
| `BAD_REQUEST` | 结构非法、未知类型、源目标相同、时窗非法等 |

回执信封：成功为 `{"status":"OK","data":{...}}`；业务失败为
`{"status":"FAILED","error":{"code","message","details"}}`，
与业务表分表存储，可明确区分业务状态与回执。

## 6. 边界示例（cases.json 实测）

| 场景 | 关键边界 | 实测结果 |
|---|---|---|
| A `action_gap_seats_ok` | 退役投影后 2 席仍达标，但 write 合格身份仅 1 < 2 | `COVERAGE_ACTION_GAP` |
| B `joint_retirement_failure` | min_seats=2、3 身份；单退役任一身份剩 2 席可行，联合退役只剩 1 席 | 单退役 OK（单元测试显式断言），联合 finalize `COVERAGE_SEAT_GAP` |
| C `exact_expiry` | `now == valid_until == deadline` 仍合格；`now+1` 起失效；401 后另一凭据也到期 | 恰在 deadline 的 finalize 成功；201 仍 1 席达标，401 席位归零 |
| D `cancel_then_reprepare` | 首次 finalize 失败 → cancel → 重新 prepare → 分批迁入新凭据 → 再 finalize | 第二次 finalize OK，取消时回执列出保留的迁移 |
| E `idempotency` | 同 ID 同参回放、异参拒绝、失败回执回放 | `replayed=true` / `DUPLICATE_REQUEST` |
| F `cross_tenant_migration_rejected` | 跨租户移动 vs 同租户移动 | 前者 `MIGRATION_TENANT_VIOLATION`，后者 OK |
| G `generation_rotation...` | 轮换后旧凭据失格、回退被拒、gen2 新凭据恢复席位（恢复部分在单元测试） | 席位 2→1，`GENERATION_NOT_NEW` |
| H `reader_next_boundary` | 闭区间末端+1、未来时窗起点、无边界 | 边界依次 201→300→401→`null` |

## 7. 并发与真实终止测试

`solution/syncpoints.py` 用标记文件实现两段同步点（环境变量
`DECOMM_SYNC_DIR` 启用，不设置时为空操作）：

- `<point>:BEGINTX`：拿到 `BEGIN IMMEDIATE` 写锁之后；
- `<point>:RECEIPT`：回执写入之后、`COMMIT` 之前；提交后再写
  `<point>:RECEIPT.committed`。

`tests/test_concurrency.py` 的三个用例：

1. **提交前终止**：子进程停在 RECEIPT 时 `terminate()`（Windows
   TerminateProcess）→ 重启视角全新连接检查，迁移记录、迁移明细、
   命令回执全部不存在，计划仍 PREPARED，且数据库可立即写入（无残留锁）。
2. **提交后终止**：看到 committed 标记后再 `terminate()` → 业务变更
   与 OK 回执均完整保留，计划已 ACTIVE。
3. **固定先后的可线性化**：A、B 竞争同一写锁，B 的 `BEGIN IMMEDIATE`
   阻塞到 A 提交；A 成功把 CA 迁出 SA 后，B 因源绑定已消失而业务失败。
   终态只对应唯一串行历史 A;B（OK;FAILED），反向次序 B;A 会给出
   FAILED;OK，与实测终态不符。

测试只 `terminate()` 自己 `Popen` 启动并登记在案的子进程；tearDown
对每个子进程 `communicate()` 回收管道，不触碰任何其他 Python 进程。

## 8. 期望值的独立性

测试期望**不**从被测实现反推，来自两个独立来源：

1. `cases.json` 每条命令手写的 `expect`（状态、错误码、席位、动作计数、
   边界、受影响服务、退役身份等明确断言）；
2. `solution/oracle.py`：用与 `domain.py/engine.py` 完全独立的字典数据
   结构和独立判定流程重放全部命令，预测成败、错误码、席位与边界，
   与引擎回执逐项比对。oracle 不导入任何被测业务模块（仅共享错误码
   字符串这一对外契约）。

## 9. 实测结果

Windows 11 + Python 3.12.8（原生，非 WSL），本机离线执行：

```
$ python -m unittest discover -s tests -v
...
Ran 26 tests in ~1.0 s

OK
```

```
$ python -m solution --input cases.json --output result.json
# 生成 8 个场景、32 个步骤的完整回执；全部步骤与手写 expect、
# oracle 独立预测一致（test_cases.py 两个交叉判定用例覆盖）。
```

## 10. 设计取舍说明

- **prepare 不做覆盖验证**：题目要求 prepare 只"固定候选和旧凭据存续
  截止"，覆盖验证集中在迁移批次与 finalize（二者都在事务内以当前时间
  和状态重算），因此 prepare 后、迁移前服务可以暂时不满足终态要求。
- **FAILED 回执与业务状态分离**：业务失败在同一事务里只插入 commands
  行、不触碰业务表；`commands.status` 与计划/身份状态分属不同表，
  查询时可分别核对。
- **异参幂等冲突不新增回执行**：commands 主键即 `(tenant_id, request_id)`，
  首次结果天然不可覆盖；冲突请求以 FAILED 信封返回，业务表零修改。
- **同步点仅测试启用**：生产路径（无 `DECOMM_SYNC_DIR`）下所有
  wait/signal 都是空函数调用，零 IO 开销。
