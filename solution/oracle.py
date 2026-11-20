"""独立判定器（oracle）：用朴素字典数据结构重新实现一遍业务规则。

本模块**不导入** solution.domain / solution.engine / solution.store，
只用 Python 内建容器和 json，从同一份 cases.json 自行维护状态并独立
预测每条命令的成败与错误码、服务覆盖与边界。测试用它来核对被测引擎
的输出——期望值来自这份独立判定，而不是从被测实现反推。

两边共享的只有场景输入（cases.json）和 errors.py 里的错误码字符串常量
（错误码属于题面约定的对外契约，不是实现逻辑）。
"""

import json


class OracleState:
    def __init__(self, bootstrap):
        self.tenants = {t["tenant_id"]: t for t in bootstrap.get("tenants", [])}
        self.identities = {i["identity_id"]: dict(i, phase="ACTIVE")
                           for i in bootstrap.get("identities", [])}
        self.services = {s["service_id"]: dict(s) for s in bootstrap.get("services", [])}
        self.credentials = {c["credential_id"]: dict(c)
                            for c in bootstrap.get("credentials", [])}
        # 静态绑定：credential_id -> set(service_id)
        self.static_binds = {}
        for b in bootstrap.get("bindings", []):
            self.static_binds.setdefault(b["credential_id"], set()).add(b["service_id"])
        # 已提交迁移：迁入集合与迁出集合分开记录（迁移=从源移除、加入目标）。
        self.migrated_adds = {}
        self.migrated_removals = {}
        self.revoked_gens = {i: set() for i in self.identities}
        # plan_id -> {"deadline","status","candidates":[...]}
        self.plans = {}
        self.plan_of_identity = {}
        # 幂等记忆：(tenant_id, request_id) -> (command_type, params 字典, 首次结果)
        self.command_log = {}

    # ---------- 独立实现的资格规则 ---------- #
    def _open_plan(self, identity_id):
        pid = self.plan_of_identity.get(identity_id)
        if pid is not None and self.plans[pid]["status"] in ("PREPARED", "ACTIVE"):
            return self.plans[pid]
        return None

    def _cred_ok(self, cred, now):
        """独立判定一张凭据在 now 是否合格，返回 (bool, reason)。"""
        ident = self.identities[cred["identity_id"]]
        if ident["phase"] == "RETIRED":
            return False, "IDENTITY_RETIRED"
        if cred["generation"] != ident["generation"]:
            return False, "GENERATION_STALE"
        if cred["generation"] in self.revoked_gens[ident["identity_id"]]:
            return False, "GENERATION_REVOKED"
        if now < cred["valid_from"]:
            return False, "NOT_YET_VALID"
        end = cred["valid_until"]
        plan = self._open_plan(cred["identity_id"])
        if plan is not None:
            end = min(end, plan["deadline"])
        if now > end:
            return False, "WINDOW_CLOSED"
        return True, "OK"

    def _bound(self, cred_id):
        return (set(self.static_binds.get(cred_id, set()))
                | set(self.migrated_adds.get(cred_id, set()))
                - set(self.migrated_removals.get(cred_id, set())))

    def _service_coverage(self, service_id, now, extra_adds=None,
                          extra_removals=None, excluded=()):
        """独立实现的按合格身份去重覆盖判定。"""
        creds = {cid for cid, svcs in self.static_binds.items() if service_id in svcs}
        creds |= {cid for cid, svcs in self.migrated_adds.items() if service_id in svcs}
        creds -= {cid for cid, svcs in self.migrated_removals.items() if service_id in svcs}
        if extra_adds:
            creds |= {cid for cid, svcs in extra_adds.items() if service_id in svcs}
        if extra_removals:
            creds -= {cid for cid, svcs in extra_removals.items() if service_id in svcs}
        eligible_ids, eligible_actions = set(), {}
        for cid in set(creds):
            cred = self.credentials[cid]
            iid = cred["identity_id"]
            if iid in excluded:
                continue
            ok, _ = self._cred_ok(cred, now)
            if ok and iid not in eligible_ids:
                eligible_ids.add(iid)
                eligible_actions[iid] = set(self.identities[iid]["actions"])
        svc = self.services[service_id]
        seat = len(eligible_ids)
        seat_ok = seat >= svc["min_seats"]
        violations = []
        for action, need in sorted(svc.get("min_for_actions", {}).items()):
            actual = sum(1 for acts in eligible_actions.values() if action in acts)
            if actual < need:
                violations.append((action, need, actual))
        return seat_ok, violations, sorted(eligible_ids)

    def _check_union(self, service_ids, now, extra_adds=None,
                     extra_removals=None, excluded=()):
        """整批检查服务并集，返回错误码或 None。席位优先于动作。"""
        seat_failures, action_failures = [], []
        for sid in sorted(set(service_ids)):
            seat_ok, violations, _ = self._service_coverage(
                sid, now, extra_adds, extra_removals, excluded
            )
            if not seat_ok:
                seat_failures.append(sid)
            elif violations:
                action_failures.append(sid)
        if seat_failures:
            return "COVERAGE_SEAT_GAP"
        if action_failures:
            return "COVERAGE_ACTION_GAP"
        return None

    # ---------- 命令预测：返回 (status, error_code|None, 状态是否变更) ---------- #
    def predict(self, command, now):
        ctype = command["type"]
        # 读者命令不参与 request_id 幂等。
        if ctype == "reader_coverage":
            return self._reader(command.get("params", {}), now)

        tenant_id = command.get("tenant_id")
        request_id = command.get("request_id")
        key = (tenant_id, request_id)
        params = command.get("params", {})
        if key in self.command_log:
            logged_type, logged_params, logged_result = self.command_log[key]
            if logged_type != ctype or logged_params != params:
                # 异参冲突：不覆盖首次结果，本次请求被拒绝。
                return "FAILED", "DUPLICATE_REQUEST"
            # 同 ID 同参数：回放首次持久化结果（含失败）。
            return logged_result

        result = self._dispatch(ctype, params, now)
        # 首次结果（成功或业务失败）记入幂等日志。
        self.command_log[key] = (ctype, dict(params), result)
        return result

    def _dispatch(self, ctype, params, now):
        handler = {
            "prepare": self._prep,
            "migrate": self._migrate,
            "finalize": self._finalize,
            "cancel": self._cancel,
            "rotate_generation": self._rotate,
        }[ctype]
        return handler(params, now)

    def _prep(self, p, now):
        if p["tenant_id"] not in self.tenants:
            return "FAILED", "UNKNOWN_TENANT"
        if not p.get("identity_ids"):
            return "FAILED", "EMPTY_CANDIDATES"
        if p["plan_id"] in self.plans:
            return "FAILED", "BAD_REQUEST"
        for iid in p["identity_ids"]:
            ident = self.identities.get(iid)
            if ident is None:
                return "FAILED", "UNKNOWN_IDENTITY"
            if ident["tenant_id"] != p["tenant_id"]:
                return "FAILED", "TENANT_MISMATCH"
            if ident["phase"] == "RETIRED" or self._open_plan(iid) is not None:
                return "FAILED", "IDENTITY_BUSY"
        # 预测成功 -> 应用状态
        self.plans[p["plan_id"]] = {
            "deadline": p["deadline"], "status": "PREPARED",
            "candidates": list(p["identity_ids"]),
        }
        for iid in p["identity_ids"]:
            self.identities[iid]["phase"] = "DECOMMISSIONING"
            self.plan_of_identity[iid] = p["plan_id"]
        return "OK", None

    def _migrate(self, p, now):
        plan = self.plans.get(p["plan_id"])
        if plan is None:
            return "FAILED", "UNKNOWN_PLAN"
        if plan["status"] not in ("PREPARED", "ACTIVE"):
            return "FAILED", "PLAN_NOT_OPEN"
        if not p.get("moves"):
            return "FAILED", "EMPTY_BATCH"
        extra_adds, extra_removals, affected = {}, {}, set()
        for mv in p["moves"]:
            cid, src, dst = mv["credential_id"], mv["from_service_id"], mv["to_service_id"]
            cred = self.credentials.get(cid)
            if cred is None:
                return "FAILED", "UNKNOWN_CREDENTIAL"
            if src == dst:
                return "FAILED", "BAD_REQUEST"
            if self.identities[cred["identity_id"]]["tenant_id"] != p["tenant_id"] \
                    or self.services[src]["tenant_id"] != p["tenant_id"] \
                    or self.services[dst]["tenant_id"] != p["tenant_id"]:
                return "FAILED", "MIGRATION_TENANT_VIOLATION"
            if src not in self._bound(cid):
                return "FAILED", "UNKNOWN_SERVICE"
            extra_adds.setdefault(cid, set()).add(dst)
            extra_removals.setdefault(cid, set()).add(src)
            affected.update((src, dst))
        code = self._check_union(
            affected, now, extra_adds=extra_adds, extra_removals=extra_removals
        )
        if code:
            return "FAILED", code
        # 成功 -> 记录迁入/迁出并激活计划（迁移是重新绑定，源服务不再保留）。
        for cid, svcs in extra_adds.items():
            self.migrated_adds.setdefault(cid, set()).update(svcs)
        for cid, svcs in extra_removals.items():
            self.migrated_removals.setdefault(cid, set()).update(svcs)
        plan["status"] = "ACTIVE"
        return "OK", None

    def _finalize(self, p, now):
        plan = self.plans.get(p["plan_id"])
        if plan is None:
            return "FAILED", "UNKNOWN_PLAN"
        if plan["status"] not in ("PREPARED", "ACTIVE"):
            return "FAILED", "PLAN_NOT_OPEN"
        if now < plan["deadline"]:
            return "FAILED", "DEADLINE_NOT_REACHED"
        candidates = set(plan["candidates"])
        affected = set()
        for cid, cred in self.credentials.items():
            if cred["identity_id"] in candidates:
                affected |= self._bound(cid)
        if not affected:
            return "FAILED", "UNKNOWN_SERVICE"
        code = self._check_union(affected, now, excluded=candidates)
        if code:
            return "FAILED", code
        plan["status"] = "FINALIZED"
        for iid in candidates:
            self.identities[iid]["phase"] = "RETIRED"
        return "OK", None

    def _cancel(self, p, now):
        plan = self.plans.get(p["plan_id"])
        if plan is None:
            return "FAILED", "UNKNOWN_PLAN"
        if plan["status"] not in ("PREPARED", "ACTIVE"):
            return "FAILED", "PLAN_NOT_OPEN"
        plan["status"] = "CANCELLED"
        for iid in plan["candidates"]:
            if self.identities[iid]["phase"] == "DECOMMISSIONING":
                self.identities[iid]["phase"] = "ACTIVE"
        # migrated_adds/migrated_removals 故意保留：取消不回滚已提交迁移。
        return "OK", None

    def _rotate(self, p, now):
        ident = self.identities.get(p["identity_id"])
        if ident is None:
            return "FAILED", "UNKNOWN_IDENTITY"
        if ident["tenant_id"] != p["tenant_id"]:
            return "FAILED", "TENANT_MISMATCH"
        if ident["phase"] == "RETIRED":
            return "FAILED", "IDENTITY_BUSY"
        new_gen = p["new_generation"]
        if new_gen <= ident["generation"]:
            return "FAILED", "GENERATION_NOT_NEW"
        if new_gen in self.revoked_gens[ident["identity_id"]]:
            return "FAILED", "GENERATION_REVOKED"
        self.revoked_gens[ident["identity_id"]].add(ident["generation"])
        ident["generation"] = new_gen
        return "OK", None

    def _reader(self, p, now):
        if p["service_id"] not in self.services:
            return "FAILED", "UNKNOWN_SERVICE"
        # 席位与边界的独立计算结果由 predicted_seats/predicted_boundary 提供。
        self.predicted_seats(p["service_id"], now)
        self.predicted_boundary(p["service_id"], now)
        return "OK", None

    def predicted_boundary(self, service_id, now):
        """供测试调用：独立计算下一边界。"""
        sid = service_id
        bounds = []
        involved = ({c for c, svcs in self.static_binds.items() if sid in svcs}
                    | {c for c, svcs in self.migrated_adds.items() if sid in svcs}) \
                   - {c for c, svcs in self.migrated_removals.items() if sid in svcs}
        for cid in involved:
            cred = self.credentials[cid]
            end = cred["valid_until"]
            plan = self._open_plan(cred["identity_id"])
            if plan is not None:
                end = min(end, plan["deadline"])
            if cred["valid_from"] > now:
                bounds.append(cred["valid_from"])
            if end + 1 > now:
                bounds.append(end + 1)
        return min(bounds) if bounds else None

    def predicted_seats(self, service_id, now):
        """供测试调用：独立计算合格身份去重席位与动作计数。"""
        seat_ok, violations, eligible = self._service_coverage(service_id, now)
        return {"eligible": eligible, "seat_ok": seat_ok, "violations": violations}


def replay_scenario(scenario):
    """独立回放整个场景，返回每条命令的 (status, error_code) 预测序列。"""
    state = OracleState(scenario.get("bootstrap", {}))
    predictions = []
    for step in scenario.get("commands", []):
        status, code = state.predict(step["command"], int(step["at"]))
        predictions.append({"step": step.get("name"), "status": status, "error_code": code})
    return predictions, state
