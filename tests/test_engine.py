"""引擎级测试：幂等语义、整批服务并集覆盖、取消保留迁移、截止门槛。"""

import unittest

from solution.engine import Engine
from solution.store import Snapshot, connect, initialize


def _bootstrap_migration():
    """两个服务 SA/SB 各有独立凭据，min_seats=1，另备一个备用服务 SH 收容迁出凭据。"""
    return {
        "tenants": [{"tenant_id": "T"}],
        "services": [
            {"service_id": "SA", "tenant_id": "T", "min_seats": 1, "min_for_actions": {}},
            {"service_id": "SB", "tenant_id": "T", "min_seats": 1, "min_for_actions": {}},
            {"service_id": "SH", "tenant_id": "T", "min_seats": 0, "min_for_actions": {}},
        ],
        "identities": [
            {"identity_id": "IA", "tenant_id": "T", "generation": 1, "actions": ["read"]},
            {"identity_id": "IB", "tenant_id": "T", "generation": 1, "actions": ["read"]},
            {"identity_id": "IH", "tenant_id": "T", "generation": 1, "actions": ["read"]},
        ],
        "credentials": [
            {"credential_id": "CA", "identity_id": "IA", "generation": 1,
             "valid_from": 0, "valid_until": 500},
            {"credential_id": "CB", "identity_id": "IB", "generation": 1,
             "valid_from": 0, "valid_until": 500},
            {"credential_id": "CH", "identity_id": "IH", "generation": 1,
             "valid_from": 0, "valid_until": 500},
        ],
        "bindings": [
            {"credential_id": "CA", "service_id": "SA"},
            {"credential_id": "CB", "service_id": "SB"},
            {"credential_id": "CH", "service_id": "SH"},
        ],
    }


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        initialize(self.conn)
        self.engine = Engine(self.conn)
        self.engine.bootstrap(_bootstrap_migration())

    def tearDown(self):
        self.conn.close()

    def _prepare(self, request_id, plan_id="P1", deadline=300):
        return self.engine.execute(
            {"type": "prepare", "tenant_id": "T", "request_id": request_id,
             "params": {"tenant_id": "T", "plan_id": plan_id, "deadline": deadline,
                        "identity_ids": ["IA"]}},
            now=100,
        )

    def test_same_id_same_params_returns_persisted_first_result(self):
        first = self._prepare("REQ-1")
        self.assertFalse(first["replayed"])
        second = self._prepare("REQ-1")
        self.assertTrue(second["replayed"])
        # 只落了一条命令回执。
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM commands WHERE request_id='REQ-1'"
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_same_id_different_params_rejected_and_first_result_untouched(self):
        first = self._prepare("REQ-2", plan_id="PX")
        self.assertEqual(first["status"], "OK")
        conflict = self._prepare("REQ-2", plan_id="PY")
        self.assertEqual(conflict["status"], "FAILED")
        self.assertEqual(conflict["error"]["code"], "DUPLICATE_REQUEST")
        # 原回执内容与类型未被覆盖。
        row = self.conn.execute(
            "SELECT * FROM commands WHERE tenant_id='T' AND request_id='REQ-2'"
        ).fetchone()
        self.assertEqual(row["command_type"], "prepare")
        self.assertEqual(row["status"], "OK")
        # 异参计划没有被创建。
        self.assertNotIn("PY", Snapshot(self.conn).plans)

    def test_request_id_unique_per_tenant_same_id_allowed_in_other_tenant(self):
        self.engine.bootstrap({
            "tenants": [{"tenant_id": "T2"}],
            "identities": [{"identity_id": "IX", "tenant_id": "T2", "generation": 1,
                            "actions": ["read"]}],
        })
        first = self._prepare("SHARED-ID", plan_id="P1")
        self.assertEqual(first["status"], "OK")
        other = self.engine.execute(
            {"type": "prepare", "tenant_id": "T2", "request_id": "SHARED-ID",
             "params": {"tenant_id": "T2", "plan_id": "P2", "deadline": 300,
                        "identity_ids": ["IX"]}},
            now=100,
        )
        self.assertEqual(other["status"], "OK")
        self.assertFalse(other["replayed"])

    def test_failure_receipt_is_also_idempotent(self):
        self._prepare("REQ-3", plan_id="P3", deadline=300)
        early = self.engine.execute(
            {"type": "finalize", "tenant_id": "T", "request_id": "REQ-F",
             "params": {"tenant_id": "T", "plan_id": "P3"}},
            now=200,
        )
        self.assertEqual(early["status"], "FAILED")
        self.assertEqual(early["error"]["code"], "DEADLINE_NOT_REACHED")
        replay = self.engine.execute(
            {"type": "finalize", "tenant_id": "T", "request_id": "REQ-F",
             "params": {"tenant_id": "T", "plan_id": "P3"}},
            now=250,
        )
        self.assertEqual(replay["status"], "FAILED")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["error"]["code"], "DEADLINE_NOT_REACHED")


