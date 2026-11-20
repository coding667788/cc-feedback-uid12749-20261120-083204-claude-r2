"""跨进程同步点：用标记文件固定并发先后，仅测试通过环境变量启用。

环境变量：
- ``DECOMM_SYNC_DIR``   标记目录（不设置则同步点全部退化为空操作）；
- ``DECOMM_SYNC_TOKEN`` 本次进程的唯一标识，避免多进程标记互相干扰；
- ``DECOMM_SYNC_TIMEOUT`` 等待超时秒数，默认 60。

约定动作：
- ``wait_pre(point)``：先写 ``<point>.entered.<token>``，再阻塞等待
  ``<point>.proceed.<token>`` 出现。测试方在看到 entered 标记后，
  可以检查数据库状态、安排另一个进程的次序，或直接终止本进程
  （终止发生在 COMMIT 之前 -> 事务回滚）。
- ``signal_post(point)``：提交完成后写 ``<point>.committed.<token>``，
  供测试方确认提交已落盘（终止发生在此之后 -> 变更持久保留）。
"""

import os
import time


def _dir():
    return os.environ.get("DECOMM_SYNC_DIR")


def _token():
    return os.environ.get("DECOMM_SYNC_TOKEN", "single")


def _timeout():
    try:
        return float(os.environ.get("DECOMM_SYNC_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def _marker(name):
    return os.path.join(_dir(), name)


def _write(name):
    with open(_marker(name), "w", encoding="utf-8") as fh:
        fh.write(str(time.time_ns()))


def wait_pre(point):
    """事务内、COMMIT 之前的阻塞点。未启用同步目录时直接返回。"""
    directory = _dir()
    if not directory:
        return
    os.makedirs(directory, exist_ok=True)
    _write(f"{point}.entered.{_token()}")
    deadline = time.monotonic() + _timeout()
    proceed = _marker(f"{point}.proceed.{_token()}")
    while not os.path.exists(proceed):
        if time.monotonic() > deadline:
            raise TimeoutError(f"同步点 {point} 等待 proceed 超时")
        time.sleep(0.01)


def signal_post(point):
    """COMMIT 成功之后的通知点。"""
    if not _dir():
        return
    os.makedirs(_dir(), exist_ok=True)
    _write(f"{point}.committed.{_token()}")


def entered_marker(directory, point, token):
    return os.path.join(directory, f"{point}.entered.{token}")


def committed_marker(directory, point, token):
    return os.path.join(directory, f"{point}.committed.{token}")


def allow_proceed(directory, point, token):
    """测试辅助：放行某个阻塞在 wait_pre 上的进程。"""
    path = os.path.join(directory, f"{point}.proceed.{token}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("go")
