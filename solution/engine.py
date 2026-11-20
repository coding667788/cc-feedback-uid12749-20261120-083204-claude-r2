"""协调器引擎：命令校验、事务内覆盖验证、回执与幂等。

写入类命令的事务骨架（满足"同一事务内以当前时间及状态验证全部受影响
服务的覆盖，并在同一事务保存变更与回执"）::

    BEGIN IMMEDIATE
      1. 幂等检查（request_id 在租户内唯一）
      2. 加载当前快照 Snapshot
      3. 纯函数域逻辑验证覆盖（含本批假设变更/退役投影）
      4. 写入业务变更 + 写入 commands 回执
      5. （测试同步点 wait_pre，可在提交前被真实终止 -> 整体回滚）
    COMMIT
      6. （测试同步点 signal_post，提交后终止 -> 变更已持久化）

业务失败（DomainError）时第 4 步只写 FAILED 回执、不写业务变更，
同样在一个事务里提交；它与 OK 回执、业务表状态三者可明确区分。
"""

import hashlib
import json

from . import domain, syncpoints
from .errors import DomainError
from .store import Snapshot

WRITER_TYPES = ("prepare", "migrate", "finalize", "cancel", "rotate_generation")


def canonical_hash(params):
    """规范化参数哈希：同参数必得同值，键序不影响结果。"""
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Engine:
    def __init__(self, conn):
        self.conn = conn

    # ------------------------------------------------------------------ #
    # 场景引导（合成初始数据）：独立事务，不挂命令回执，不参与幂等。
    # ------------------------------------------------------------------ #
    def bootstrap(self, payload):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for tenant in payload.get("tenants", []):
                conn.execute(
                    "INSERT OR REPLACE INTO tenants(tenant_id, name) VALUES (?, ?)",
                    (tenant["tenant_id"], tenant.get("name", tenant["tenant_id"])),
                )
            for svc in payload.get("services", []):
                if svc["tenant_id"] not in self._tenants():
                    raise DomainError("UNKNOWN_TENANT", f"租户不存在: {svc['tenant_id']}")
                conn.execute(
                    "INSERT OR REPLACE INTO services(tenant_id, service_id, min_seats, min_for_actions)"
                    " VALUES (?, ?, ?, ?)",
                    (svc["tenant_id"], svc["service_id"], int(svc["min_seats"]),
                     json.dumps(dict(sorted(svc.get("min_for_actions", {}).items())), ensure_ascii=False)),
                )
            for ident in payload.get("identities", []):
                if ident["tenant_id"] not in self._tenants():
                    raise DomainError("UNKNOWN_TENANT", f"租户不存在: {ident['tenant_id']}")
                actions = sorted(ident.get("actions", []))
                if not actions:
                    raise DomainError("BAD_REQUEST", f"身份 {ident['identity_id']} 至少需要一个授权动作")
                conn.execute(
                    "INSERT OR REPLACE INTO identities(identity_id, tenant_id, generation, actions, phase)"
                    " VALUES (?, ?, ?, ?, 'ACTIVE')",
                    (ident["identity_id"], ident["tenant_id"], int(ident["generation"]),
                     json.dumps(actions, ensure_ascii=False)),
                )
            for cred in payload.get("credentials", []):
                ident = self._ident(cred["identity_id"])
                if cred["generation"] < 1:
                    raise DomainError("BAD_REQUEST", "代数必须 >= 1")
                if int(cred["valid_until"]) < int(cred["valid_from"]):
                    raise DomainError("BAD_REQUEST", f"凭据 {cred['credential_id']} 时窗非法")
                conn.execute(
                    "INSERT OR REPLACE INTO credentials(credential_id, identity_id, generation,"
                    " valid_from, valid_until) VALUES (?, ?, ?, ?, ?)",
                    (cred["credential_id"], cred["identity_id"], int(cred["generation"]),
                     int(cred["valid_from"]), int(cred["valid_until"])),
                )
            for binding in payload.get("bindings", []):
                self._cred(binding["credential_id"])
                self._svc(binding["service_id"])
                conn.execute(
                    "INSERT OR IGNORE INTO bindings(credential_id, service_id) VALUES (?, ?)",
                    (binding["credential_id"], binding["service_id"]),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ #
    # 命令入口
    # ------------------------------------------------------------------ #
    def execute(self, command, now, sync_point=None):
        ctype = command.get("type")
        if ctype == "reader_coverage":
            return self._reader(command, now)
        if ctype not in WRITER_TYPES:
            raise DomainError("BAD_REQUEST", f"未知命令类型: {ctype}")
        return self._execute_writer(command, now, sync_point)

    # ------------------------------------------------------------------ #
    # 写入类命令的统一事务骨架
    # ------------------------------------------------------------------ #
    def _execute_writer(self, command, now, sync_point):
        conn = self.conn
        tenant_id = command.get("tenant_id")
        request_id = command.get("request_id")
        if not tenant_id or not request_id:
            raise DomainError("BAD_REQUEST", "写入命令必须携带 tenant_id 与 request_id")
        params = command.get("params", {})
        params_hash = canonical_hash(params)

        conn.execute("BEGIN IMMEDIATE")
        committed = False
        receipt_status = None
        # 事务已获取写锁后的同步点（用于并发测试固定"谁先拿到锁"）。
        if sync_point:
            syncpoints.wait_pre(sync_point + ":BEGINTX")
        try:
            # 1) 幂等锚点检查（在写锁内，并发重复请求在此串行化）。
            row = conn.execute(
                "SELECT command_type, params_hash, status, result FROM commands"
                " WHERE tenant_id = ? AND request_id = ?",
                (tenant_id, request_id),
            ).fetchone()
            if row is not None:
                if row["params_hash"] != params_hash or row["command_type"] != command["type"]:
                    # 异参冲突：commands 主键为 (tenant_id, request_id)，首次结果
                    # 行保持不变（不可覆盖）；本次请求以 FAILED 信封拒绝，业务表
                    # 不做任何修改。
                    conflict = DomainError(
                        "DUPLICATE_REQUEST",
                        f"租户 {tenant_id} 内 request_id={request_id} 已用于不同参数/类型的请求",
                        {"existing_type": row["command_type"], "incoming_type": command["type"]},
                    )
                    conn.execute("COMMIT")
                    committed = True
                    stored = {"status": "FAILED", "error": conflict.to_dict()}
                    return self._envelope(command, now, stored, replayed=False)
                # 同 ID 同参数：返回已持久化的第一次结果，不再次执行业务。
                conn.execute("COMMIT")
                committed = True
                stored = json.loads(row["result"])
                return self._envelope(command, now, stored, replayed=True)

            # 2) 业务执行；业务失败只落 FAILED 回执。
            try:
                data = self._dispatch(command["type"], command, params, now)
                stored = {"status": "OK", "data": data}
                receipt_status = "OK"
            except DomainError as exc:
                stored = {"status": "FAILED", "error": exc.to_dict()}
                receipt_status = "FAILED"

            conn.execute(
                "INSERT INTO commands(tenant_id, request_id, command_type, params_hash,"
                " status, result, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, request_id, command["type"], params_hash,
                 receipt_status, json.dumps(stored, ensure_ascii=False), now),
            )

            # 业务失败与成功都走到统一的回执提交点；先在此同步点阻塞，
            # 测试可在提交前终止进程（=> 整笔事务回滚，回执也不落盘）。
            if sync_point:
                syncpoints.wait_pre(sync_point + ":RECEIPT")
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if not committed:
                conn.execute("ROLLBACK")
            raise
        # 提交成功后再发信号；信号点在事务外，此处终止不影响已落盘变更。
        if sync_point:
            syncpoints.signal_post(sync_point + ":RECEIPT")
        return self._envelope(command, now, stored, replayed=False)

    @staticmethod
    def _envelope(command, now, stored, replayed):
        return {
            "type": command["type"],
            "tenant_id": command.get("tenant_id"),
            "request_id": command.get("request_id"),
            "at": now,
            "replayed": replayed,
            **stored,
        }

    # ------------------------------------------------------------------ #
    # 命令分派
    # ------------------------------------------------------------------ #
    def _dispatch(self, ctype, command, params, now):
        snapshot = Snapshot(self.conn)
        if ctype == "prepare":
            return self._cmd_prepare(snapshot, params, now)
        if ctype == "migrate":
            return self._cmd_migrate(snapshot, command, params, now)
        if ctype == "finalize":
            return self._cmd_finalize(snapshot, params, now)
        if ctype == "cancel":
            return self._cmd_cancel(snapshot, params, now)
        if ctype == "rotate_generation":
            return self._cmd_rotate(snapshot, params, now)
        raise DomainError("BAD_REQUEST", f"未知命令类型: {ctype}")

    # ---- prepare：固定候选身份与旧凭据存续截止 ------------------------ #
    def _cmd_prepare(self, snap, params, now):
        tenant_id = params["tenant_id"]
        plan_id = params["plan_id"]
        deadline = int(params["deadline"])
        candidates = params.get("identity_ids", [])
        if tenant_id not in snap.tenants:
            raise DomainError("UNKNOWN_TENANT", f"租户不存在: {tenant_id}")
        if not candidates:
            raise DomainError("EMPTY_CANDIDATES", "prepare 至少需要一个候选身份")
        if plan_id in snap.plans:
            raise DomainError("BAD_REQUEST", f"计划已存在: {plan_id}")
        for ident_id in candidates:
            ident = snap.identities.get(ident_id)
            if ident is None:
                raise DomainError("UNKNOWN_IDENTITY", f"身份不存在: {ident_id}")
            if ident["tenant_id"] != tenant_id:
                raise DomainError("TENANT_MISMATCH", f"身份 {ident_id} 不属于租户 {tenant_id}")
            if ident["phase"] == "RETIRED":
                raise DomainError("IDENTITY_BUSY", f"身份 {ident_id} 已退役")
            if domain.open_plan_for_identity(snap, ident_id) is not None:
                raise DomainError("IDENTITY_BUSY", f"身份 {ident_id} 已存在未终结的退役计划")
        conn = self.conn
        conn.execute(
            "INSERT INTO plans(plan_id, tenant_id, deadline, status, created_at)"
            " VALUES (?, ?, ?, 'PREPARED', ?)",
            (plan_id, tenant_id, deadline, now),
        )
        for ident_id in candidates:
            conn.execute(
                "INSERT INTO plan_identities(plan_id, identity_id) VALUES (?, ?)",
                (plan_id, ident_id),
            )
            conn.execute(
                "UPDATE identities SET phase = 'DECOMMISSIONING' WHERE identity_id = ?",
                (ident_id,),
            )
        return {
            "plan_id": plan_id,
            "deadline": deadline,
            "identity_ids": sorted(candidates),
            "note": "候选与旧凭据存续截止已固定；迁移/终结时再做事务内覆盖验证",
        }

    # ---- migrate：分批、同租户、事务内整批覆盖检查 -------------------- #
    def _cmd_migrate(self, snap, command, params, now):
        tenant_id = params["tenant_id"]
        plan_id = params["plan_id"]
        migration_id = params["migration_id"]
        moves = params.get("moves", [])
        plan = self._open_plan(snap, plan_id, tenant_id)
        if not moves:
            raise DomainError("EMPTY_BATCH", "迁移批次不能为空")

        pending_adds = {}      # credential_id -> {to_service_id}（本批假设迁入）
        pending_removals = {}  # credential_id -> {from_service_id}（本批假设迁出）
        affected = set()       # 变更前后受影响服务并集
        for move in moves:
            cred_id = move["credential_id"]
            from_sid = move["from_service_id"]
            to_sid = move["to_service_id"]
            if from_sid == to_sid:
                raise DomainError("BAD_REQUEST", f"迁移 {cred_id} 的源与目标服务相同")
            cred = self._cred(cred_id)
            ident = snap.identities[cred["identity_id"]]
            if ident["tenant_id"] != tenant_id:
                raise DomainError("MIGRATION_TENANT_VIOLATION",
                                  f"凭据 {cred_id} 属于租户 {ident['tenant_id']}，不能在租户 {tenant_id} 内迁移")
            self._same_tenant_service(snap, from_sid, tenant_id)
            self._same_tenant_service(snap, to_sid, tenant_id)
            # 源服务必须是该凭据当前（静态或已迁移）绑定的服务。
            if from_sid not in domain.bound_services(snap, cred_id):
                raise DomainError("UNKNOWN_SERVICE",
                                  f"凭据 {cred_id} 未绑定源服务 {from_sid}，无法迁移")
            pending_adds.setdefault(cred_id, set()).add(to_sid)
            pending_removals.setdefault(cred_id, set()).add(from_sid)
            affected.add(from_sid)
            affected.add(to_sid)

        # 事务内以当前时间与状态，对服务并集做整批覆盖验证。
        evidence = domain.assert_coverage(
            snap, affected, now,
            pending_adds=pending_adds, pending_removals=pending_removals,
        )

        conn = self.conn
        conn.execute(
            "INSERT INTO migrations(migration_id, plan_id, tenant_id, committed_at, request_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (migration_id, plan_id, tenant_id, now, command["request_id"]),
        )
        for move in moves:
            conn.execute(
                "INSERT INTO migration_moves(migration_id, credential_id, from_service_id, to_service_id)"
                " VALUES (?, ?, ?, ?)",
                (migration_id, move["credential_id"], move["from_service_id"], move["to_service_id"]),
            )
        if plan["status"] == "PREPARED":
            conn.execute("UPDATE plans SET status = 'ACTIVE' WHERE plan_id = ?", (plan_id,))
        return {
            "migration_id": migration_id,
            "plan_id": plan_id,
            "affected_services": sorted(affected),
            "evidence": evidence,
        }

    # ---- finalize：截止后、退役投影下重新验证并集，然后终结 ----------- #
    def _cmd_finalize(self, snap, params, now):
        tenant_id = params["tenant_id"]
        plan_id = params["plan_id"]
        plan = self._open_plan(snap, plan_id, tenant_id)
        if now < plan["deadline"]:
            raise DomainError(
                "DEADLINE_NOT_REACHED",
                f"当前时刻 {now} 早于旧凭据存续截止 {plan['deadline']}，不能终结",
                {"now": now, "deadline": plan["deadline"]},
            )
        candidates = set(snap.plan_identities[plan_id])
        affected = set()
        for ident_id in candidates:
            for cred_id, cred in snap.credentials.items():
                if cred["identity_id"] == ident_id:
                    affected |= domain.bound_services(snap, cred_id)
        if not affected:
            raise DomainError("UNKNOWN_SERVICE", "候选身份的凭据未绑定任何服务，无可验证对象")

        # 退役投影：候选身份视为已 RETIRED，其全部凭据不再贡献席位。
        evidence = domain.assert_coverage(
            snap, affected, now, exclude_identities=candidates
        )

        conn = self.conn
        conn.execute(
            "UPDATE plans SET status = 'FINALIZED', finalized_at = ? WHERE plan_id = ?",
            (now, plan_id),
        )
        for ident_id in candidates:
            conn.execute(
                "UPDATE identities SET phase = 'RETIRED', retired_at = ? WHERE identity_id = ?",
                (now, ident_id),
            )
        return {
            "plan_id": plan_id,
            "retired_identities": sorted(candidates),
            "affected_services": sorted(affected),
            "evidence": evidence,
        }

    # ---- cancel：不回滚已提交迁移，计划关闭、候选恢复 ACTIVE ---------- #
    def _cmd_cancel(self, snap, params, now):
        tenant_id = params["tenant_id"]
        plan_id = params["plan_id"]
        plan = self._open_plan(snap, plan_id, tenant_id)
        candidates = sorted(snap.plan_identities[plan_id])
        conn = self.conn
        conn.execute(
            "UPDATE plans SET status = 'CANCELLED', cancelled_at = ? WHERE plan_id = ?",
            (now, plan_id),
        )
        for ident_id in candidates:
            conn.execute(
                "UPDATE identities SET phase = 'ACTIVE' WHERE identity_id = ?",
                (ident_id,),
            )
        committed_migrations = conn.execute(
            "SELECT migration_id FROM migrations WHERE plan_id = ? ORDER BY committed_at",
            (plan_id,),
        ).fetchall()
        return {
            "plan_id": plan_id,
            "cancelled_identities": candidates,
            "retained_migrations": [r["migration_id"] for r in committed_migrations],
            "note": "已提交迁移不回滚；旧凭据存续截止限制随计划取消而解除",
        }

    # ---- rotate_generation：旧代数入撤销名单，永不复活 --------------- #
    def _cmd_rotate(self, snap, params, now):
        ident_id = params["identity_id"]
        new_generation = int(params["new_generation"])
        ident = snap.identities.get(ident_id)
        if ident is None:
            raise DomainError("UNKNOWN_IDENTITY", f"身份不存在: {ident_id}")
        if ident["tenant_id"] != params["tenant_id"]:
            raise DomainError("TENANT_MISMATCH", f"身份 {ident_id} 不属于该租户")
        if ident["phase"] == "RETIRED":
            raise DomainError("IDENTITY_BUSY", f"身份 {ident_id} 已退役，不能轮换代数")
        old_generation = ident["generation"]
        if new_generation <= old_generation:
            raise DomainError(
                "GENERATION_NOT_NEW",
                f"新代数 {new_generation} 必须严格大于当前代数 {old_generation}",
            )
        if new_generation in snap.revoked.get(ident_id, set()):
            raise DomainError("GENERATION_REVOKED", f"代数 {new_generation} 已撤销，永不复活")
        conn = self.conn
        conn.execute(
            "INSERT OR IGNORE INTO revoked_generations(identity_id, generation, revoked_at)"
            " VALUES (?, ?, ?)",
            (ident_id, old_generation, now),
        )
        conn.execute(
            "UPDATE identities SET generation = ? WHERE identity_id = ?",
            (new_generation, ident_id),
        )
        return {
            "identity_id": ident_id,
            "old_generation": old_generation,
            "new_generation": new_generation,
            "revoked_generations": sorted(snap.revoked.get(ident_id, set()) | {old_generation}),
        }

    # ---- reader_coverage：给定时刻到下一已知凭据边界的覆盖证据 ------- #
    def _reader(self, command, now):
        snap = Snapshot(self.conn)  # autocommit 下每条 SELECT 取一致读
        params = command.get("params", {})
        return self._envelope(
            command, now,
            {"status": "OK", "data": domain.coverage_evidence(snap, params["service_id"], now)},
            replayed=False,
        )

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    def _tenants(self):
        return {r["tenant_id"] for r in self.conn.execute("SELECT tenant_id FROM tenants")}

    def _ident(self, ident_id):
        row = self.conn.execute(
            "SELECT * FROM identities WHERE identity_id = ?", (ident_id,)
        ).fetchone()
        if row is None:
            raise DomainError("UNKNOWN_IDENTITY", f"身份不存在: {ident_id}")
        return row

    def _cred(self, cred_id):
        row = self.conn.execute(
            "SELECT * FROM credentials WHERE credential_id = ?", (cred_id,)
        ).fetchone()
        if row is None:
            raise DomainError("UNKNOWN_CREDENTIAL", f"凭据不存在: {cred_id}")
        return row

    def _svc(self, service_id):
        row = self.conn.execute(
            "SELECT * FROM services WHERE service_id = ?", (service_id,)
        ).fetchone()
        if row is None:
            raise DomainError("UNKNOWN_SERVICE", f"服务不存在: {service_id}")
        return row

    def _same_tenant_service(self, snap, service_id, tenant_id):
        svc = snap.services.get(service_id)
        if svc is None:
            raise DomainError("UNKNOWN_SERVICE", f"服务不存在: {service_id}")
        if svc["tenant_id"] != tenant_id:
            raise DomainError("MIGRATION_TENANT_VIOLATION",
                              f"服务 {service_id} 属于租户 {svc['tenant_id']}，迁移只限同租户")

    def _open_plan(self, snap, plan_id, tenant_id):
        plan = snap.plans.get(plan_id)
        if plan is None:
            raise DomainError("UNKNOWN_PLAN", f"退役计划不存在: {plan_id}")
        if plan["tenant_id"] != tenant_id:
            raise DomainError("TENANT_MISMATCH", f"计划 {plan_id} 不属于租户 {tenant_id}")
        if plan["status"] not in domain.OPEN_PLAN_STATUSES:
            raise DomainError(
                "PLAN_NOT_OPEN",
                f"计划 {plan_id} 当前状态 {plan['status']}，要求 PREPARED 或 ACTIVE",
            )
        return plan