class BatchUnionCoverageTest(unittest.TestCase):
    """整批检查对象必须是变更前后受影响服务的并集。"""

    def setUp(self):
        self.conn = connect(":memory:")
        initialize(self.conn)
        self.engine = Engine(self.conn)
        self.engine.bootstrap(_bootstrap_migration())
        self.engine.execute(
            {"type": "prepare", "tenant_id": "T", "request_id": "MP",
             "params": {"tenant_id": "T", "plan_id": "PM", "deadline": 400,
                        "identity_ids": ["IA"]}},
            now=100,
        )

    def tearDown(self):
        self.conn.close()

    def test_moving_only_credential_out_of_SA_fails_on_source_service(self):
        # SA 只有 CA 一张凭据（一个席位），把它迁去 SH：
        # 目标 SH 无最低席位要求，但源 SA 席位归零 -> 并集检查必须失败。
        env = self.engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "M1",
             "params": {"tenant_id": "T", "plan_id": "PM", "migration_id": "MG1",
                        "moves": [{"credential_id": "CA",
                                   "from_service_id": "SA",
                                   "to_service_id": "SH"}]}},
            now=150,
        )
        self.assertEqual(env["status"], "FAILED")
        self.assertEqual(env["error"]["code"], "COVERAGE_SEAT_GAP")
        # 失败批次不留迁移记录。
        snap = Snapshot(self.conn)
        self.assertNotIn("MG1", [r["migration_id"] for r in
                                 self.conn.execute("SELECT migration_id FROM migrations")])
        self.assertEqual(snap.extra_bindings.get("CA", set()), set())
        # 计划保持 PREPARED（未被失败批次激活）。
        self.assertEqual(snap.plans["PM"]["status"], "PREPARED")

    def test_successful_batch_records_evidence_and_activates_plan(self):
        # 先给 SA 补一个合格身份 IB2，使迁出 CA 后 SA 仍有 1 席。
        self.engine.bootstrap({
            "identities": [{"identity_id": "IA2", "tenant_id": "T", "generation": 1,
                            "actions": ["read"]}],
            "credentials": [{"credential_id": "CA2", "identity_id": "IA2",
                             "generation": 1, "valid_from": 0, "valid_until": 500}],
            "bindings": [{"credential_id": "CA2", "service_id": "SA"}],
        })
        env = self.engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "M2",
             "params": {"tenant_id": "T", "plan_id": "PM", "migration_id": "MG2",
                        "moves": [{"credential_id": "CA",
                                   "from_service_id": "SA",
                                   "to_service_id": "SH"}]}},
            now=160,
        )
        self.assertEqual(env["status"], "OK")
        self.assertEqual(env["data"]["affected_services"], ["SA", "SH"])
        # 证据覆盖并集中的每个服务。
        evidenced = {e["service_id"] for e in env["data"]["evidence"]}
        self.assertEqual(evidenced, {"SA", "SH"})
        snap = Snapshot(self.conn)
        self.assertEqual(snap.plans["PM"]["status"], "ACTIVE")
        self.assertIn("SH", snap.extra_bindings["CA"])

    def test_empty_batch_rejected(self):
        env = self.engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "M3",
             "params": {"tenant_id": "T", "plan_id": "PM", "migration_id": "MG3",
                        "moves": []}},
            now=170,
        )
        self.assertEqual(env["status"], "FAILED")
        self.assertEqual(env["error"]["code"], "EMPTY_BATCH")


