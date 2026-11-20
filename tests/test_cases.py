"""cases.json 回放测试：手写 expect 断言 + 独立 oracle 交叉判定。

期望值有两个相互独立的来源，二者都不读取被测实现的内部：
1. cases.json 中人工预先写定的 expect（明确断言）；
2. solution.oracle.OracleState —— 用独立的数据结构与判定逻辑
   重新预测每条命令的成败与错误码。
"""

import unittest

from solution.oracle import OracleState
from solution.runner import run_scenario
from tests.helpers import load_cases


def _get(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


_EXPECT_CHECKS = [
    ("status", "status"),
    ("error_code", "error.code"),
    ("replayed", "replayed"),
    ("seat_count", "data.coverage.seat_count"),
    ("satisfied", "data.coverage.satisfied"),
    ("eligible_identities", "data.coverage.eligible_identities"),
    ("action_counts", "data.coverage.action_counts"),
    ("next_boundary", "data.next_boundary"),
    ("affected_services", "data.affected_services"),
    ("retired_identities", "data.retired_identities"),
    ("cancelled_identities", "data.cancelled_identities"),
    ("retained_migrations", "data.retained_migrations"),
]


class ScenarioExpectTest(unittest.TestCase):
    """逐条比对 cases.json 中手写的 expect（明确断言来源）。"""

    def test_all_scenarios_match_handwritten_expectations(self):
        payload = load_cases()
        mismatches = []
        for scenario in payload["scenarios"]:
            report = run_scenario(scenario)
            self.assertEqual(len(report), len(scenario["commands"]),
                             f"{scenario['name']} 回执数与命令数不一致")
            for item, step in zip(report, scenario["commands"]):
                expect = step["expect"]
                if expect.get("status") == "SKIP":
                    continue
                result = item["result"]
                for expect_key, result_path in _EXPECT_CHECKS:
                    if expect_key in expect:
                        actual = _get(result, result_path)
                        if actual != expect[expect_key]:
                            mismatches.append(
                                f"{scenario['name']}/{step['name']}: {expect_key} "
                                f"实际={actual!r} 期望={expect[expect_key]!r}"
                            )
        self.assertEqual(mismatches, [], "\n".join(mismatches))


class OracleCrossCheckTest(unittest.TestCase):
    """独立 oracle 对每条命令的状态/错误码预测必须与引擎回执一致。"""

    def test_engine_matches_independent_oracle(self):
        payload = load_cases()
        disagreements = []
        for scenario in payload["scenarios"]:
            oracle = OracleState(scenario.get("bootstrap", {}))
            report = run_scenario(scenario)
            for item, step in zip(report, scenario["commands"]):
                command = step["command"]
                if command["type"] == "reader_coverage":
                    predicted_status, predicted_code = oracle.predict(command, int(step["at"]))
                else:
                    predicted_status, predicted_code = oracle.predict(command, int(step["at"]))
                result = item["result"]
                actual_status = result["status"]
                # REJECTED 是结构性拒绝（正常场景里不应出现）；FAILED 对应业务失败。
                if actual_status == "REJECTED":
                    disagreements.append(
                        f"{scenario['name']}/{step['name']}: 出现未预期的结构性拒绝 "
                        f"{result.get('error')}"
                    )
                    continue
                if actual_status != predicted_status:
                    disagreements.append(
                        f"{scenario['name']}/{step['name']}: 状态 实际={actual_status} "
                        f"oracle={predicted_status}"
                    )
                actual_code = _get(result, "error.code")
                if predicted_code != actual_code:
                    disagreements.append(
                        f"{scenario['name']}/{step['name']}: 错误码 实际={actual_code} "
                        f"oracle={predicted_code}"
                    )
        self.assertEqual(disagreements, [], "\n".join(disagreements))

    def test_reader_boundaries_match_oracle_computation(self):
        """读者边界与席位：oracle 独立计算值必须与引擎输出逐项相等。"""
        payload = load_cases()
        for scenario in payload["scenarios"]:
            oracle = OracleState(scenario.get("bootstrap", {}))
            report = run_scenario(scenario)
            for item, step in zip(report, scenario["commands"]):
                command = step["command"]
                at = int(step["at"])
                oracle.predict(command, at)  # 驱动 oracle 状态前进
                if command["type"] != "reader_coverage":
                    continue
                sid = command["params"]["service_id"]
                result = item["result"]
                data = result["data"]
                self.assertEqual(
                    data["next_boundary"], oracle.predicted_boundary(sid, at),
                    f"{scenario['name']}/{step['name']} 边界不一致",
                )
                seats = oracle.predicted_seats(sid, at)
                self.assertEqual(
                    data["coverage"]["eligible_identities"], seats["eligible"],
                    f"{scenario['name']}/{step['name']} 合格身份集合不一致",
                )
                self.assertEqual(
                    data["coverage"]["satisfied"],
                    seats["seat_ok"] and not seats["violations"],
                    f"{scenario['name']}/{step['name']} 满足判定不一致",
                )


if __name__ == "__main__":
    unittest.main()
