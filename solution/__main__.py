"""命令行入口：

    python -m solution --input cases.json --output result.json
    python -m solution --input cases.json            # 结果打印到 stdout

并发/崩溃测试还会通过子进程调用单步模式（见 tests/concurrent_process.py）：

    python -m solution --db state.db --one-step step.json --sync-point MIGRATE_BATCH
        --sync-dir <dir> --sync-token <token>

其中 step.json 为 {"bootstrap": {...}, "command": {...}, "at": 100}。
单步模式把数据库文件留在磁盘上，父进程在重启后直接检查该文件，
以此判断"未提交中断"与"已提交终止"的差别。
"""

import argparse
import json
import sys

from .engine import Engine
from .errors import DomainError
from .runner import dumps, run_file
from .store import connect, initialize


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _run_one_step(args):
    payload = _read_json(args.one_step)
    conn = connect(args.db)
    initialize(conn)
    engine = Engine(conn)
    if "bootstrap" in payload:
        engine.bootstrap(payload["bootstrap"])
    try:
        envelope = engine.execute(
            payload["command"], int(payload["at"]), sync_point=args.sync_point
        )
        output = {"ok": True, "result": envelope}
    except DomainError as exc:
        output = {"ok": False, "result": {"status": "REJECTED", "error": exc.to_dict()}}
    conn.close()
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线服务身份退役协调器")
    parser.add_argument("--input", help="cases.json 路径")
    parser.add_argument("--output", help="result.json 输出路径（缺省打印到 stdout）")
    parser.add_argument("--db", help="单步模式使用的 SQLite 文件路径")
    parser.add_argument("--one-step", help="单步模式：执行一个 step.json")
    parser.add_argument("--sync-point", help="单步模式：本命令的事务同步点名")
    parser.add_argument("--sync-dir", help="同步点标记目录")
    parser.add_argument("--sync-token", help="同步点令牌（区分测试子进程）")
    args = parser.parse_args(argv)

    if args.sync_dir:
        import os
        os.environ["DECOMM_SYNC_DIR"] = args.sync_dir
        if args.sync_token:
            os.environ["DECOMM_SYNC_TOKEN"] = args.sync_token

    if args.one_step:
        return _run_one_step(args)

    if not args.input:
        parser.error("需要 --input cases.json（或 --one-step step.json）")
    payload = _read_json(args.input)
    report = run_file(payload)
    text = dumps(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