class ChainedMigrationTest(unittest.TestCase):
    def test_credential_can_migrate_again_from_previous_destination(self):
        from solution.store import Snapshot

        conn = connect(":memory:")
        initialize(conn)
        engine = Engine(conn)
        engine.bootstrap(_bootstrap_migration())  # 含 SA/SB/SH，SH 上有 CH
        engine.bootstrap({
            "identities": [{"identity_id": "IA2", "tenant_id": "T", "generation": 1,
                            "actions": ["read"]}],
            "credentials": [{"credential_id": "CA2", "identity_id": "IA2",
                             "generation": 1, "valid_from": 0, "valid_until": 500}],
            "bindings": [{"credential_id": "CA2", "service_id": "SA"}],
        })
        engine.execute(
            {"type": "prepare", "tenant_id": "T", "request_id": "CP",
             "params": {"tenant_id": "T", "plan_id": "PC", "deadline": 400,
                        "identity_ids": ["IA"]}},
            now=100,
        )
        # 第一批：SA -> SH。
        first = engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "C1",
             "params": {"tenant_id": "T", "plan_id": "PC", "migration_id": "MC1",
                        "moves": [{"credential_id": "CA",
                                   "from_service_id": "SA", "to_service_id": "SH"}]}},
            now=150,
        )
        self.assertEqual(first["status"], "OK")
        # 第二批：从新位置 SH 继续迁到 SB（SH 的 min_seats=0，SB 亦无最低席位要求）。
        second = engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "C2",
             "params": {"tenant_id": "T", "plan_id": "PC", "migration_id": "MC2",
                        "moves": [{"credential_id": "CA",
                                   "from_service_id": "SH", "to_service_id": "SB"}]}},
            now=160,
        )
        self.assertEqual(second["status"], "OK")
        snap = Snapshot(conn)
        # 终态：CA 既不在 SA 也不在 SH，只在 SB。
        from solution.domain import bound_services
        self.assertEqual(bound_services(snap, "CA"), {"SB"})
        # 从旧位置再次迁移必须失败（源绑定已不存在）。
        stale = engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "C3",
             "params": {"tenant_id": "T", "plan_id": "PC", "migration_id": "MC3",
                        "moves": [{"credential_id": "CA",
                                   "from_service_id": "SA", "to_service_id": "SB"}]}},
            now=170,
        )
        self.assertEqual(stale["status"], "FAILED")
        self.assertEqual(stale["error"]["code"], "UNKNOWN_SERVICE")
        conn.close()


class CancelKeepsMigrationsTest(unittest.TestCase):
    def test_cancel_retains_committed_migration_and_reopens_identity(self):
        conn = connect(":memory:")
        initialize(conn)
        engine = Engine(conn)
        engine.bootstrap(_bootstrap_migration())
        # 给 SA 补席位后才能迁出 CA。
        engine.bootstrap({
            "identities": [{"identity_id": "IA2", "tenant_id": "T", "generation": 1,
                            "actions": ["read"]}],
            "credentials": [{"credential_id": "CA2", "identity_id": "IA2",
                             "generation": 1, "valid_from": 0, "valid_until": 500}],
            "bindings": [{"credential_id": "CA2", "service_id": "SA"}],
        })
        engine.execute(
            {"type": "prepare", "tenant_id": "T", "request_id": "CP",
             "params": {"tenant_id": "T", "plan_id": "PC", "deadline": 400,
                        "identity_ids": ["IA"]}},
            now=100,
        )
        engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "CM",
             "params": {"tenant_id": "T", "plan_id": "PC", "migration_id": "MCG",
                        "moves": [{"credential_id": "CA",
                                   "from_service_id": "SA",
                                   "to_service_id": "SH"}]}},
            now=150,
        )
        cancel = engine.execute(
            {"type": "cancel", "tenant_id": "T", "request_id": "CC",
             "params": {"tenant_id": "T", "plan_id": "PC"}},
            now=200,
        )
        self.assertEqual(cancel["data"]["retained_migrations"], ["MCG"])
        snap = Snapshot(conn)
        self.assertEqual(snap.plans["PC"]["status"], "CANCELLED")
        self.assertEqual(snap.identities["IA"]["phase"], "ACTIVE")
        # 迁移不回滚：CA 仍绑定 SH。
        self.assertIn("SH", snap.extra_bindings["CA"])
        # 取消后可以对同一身份重新 prepare。
        again = engine.execute(
            {"type": "prepare", "tenant_id": "T", "request_id": "CP2",
             "params": {"tenant_id": "T", "plan_id": "PC2", "deadline": 450,
                        "identity_ids": ["IA"]}},
            now=210,
        )
        self.assertEqual(again["status"], "OK")
        conn.close()


