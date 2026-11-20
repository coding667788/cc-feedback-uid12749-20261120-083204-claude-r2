"""独立进程 + 真实终止的并发/崩溃测试。

本模块启动的全部子进程都登记在 ``self.spawned`` 中，tearDown 只终止
这些由本测试套件自己启动的进程，绝不触碰系统中的其他 Python 进程。

同步点（solution/syncpoints.py，标记文件实现），每个写事务有两段：
- ``<point>:BEGINTX``：拿到 BEGIN IMMEDIATE 写锁之后、业务处理之前；
- ``<point>:RECEIPT``：回执（成功或业务失败）写入后、COMMIT 之前；
- 提交完成后写 ``<point>:RECEIPT.committed``。

因此：
- 在 RECEIPT 等待处 terminate -> 事务回滚，重启后 DB 无该命令痕迹；
- 在 committed 标记之后 terminate -> 变更与回执均已持久化；
- 两个进程竞争同一写锁，用 BEGINTX 标记固定"谁先拿到锁"的先后，
  终态必须等价于该唯一串行次序（可线性化）。
"""

import os
import unittest

from solution import syncpoints
from tests.helpers import (
    close_pipes,
    decode_stream,
    new_temp_dir,
    read_db,
    spawn_step,
    wait_for_marker,
    write_json,
)

POINT = "MIGRATE_BATCH"


def _bootstrap():
    return {
        "tenants": [{"tenant_id": "T"}],
        "services": [
            {"service_id": "SA", "tenant_id": "T", "min_seats": 1, "min_for_actions": {}},
            {"service_id": "SB", "tenant_id": "T", "min_seats": 0, "min_for_actions": {}},
            {"service_id": "SC", "tenant_id": "T", "min_seats": 0, "min_for_actions": {}},
        ],
        "identities": [
            {"identity_id": "IA", "tenant_id": "T", "generation": 1, "actions": ["read"]},
            {"identity_id": "IA2", "tenant_id": "T", "generation": 1, "actions": ["read"]},
        ],
        "credentials": [
            {"credential_id": "CA", "identity_id": "IA", "generation": 1,
             "valid_from": 0, "valid_until": 500},
            {"credential_id": "CA2", "identity_id": "IA2", "generation": 1,
             "valid_from": 0, "valid_until": 500},
        ],
        "bindings": [
            {"credential_id": "CA", "service_id": "SA"},
            {"credential_id": "CA2", "service_id": "SA"},
        ],
    }


def _prepare_step(token):
    return {
        "bootstrap": _bootstrap(),
        "at": 100,
        "command": {
            "type": "prepare", "tenant_id": "T", "request_id": f"prep-{token}",
            "params": {"tenant_id": "T", "plan_id": "P", "deadline": 400,
                       "identity_ids": ["IA"]},
        },
    }


def _migrate_step(token, request_id, to_service):
    return {
        "at": 200,
        "command": {
            "type": "migrate", "tenant_id": "T", "request_id": request_id,
            "params": {
                "tenant_id": "T", "plan_id": "P", "migration_id": f"MG-{request_id}",
                "moves": [{"credential_id": "CA",
                           "from_service_id": "SA",
                           "to_service_id": to_service}],
            },
        },
    }


class ProcessLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = new_temp_dir()
        self.db = os.path.join(self.tmp, "state.db")
        self.sync_dir = os.path.join(self.tmp, "sync")
        os.makedirs(self.sync_dir, exist_ok=True)
        self.spawned = []  # 只登记本测试启动的子进程

    def tearDown(self):
        # 只清理本测试自己启动、且仍在运行的子进程，并回收管道。
        for proc in self.spawned:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                close_pipes(proc)

    def _spawn(self, token, step, point=POINT):
        step_path = os.path.join(self.tmp, f"step-{token}.json")
        write_json(step_path, step)
        proc = spawn_step(self.db, step_path, point, self.sync_dir, token)
        self.spawned.append(proc)
        return proc

    def _spawn_no_sync(self, token, step):
        """不带同步点的子进程（用于种子写入，避免无谓持锁）。"""
        step_path = os.path.join(self.tmp, f"step-{token}.json")
        write_json(step_path, step)
        proc = spawn_step(self.db, step_path, None, self.sync_dir, token)
        self.spawned.append(proc)
        return proc

    def _seed(self):
        """先跑一个带 bootstrap 的 prepare 子进程建立磁盘数据库。"""
        token = "seed"
        proc = self._spawn_no_sync(token, _prepare_step(token))
        proc.wait(timeout=15)
        self.assertEqual(proc.returncode, 0, decode_stream(proc.stderr.read()))

    def _begin_marker(self, token):
        return syncpoints.entered_marker(self.sync_dir, POINT + ":BEGINTX", token)

    def _receipt_marker(self, token):
        return syncpoints.entered_marker(self.sync_dir, POINT + ":RECEIPT", token)

    def _committed_marker(self, token):
        return syncpoints.committed_marker(self.sync_dir, POINT + ":RECEIPT", token)

    def _allow_begin(self, token):
        syncpoints.allow_proceed(self.sync_dir, POINT + ":BEGINTX", token)

    def _allow_receipt(self, token):
        syncpoints.allow_proceed(self.sync_dir, POINT + ":RECEIPT", token)

    def test_kill_before_commit_rolls_back_everything(self):
        self._seed()
        token = "precommit"
        proc = self._spawn(token, _migrate_step(token, "REQ-PRE", "SB"))
        # 依次通过两段同步点，最终停在 COMMIT 之前。
        wait_for_marker(self._begin_marker(token))
        self._allow_begin(token)
        wait_for_marker(self._receipt_marker(token))
        # 真实终止（Windows: TerminateProcess）。此时尚未 COMMIT。
        proc.terminate()
        proc.wait(timeout=15)
        self.assertNotEqual(proc.returncode, 0)

        # 重启视角：以全新连接检查磁盘数据库——迁移与命令回执都不应存在。
        migrations = read_db(self.db,
                             "SELECT * FROM migrations WHERE migration_id='MG-REQ-PRE'")
        self.assertEqual(migrations, [])
        moves = read_db(self.db, "SELECT * FROM migration_moves")
        self.assertEqual(moves, [])
        receipts = read_db(self.db,
                           "SELECT * FROM commands WHERE request_id='REQ-PRE'")
        self.assertEqual(receipts, [])
        # 计划仍停在 PREPARED，未被失败批次激活。
        plans = read_db(self.db, "SELECT status FROM plans WHERE plan_id='P'")
        self.assertEqual(plans[0]["status"], "PREPARED")

        # 重启后数据库必须完全可用：新连接能立即写入（无残留写锁/活动事务）。
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO plans(plan_id, tenant_id, deadline, status, created_at)"
                " VALUES ('P-RESTART', 'T', 400, 'PREPARED', 250)"
            )
            conn.commit()
            rows = conn.execute("SELECT plan_id FROM plans").fetchall()
        finally:
            conn.close()
        self.assertIn(("P-RESTART",), [tuple(r) for r in rows])

    def test_kill_after_commit_persists_changes_and_receipt(self):
        self._seed()
        token = "postcommit"
        proc = self._spawn(token, _migrate_step(token, "REQ-POST", "SB"))
        wait_for_marker(self._begin_marker(token))
        self._allow_begin(token)
        wait_for_marker(self._receipt_marker(token))
        self._allow_receipt(token)
        # 等 committed 标记：事务已经落盘。
        wait_for_marker(self._committed_marker(token))
        # 提交之后再真实终止进程。
        proc.terminate()
        proc.wait(timeout=15)

        # 重启视角：变更与回执都必须完整保留。
        migrations = read_db(self.db,
                             "SELECT * FROM migrations WHERE migration_id='MG-REQ-POST'")
        self.assertEqual(len(migrations), 1)
        moves = read_db(self.db,
                        "SELECT to_service_id FROM migration_moves WHERE migration_id='MG-REQ-POST'")
        self.assertEqual(moves[0]["to_service_id"], "SB")
        receipts = read_db(self.db,
                           "SELECT status, command_type FROM commands WHERE request_id='REQ-POST'")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "OK")
        self.assertEqual(receipts[0]["command_type"], "migrate")
        plans = read_db(self.db, "SELECT status FROM plans WHERE plan_id='P'")
        self.assertEqual(plans[0]["status"], "ACTIVE")

    def test_concurrent_fixed_order_is_linearizable(self):
        """两个迁移进程竞争写锁，同步点固定 A 先于 B，终态只能按 A;B 解释。

        A：CA 从 SA 迁到 SB（SA 仍有 CA2，1 席达标）-> 成功。
        B：同一张 CA 再从 SA 迁到 SC。B 的 BEGIN IMMEDIATE 阻塞到 A
        提交释放写锁后才拿到锁；此时 CA 已不在 SA -> 源绑定校验失败，
        得到业务失败回执。终态等价于唯一串行历史 A;B（OK;FAILED）；
        反向次序 B;A 会给出 FAILED;OK，与实际终态不符，故可线性化。
        """
        self._seed()
        token_a, token_b = "procA", "procB"
        proc_a = self._spawn(token_a, _migrate_step(token_a, "REQ-A", "SB"))
        proc_b = self._spawn(token_b, _migrate_step(token_b, "REQ-B", "SC"))

        # A 先拿到写锁并停在 BEGINTX；B 阻塞在 BEGIN IMMEDIATE。
        wait_for_marker(self._begin_marker(token_a))
        self._allow_begin(token_a)
        wait_for_marker(self._receipt_marker(token_a))
        # 放行 A 的提交并等待落盘，随后释放写锁，B 才能进入事务。
        self._allow_receipt(token_a)
        wait_for_marker(self._committed_marker(token_a))
        proc_a.wait(timeout=15)
        self.assertEqual(proc_a.returncode, 0)

        # B 现在拿到写锁，依次通过自己的两段同步点后完成。
        wait_for_marker(self._begin_marker(token_b))
        self._allow_begin(token_b)
        wait_for_marker(self._receipt_marker(token_b))
        self._allow_receipt(token_b)
        proc_b.wait(timeout=30)
        self.assertEqual(proc_b.returncode, 0)

        # A 必须成功。
        receipt_a = read_db(self.db,
                            "SELECT status FROM commands WHERE request_id='REQ-A'")
        self.assertEqual(len(receipt_a), 1)
        self.assertEqual(receipt_a[0]["status"], "OK")
        # B 必须失败，且失败回执已持久化、业务表无 B 的迁移。
        receipt_b = read_db(self.db,
                            "SELECT status, result FROM commands WHERE request_id='REQ-B'")
        self.assertEqual(len(receipt_b), 1)
        self.assertEqual(receipt_b[0]["status"], "FAILED")
        self.assertIn("UNKNOWN_SERVICE", receipt_b[0]["result"])
        mg_b = read_db(self.db, "SELECT * FROM migrations WHERE migration_id='MG-REQ-B'")
        self.assertEqual(mg_b, [])
        # 只有 A 的迁移落盘，终态对应唯一串行历史 A;B。
        all_mg = read_db(self.db, "SELECT migration_id FROM migrations ORDER BY migration_id")
        self.assertEqual([r["migration_id"] for r in all_mg], ["MG-REQ-A"])


if __name__ == "__main__":
    unittest.main()
