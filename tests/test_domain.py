"""领域规则的显式断言测试（期望值全部手写，不参考被测输出反推）。

覆盖：
- 同一身份两张凭据只贡献一个席位（去重）；
- B 场景：单身份退役可行、两身份联合退役失败；
- 闭区间恰好到期语义；
- 代数轮换后旧代数不合格、撤销代数永不复活、新代数凭据恢复席位；
- 计划 deadline 对旧凭据有效末端的截断与取消后恢复。
"""

import unittest

from solution.domain import (
    assert_coverage,
    effective_until,
    eligibility_reason,
    evaluate_service,
    next_credential_boundary,
)
from solution.engine import Engine
from solution.errors import DomainError
from solution.store import Snapshot, connect, initialize


def _engine(bootstrap):
    conn = connect(":memory:")
    initialize(conn)
    Engine(conn).bootstrap(bootstrap)
    return conn, Engine(conn)


TENANT = "T"


def _bootstrap_three_seats(min_seats=2):
    return {
        "tenants": [{"tenant_id": TENANT}],
        "services": [{"service_id": "S", "tenant_id": TENANT, "min_seats": min_seats,
                      "min_for_actions": {"read": 1}}],
        "identities": [
            {"identity_id": "I1", "tenant_id": TENANT, "generation": 1, "actions": ["read"]},
            {"identity_id": "I2", "tenant_id": TENANT, "generation": 1, "actions": ["read"]},
            {"identity_id": "I3", "tenant_id": TENANT, "generation": 1, "actions": ["read"]},
        ],
        "credentials": [
            {"credential_id": "C1", "identity_id": "I1", "generation": 1,
             "valid_from": 0, "valid_until": 500},
            {"credential_id": "C2", "identity_id": "I2", "generation": 1,
             "valid_from": 0, "valid_until": 500},
            {"credential_id": "C3", "identity_id": "I3", "generation": 1,
             "valid_from": 0, "valid_until": 500},
        ],
        "bindings": [
            {"credential_id": "C1", "service_id": "S"},
            {"credential_id": "C2", "service_id": "S"},
            {"credential_id": "C3", "service_id": "S"},
        ],
    }


class DedupSeatTest(unittest.TestCase):
    def test_two_credentials_same_identity_count_one_seat(self):
        bootstrap = {
            "tenants": [{"tenant_id": TENANT}],
            "services": [{"service_id": "S", "tenant_id": TENANT, "min_seats": 2,
                          "min_for_actions": {}}],
            "identities": [
                {"identity_id": "I1", "tenant_id": TENANT, "generation": 1,
                 "actions": ["read"]},
            ],
            "credentials": [
                {"credential_id": "C1", "identity_id": "I1", "generation": 1,
                 "valid_from": 0, "valid_until": 500},
                {"credential_id": "C1B", "identity_id": "I1", "generation": 1,
                 "valid_from": 0, "valid_until": 500},
            ],
            "bindings": [
                {"credential_id": "C1", "service_id": "S"},
                {"credential_id": "C1B", "service_id": "S"},
            ],
        }
        conn, _ = _engine(bootstrap)
        result = evaluate_service(Snapshot(conn), "S", now=100)
        # 两张凭据同一身份 -> 去重后只有 1 个席位，min_seats=2 不达标。
        self.assertEqual(result["seat_count"], 1)
        self.assertEqual(result["eligible_identities"], ["I1"])
        self.assertFalse(result["seat_ok"])
        self.assertFalse(result["satisfied"])
        with self.assertRaises(DomainError) as ctx:
            assert_coverage(Snapshot(conn), ["S"], now=100)
        self.assertEqual(ctx.exception.code, "COVERAGE_SEAT_GAP")
        conn.close()


