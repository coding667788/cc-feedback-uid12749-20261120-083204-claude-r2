"""业务错误与稳定错误码。

所有业务失败都抛出 :class:`DomainError`，携带稳定字符串错误码，
CLI/runner 会把它转成 status=FAILED 的回执；回执与业务表分离存储。
"""


class DomainError(Exception):
    """可预期的业务失败（与程序崩溃/未提交中断严格区分）。"""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


# 错误码一览（README 中有同样的表，保持同步）：
# UNKNOWN_TENANT            租户不存在
# UNKNOWN_IDENTITY          身份不存在
# UNKNOWN_CREDENTIAL        凭据不存在
# UNKNOWN_SERVICE           服务不存在
# UNKNOWN_PLAN              退役计划不存在
# TENANT_MISMATCH           资源不属于命令指定的租户
# GENERATION_NOT_NEW        新代数必须严格大于身份当前代数（撤销代数不可复活）
# GENERATION_REVOKED        目标代数已在撤销名单中，永不复活
# IDENTITY_BUSY             身份已存在未终结（PREPARED/ACTIVE）的退役计划
# EMPTY_CANDIDATES          prepare 候选为空
# EMPTY_BATCH               迁移批次为空
# MIGRATION_TENANT_VIOLATION 迁移涉及两个不同租户，迁移只限同租户
# PLAN_NOT_OPEN             计划状态不允许该操作（已取消/已终结）
# DEADLINE_NOT_REACHED      finalize 时间早于旧凭据存续截止时刻
# COVERAGE_SEAT_GAP         合格身份去重席位数低于 min_seats
# COVERAGE_ACTION_GAP       席位数达标但某授权动作的合格身份数低于 min_for_action
# DUPLICATE_REQUEST         同租户同 request_id 但参数不同（幂等冲突）
# BAD_REQUEST               输入结构/枚举非法
