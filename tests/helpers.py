"""测试辅助：定位 cases.json、临时数据库目录、子进程引导。"""

import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_cases():
    with open(os.path.join(ROOT, "cases.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def scenario_by_name(name):
    for sc in load_cases()["scenarios"]:
        if sc["name"] == name:
            return sc
    raise KeyError(name)


def new_temp_dir():
    return tempfile.mkdtemp(prefix="decomm_test_")


def wait_for_marker(path, timeout=30):
    """轮询等待标记文件出现。"""
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待标记超时: {path}")
        time.sleep(0.01)


def spawn_step(db_path, step_path, sync_point, sync_dir, token):
    """启动一个真实子进程执行单步命令（只由本测试套件启动和终止）。

    使用 ``sys.executable -I`` 与绝对 -m 路径，cwd 设为工作区根，
    保证从干净环境导入本仓库的 solution 包。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # -I（隔离模式）会忽略 PYTHONPATH 且 cwd 不进 sys.path，因此用
    # -c 引导：先把工作区根显式插入 sys.path，再 runpy 运行包入口。
    # 仍保留 -I 对用户站点包、PYTHONSTARTUP 等的屏蔽，避免环境污染。
    launcher = (
        "import runpy, sys; "
        f"sys.path.insert(0, {ROOT!r}); "
        "runpy.run_module('solution', run_name='__main__', alter_sys=True)"
    )
    cmd = [
        sys.executable, "-I", "-c", launcher,
        "--db", db_path,
        "--one-step", step_path,
        "--sync-dir", sync_dir,
        "--sync-token", token,
    ]
    if sync_point is not None:
        cmd.extend(["--sync-point", sync_point])
    # 二进制管道：子进程在中文 Windows 上的异常文本可能带系统代码页
    # （GBK）字节，统一在需要时用 errors="replace" 解码，避免解码异常
    # 把测试本身弄 flaky。
    return subprocess.Popen(
        cmd, cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def close_pipes(proc):
    """关闭子进程管道，避免 ResourceWarning。"""
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def decode_stream(data):
    """容错解码子进程输出（UTF-8 优先，非法字节替换而非抛异常）。"""
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace")


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def read_db(db_path, query, params=()):
    """重启视角：用全新连接只读查询数据库（模拟进程重启后的状态检查）。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(query, params)]
    finally:
        conn.close()
    return rows