class ReaderBoundaryAfterMigrationTest(unittest.TestCase):
    def test_migrated_credential_drops_out_of_source_boundary_and_appears_at_target(self):
        from solution.domain import coverage_evidence
        from solution.store import Snapshot

        conn = connect(":memory:")
        initialize(conn)
        engine = Engine(conn)
        bootstrap = _bootstrap_migration()
        engine.bootstrap(bootstrap)
        # SA 需要留守席位后才能迁出 CA。
        engine.bootstrap({
            "identities": [{"identity_id": "IA2", "tenant_id": "T", "generation": 1,
                            "actions": ["read"]}],
            "credentials": [{"credential_id": "CA2", "identity_id": "IA2",
                             "generation": 1, "valid_from": 0, "valid_until": 900}],
            "bindings": [{"credential_id": "CA2", "service_id": "SA"}],
        })
        engine.execute(
            {"type": "prepare", "tenant_id": "T", "request_id": "BP",
             "params": {"tenant_id": "T", "plan_id": "PB", "deadline": 400,
                        "identity_ids": ["IA"]}},
            now=100,
        )
        # 迁移前：CA 被计划 deadline=400 截断（effective_until=400），
        # CA2 不受计划影响（until=900），SA 下一边界取较小者 401。
        before = coverage_evidence(Snapshot(conn), "SA", now=100)
        self.assertEqual(before["next_boundary"], 401)

        engine.execute(
            {"type": "migrate", "tenant_id": "T", "request_id": "BM",
             "params": {"tenant_id": "T", "plan_id": "PB", "migration_id": "MBG",
                        "moves": [{"credential_id": "CA",
                                   "from_service_id": "SA",
                                   "to_service_id": "SH"}]}},
            now=150,
        )
        # 迁移后：CA 已从 SA 移除，SA 只剩 CA2 -> 下一边界变为 901。
        after_source = coverage_evidence(Snapshot(conn), "SA", now=160)
        self.assertEqual(after_source["next_boundary"], 901)
        self.assertEqual(
            [e["credential_id"] for e in after_source["credential_evidence"]],
            ["CA2"],
        )
        # CA 出现在目标 SH 的证据里（SH 原本只有 CH，until=500）。
        after_target = coverage_evidence(Snapshot(conn), "SH", now=160)
        target_creds = {e["credential_id"] for e in after_target["credential_evidence"]}
        self.assertEqual(target_creds, {"CA", "CH"})
        conn.close()


class FinalizeDeadlineGateTest(unittest.TestCase):
    def test_finalize_before_deadline_rejected_without_state_change(self):
        conn = connect(":memory:")
        initialize(conn)
        engine = Engine(conn)
        engine.bootstrap(_bootstrap_migration())
        engine.execute(
            {"type": "prepare", "tenant_id": "T", "request_id": "FP",
             "params": {"tenant_id": "T", "plan_id": "PF", "deadline": 400,
                        "identity_ids": ["IH"]}},
            now=100,
        )
        env = engine.execute(
            {"type": "finalize", "tenant_id": "T", "request_id": "FF",
             "params": {"tenant_id": "T", "plan_id": "PF"}},
            now=399,
        )
        self.assertEqual(env["status"], "FAILED")
        self.assertEqual(env["error"]["code"], "DEADLINE_NOT_REACHED")
        snap = Snapshot(conn)
        self.assertEqual(snap.plans["PF"]["status"], "PREPARED")
        # 恰好在 deadline 时刻允许终结（SH 无最低席位要求，退役投影通过）。
        ok = engine.execute(
            {"type": "finalize", "tenant_id": "T", "request_id": "FF2",
             "params": {"tenant_id": "T", "plan_id": "PF"}},
            now=400,
        )
        self.assertEqual(ok["status"], "OK")
        self.assertEqual(Snapshot(conn).identities["IH"]["phase"], "RETIRED")
        conn.close()


if __name__ == "__main__":
    unittest.main()