class SingleVsJointRetirementTest(unittest.TestCase):
    """题目点名：两个身份各自退役可行，同时退役失败（min_seats=2, 三身份）。"""

    def setUp(self):
        self.conn, self.engine = _engine(_bootstrap_three_seats(min_seats=2))
        self.snap = Snapshot(self.conn)

    def tearDown(self):
        self.conn.close()

    def _retire(self, excluded):
        return evaluate_service(self.snap, "S", now=100, exclude_identities=set(excluded))

    def test_retire_either_identity_alone_still_satisfies(self):
        for alone in ("I1", "I2"):
            result = self._retire([alone])
            # 剩余两个身份，恰好达到 min_seats=2，动作 read 仍有 2 个。
            self.assertEqual(result["seat_count"], 2, f"单独退役 {alone} 后席位应为 2")
            self.assertTrue(result["satisfied"], f"单独退役 {alone} 应仍可行")

    def test_retire_both_jointly_fails_with_seat_gap(self):
        result = self._retire(["I1", "I2"])
        # 联合退役只剩 I3 一个席位 < 2。
        self.assertEqual(result["seat_count"], 1)
        self.assertFalse(result["seat_ok"])
        with self.assertRaises(DomainError) as ctx:
            assert_coverage(self.snap, ["S"], now=100,
                            exclude_identities={"I1", "I2"})
        self.assertEqual(ctx.exception.code, "COVERAGE_SEAT_GAP")

    def test_finalize_reflects_joint_failure_through_engine(self):
        # 端到端：prepare 两候选 -> finalize 必须失败且业务状态未被终结。
        self.engine.execute(
            {"type": "prepare", "tenant_id": TENANT, "request_id": "r1",
             "params": {"tenant_id": TENANT, "plan_id": "P", "deadline": 50,
                        "identity_ids": ["I1", "I2"]}},
            now=10,
        )
        envelope = self.engine.execute(
            {"type": "finalize", "tenant_id": TENANT, "request_id": "r2",
             "params": {"tenant_id": TENANT, "plan_id": "P"}},
            now=100,
        )
        self.assertEqual(envelope["status"], "FAILED")
        self.assertEqual(envelope["error"]["code"], "COVERAGE_SEAT_GAP")
        # 业务状态：计划仍未终结，身份仍处 DECOMMISSIONING。
        snap = Snapshot(self.conn)
        self.assertEqual(snap.plans["P"]["status"], "PREPARED")
        self.assertEqual(snap.identities["I1"]["phase"], "DECOMMISSIONING")
        self.assertEqual(snap.identities["I2"]["phase"], "DECOMMISSIONING")


class ClosedIntervalExpiryTest(unittest.TestCase):
    def test_credential_eligible_exactly_at_valid_until(self):
        bootstrap = _bootstrap_three_seats(min_seats=1)
        conn, _ = _engine(bootstrap)
        snap = Snapshot(conn)
        cred = snap.credentials["C1"]  # valid_until = 500
        ok, reason = eligibility_reason(snap, cred, now=500)
        self.assertTrue(ok, f"恰好到期时刻应合格，实际原因 {reason}")
        ok2, reason2 = eligibility_reason(snap, cred, now=501)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "WINDOW_CLOSED")
        # 边界：下一已知翻转时刻为 501。
        self.assertEqual(next_credential_boundary(snap, "S", now=499), 501)
        self.assertIsNone(next_credential_boundary(snap, "S", now=501))
        conn.close()

    def test_not_yet_valid_window(self):
        bootstrap = _bootstrap_three_seats(min_seats=1)
        conn, _ = _engine(bootstrap)
        snap = Snapshot(conn)
        cred = snap.credentials["C1"]
        cred["valid_from"] = 200  # 直接在快照上构造未来时窗场景
        ok, reason = eligibility_reason(snap, cred, now=199)
        self.assertFalse(ok)
        self.assertEqual(reason, "NOT_YET_VALID")
        ok2, _ = eligibility_reason(snap, cred, now=200)
        self.assertTrue(ok2)
        conn.close()


