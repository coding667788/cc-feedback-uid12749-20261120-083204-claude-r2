"""场景运行器：按 cases.json 的步骤顺序驱动引擎并收集结果。

每个命令都显式携带逻辑时刻 ``at``，引擎以该时刻作为"事务内当前时间"，
因此整个回放完全确定，与墙上时钟无关。
"""

import json

from .engine import Engine
from .errors import DomainError
from .store import connect, initialize


def run_scenario(scenario, db_path=":memory:"):
    """运行一个场景，返回每条命令的回执列表（含成功与业务失败）。"""
    conn = connect(db_path)
    initialize(conn)
    engine = Engine(conn)
    engine.bootstrap(scenario.get("bootstrap", {}))

    results = []
    for step in scenario.get("commands", []):
        now = int(step["at"])
        sync_point = step.get("sync_point")
        try:
            envelope = engine.execute(step["command"], now, sync_point=sync_point)
        except DomainError as exc:
            # 仅 BAD_REQUEST 等未落回执的结构性错误会走到这里；
            # 正常业务失败已在事务内转为 status=FAILED 回执。
            envelope = {
                "type": step["command"].get("type"),
                "tenant_id": step["command"].get("tenant_id"),
                "request_id": step["command"].get("request_id"),
                "at": now,
                "replayed": False,
                "status": "REJECTED",
                "error": exc.to_dict(),
            }
        results.append({"step": step.get("name"), "at": now, "result": envelope})
    conn.close()
    return results


def run_file(payload, db_path=":memory:"):
    """运行整个 cases.json 负载（单场景或多场景集合）。"""
    if "scenarios" in payload:
        return {
            "scenarios": [
                {"name": sc.get("name"), "results": run_scenario(sc, db_path)}
                for sc in payload["scenarios"]
            ]
        }
    return {"name": payload.get("name"), "results": run_scenario(payload, db_path)}


def dumps(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
