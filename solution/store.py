"""SQLite 持久层：建表 DDL、连接管理与只读快照加载。

设计约定：
- 连接默认 autocommit（``isolation_level=None``），所有事务边界由引擎显式
  ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` 控制，确保"同一事务内
  验证覆盖、保存变更与回执"。
- 业务事实（身份/凭据/服务/计划/绑定）与命令回执（commands 表）分表存放，
  业务失败只写回执、不改业务表，二者在查询结果中可明确区分。
"""

import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

-- 身份：属于某个租户，有当前代数、授权动作集合、退役阶段。
-- phase: ACTIVE（正常）/ DECOMMISSIONING（已进入退役计划）/ RETIRED（已终结）
CREATE TABLE IF NOT EXISTS identities (
    identity_id     TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id),
    generation      INTEGER NOT NULL CHECK (generation >= 1),
    actions         TEXT NOT NULL,          -- JSON 数组，排序后存储
    phase           TEXT NOT NULL CHECK (phase IN ('ACTIVE','DECOMMISSIONING','RETIRED')),
    retired_at      INTEGER
);

-- 已撤销代数：进入此表的代数永不复活（即使再发同代数凭据也不合格）。
CREATE TABLE IF NOT EXISTS revoked_generations (
    identity_id     TEXT NOT NULL REFERENCES identities(identity_id),
    generation      INTEGER NOT NULL,
    revoked_at      INTEGER NOT NULL,
    PRIMARY KEY (identity_id, generation)
);

CREATE TABLE IF NOT EXISTS services (
    service_id      TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id),
    min_seats       INTEGER NOT NULL CHECK (min_seats >= 0),
    min_for_actions TEXT NOT NULL           -- JSON 对象 {action: 最少身份数}
);

CREATE TABLE IF NOT EXISTS credentials (
    credential_id   TEXT PRIMARY KEY,
    identity_id     TEXT NOT NULL REFERENCES identities(identity_id),
    generation      INTEGER NOT NULL,
    valid_from      INTEGER NOT NULL,       -- 含
    valid_until     INTEGER NOT NULL,       -- 含：时刻 == valid_until 仍合格
    CHECK (valid_until >= valid_from)
);

-- 凭据-服务绑定（多对多）。
CREATE TABLE IF NOT EXISTS bindings (
    credential_id   TEXT NOT NULL REFERENCES credentials(credential_id),
    service_id      TEXT NOT NULL REFERENCES services(service_id),
    PRIMARY KEY (credential_id, service_id)
);

-- 退役计划。
-- status: PREPARED（候选与截止已固定）/ ACTIVE（已有迁移提交，仍可继续迁移）
--         / FINALIZED（已终结，身份转 RETIRED）/ CANCELLED（已取消）
CREATE TABLE IF NOT EXISTS plans (
    plan_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id),
    deadline        INTEGER NOT NULL,       -- 旧凭据存续截止（含）
    status          TEXT NOT NULL CHECK (status IN ('PREPARED','ACTIVE','FINALIZED','CANCELLED')),
    created_at      INTEGER NOT NULL,
    finalized_at    INTEGER,
    cancelled_at    INTEGER
);

CREATE TABLE IF NOT EXISTS plan_identities (
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id),
    identity_id     TEXT NOT NULL REFERENCES identities(identity_id),
    PRIMARY KEY (plan_id, identity_id)
);

-- 已提交的迁移批次（取消不回滚这些记录）。
CREATE TABLE IF NOT EXISTS migrations (
    migration_id    TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id),
    tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id),
    committed_at    INTEGER NOT NULL,
    request_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_moves (
    migration_id    TEXT NOT NULL REFERENCES migrations(migration_id),
    credential_id   TEXT NOT NULL REFERENCES credentials(credential_id),
    from_service_id TEXT NOT NULL REFERENCES services(service_id),
    to_service_id   TEXT NOT NULL REFERENCES services(service_id),
    PRIMARY KEY (migration_id, credential_id, from_service_id, to_service_id)
);

-- 命令回执（幂等锚点）。业务成功/失败都在这里留痕，与业务状态分表。
CREATE TABLE IF NOT EXISTS commands (
    tenant_id       TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    command_type    TEXT NOT NULL,
    params_hash     TEXT NOT NULL,          -- 规范化参数的 SHA-256
    status          TEXT NOT NULL CHECK (status IN ('OK','FAILED')),
    result          TEXT NOT NULL,          -- JSON 结果体（含错误码或数据）
    committed_at    INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_credentials_identity ON credentials(identity_id);
CREATE INDEX IF NOT EXISTS idx_bindings_service ON bindings(service_id);
CREATE INDEX IF NOT EXISTS idx_plan_identities_identity ON plan_identities(identity_id);
"""


def connect(db_path):
    """打开数据库连接并启用外键；内存库用 ``:memory:``。"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.row_factory = sqlite3.Row
    return conn


def initialize(conn):
    """幂等建表。"""
    conn.executescript(SCHEMA)


class Snapshot:
    """某一时刻的不可变只读快照，供领域层纯函数计算覆盖。

    引擎在写事务内、应用任何候选变更之前加载一次快照，随后在同一份
    快照上叠加"假设变更"做覆盖验证，验证与写入共享同一事务的时间点。
    """

    def __init__(self, conn):
        self.tenants = {r["tenant_id"]: dict(r) for r in conn.execute("SELECT * FROM tenants")}
        self.identities = {r["identity_id"]: dict(r) for r in conn.execute("SELECT * FROM identities")}
        self.services = {r["service_id"]: dict(r) for r in conn.execute("SELECT * FROM services")}
        self.credentials = {r["credential_id"]: dict(r) for r in conn.execute("SELECT * FROM credentials")}
        self.bindings = {}  # service_id -> [credential_id, ...]
        for r in conn.execute("SELECT service_id, credential_id FROM bindings"):
            self.bindings.setdefault(r["service_id"], []).append(r["credential_id"])
        self.revoked = {}  # identity_id -> set(generation)
        for r in conn.execute("SELECT identity_id, generation FROM revoked_generations"):
            self.revoked.setdefault(r["identity_id"], set()).add(r["generation"])
        self.plans = {r["plan_id"]: dict(r) for r in conn.execute("SELECT * FROM plans")}
        self.plan_identities = {}  # plan_id -> [identity_id, ...]
        for r in conn.execute("SELECT plan_id, identity_id FROM plan_identities"):
            self.plan_identities.setdefault(r["plan_id"], []).append(r["identity_id"])
        # 已提交迁移按"从源移除、加入目标"重建当前绑定：
        # extra_bindings:  credential_id -> 迁入的服务集合
        # removed_bindings: credential_id -> 迁出的服务集合
        self.extra_bindings = {}
        self.removed_bindings = {}
        for r in conn.execute(
            """
            SELECT mm.credential_id, mm.from_service_id, mm.to_service_id
              FROM migration_moves mm JOIN migrations m ON m.migration_id = mm.migration_id
            """
        ):
            self.extra_bindings.setdefault(r["credential_id"], set()).add(r["to_service_id"])
            self.removed_bindings.setdefault(r["credential_id"], set()).add(r["from_service_id"])