class GenerationRotationTest(unittest.TestCase):
    def setUp(self):
        self.bootstrap = {
            "tenants": [{"tenant_id": TENANT}],
            "services": [{"service_id": "S", "tenant_id": TENANT, "min_seats": 2,
                          "min_for_actions": {"read": 1}}],
            "identities": [
                {"identity_id": "I10", "tenant_id": TENANT, "generation": 1,
                 "actions": ["read"]},
                {"identity_id": "I11", "tenant_id": TENANT, "generation": 1,
                 "actions": ["read"]},
            ],
            "credentials": [
                {"credential_id": "C10", "identity_id": "I10", "generation": 1,
                 "valid_from": 0, "valid_until": 500},
                {"credential_id": "C11", "identity_id": "I11", "generation": 1,
                 "valid_from": 0, "valid_until": 500},
            ],
            "bindings": [
                {"credential_id": "C10", "service_id": "S"},
                {"credential_id": "C11", "service_id": "S"},
            ],
        }
        self.conn, self.engine = _engine(self.bootstrap)

    def tearDown(self):
        self.conn.close()

    def test_rotation_revokes_old_generation_and_new_credential_restores_seats(self):
        # 1) 轮换到 gen2：gen1 凭据立即不合格，席位从 2 降到 1。
        env = self.engine.execute(
            {"type": "rotate_generation", "tenant_id": TENANT, "request_id": "g1",
             "params": {"tenant_id": TENANT, "identity_id": "I10", "new_generation": 2}},
            now=100,
        )
        self.assertEqual(env["status"], "OK")
        snap = Snapshot(self.conn)
        self.assertEqual(snap.identities["I10"]["generation"], 2)
        self.assertIn(1, snap.revoked["I10"])
        result = evaluate_service(snap, "S", now=100)
        self.assertEqual(result["seat_count"], 1)
        self.assertEqual(result["eligible_identities"], ["I11"])

        # 2) 回退到已撤销的 gen1 被拒绝（GENERATION_NOT_NEW；即便数字存在也永不复活）。
        bad = self.engine.execute(
            {"type": "rotate_generation", "tenant_id": TENANT, "request_id": "g2",
             "params": {"tenant_id": TENANT, "identity_id": "I10", "new_generation": 1}},
            now=110,
        )
        self.assertEqual(bad["status"], "FAILED")
        self.assertEqual(bad["error"]["code"], "GENERATION_NOT_NEW")

        # 3) 发放 gen2 新凭据并绑定后席位恢复为 2；旧 gen1 凭据仍不合格。
        self.engine.bootstrap({
            "credentials": [
                {"credential_id": "C12", "identity_id": "I10", "generation": 2,
                 "valid_from": 0, "valid_until": 500},
            ],
            "bindings": [{"credential_id": "C12", "service_id": "S"}],
        })
        snap2 = Snapshot(self.conn)
        ok_old, reason_old = eligibility_reason(snap2, snap2.credentials["C10"], now=120)
        self.assertFalse(ok_old)
        self.assertEqual(reason_old, "GENERATION_STALE")
        ok_new, _ = eligibility_reason(snap2, snap2.credentials["C12"], now=120)
        self.assertTrue(ok_new)
        result2 = evaluate_service(snap2, "S", now=120)
        self.assertEqual(result2["seat_count"], 2)
        self.assertEqual(sorted(result2["eligible_identities"]), ["I10", "I11"])

        # 4) 直接尝试轮换到撤销名单里的代数（先升到 3 再试 2）由 GENERATION_NOT_NEW 拦截。
        self.engine.execute(
            {"type": "rotate_generation", "tenant_id": TENANT, "request_id": "g3",
             "params": {"tenant_id": TENANT, "identity_id": "I10", "new_generation": 3}},
            now=130,
        )
        snap3 = Snapshot(self.conn)
        self.assertIn(2, snap3.revoked["I10"])
        again = self.engine.execute(
            {"type": "rotate_generation", "tenant_id": TENANT, "request_id": "g4",
             "params": {"tenant_id": TENANT, "identity_id": "I10", "new_generation": 2}},
            now=140,
        )
        self.assertEqual(again["status"], "FAILED")
        self.assertEqual(again["error"]["code"], "GENERATION_NOT_NEW")


class DeadlineTruncationTest(unittest.TestCase):
    def test_plan_deadline_truncates_effective_until_and_cancel_restores(self):
        bootstrap = _bootstrap_three_seats(min_seats=1)
        conn, engine = _engine(bootstrap)
        # prepare I1，deadline=300；C1 原 valid_until=500。
        engine.execute(
            {"type": "prepare", "tenant_id": TENANT, "request_id": "d1",
             "params": {"tenant_id": TENANT, "plan_id": "P", "deadline": 300,
                        "identity_ids": ["I1"]}},
            now=10,
        )
        snap = Snapshot(conn)
        self.assertEqual(effective_until(snap, snap.credentials["C1"]), 300)
        # 301 时刻 C1 因 deadline 截断而失效。
        ok, reason = eligibility_reason(snap, snap.credentials["C1"], now=301)
        self.assertFalse(ok)
        self.assertEqual(reason, "WINDOW_CLOSED")
        # 取消计划后，截断解除，有效末端恢复为 500。
        engine.execute(
            {"type": "cancel", "tenant_id": TENANT, "request_id": "d2",
             "params": {"tenant_id": TENANT, "plan_id": "P"}},
            now=20,
        )
        snap2 = Snapshot(conn)
        self.assertEqual(effective_until(snap2, snap2.credentials["C1"]), 500)
        ok2, _ = eligibility_reason(snap2, snap2.credentials["C1"], now=301)
        self.assertTrue(ok2)
        conn.close()


class BusinessReceiptSeparationTest(unittest.TestCase):
    def test_failed_business_command_writes_receipt_but_keeps_business_state(self):
        bootstrap = _bootstrap_three_seats(min_seats=2)
        conn, engine = _engine(bootstrap)
        engine.execute(
            {"type": "prepare", "tenant_id": TENANT, "request_id": "p1",
             "params": {"tenant_id": TENANT, "plan_id": "P", "deadline": 50,
                        "identity_ids": ["I1", "I2"]}},
            now=10,
        )
        failed = engine.execute(
            {"type": "finalize", "tenant_id": TENANT, "request_id": "p2",
             "params": {"tenant_id": TENANT, "plan_id": "P"}},
            now=100,
        )
        self.assertEqual(failed["status"], "FAILED")
        # 回执表：存在 FAILED 行；业务表：计划未终结、身份未退役。
        rows = conn.execute(
            "SELECT status FROM commands WHERE tenant_id=? AND request_id='p2'", (TENANT,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "FAILED")
        snap = Snapshot(conn)
        self.assertEqual(snap.plans["P"]["status"], "PREPARED")
        self.assertIsNone(snap.identities["I1"]["retired_at"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
