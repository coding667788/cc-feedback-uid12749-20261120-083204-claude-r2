"""领域层纯函数：凭据资格、按合格身份去重的覆盖判定、凭据边界计算。

本模块不做任何 SQL 写入，所有输入来自 :class:`solution.store.Snapshot`
加上显式的"假设变更"参数（待提交的迁移绑定、视为已退役的身份），
因此同一份逻辑既可用于事务内验证，也可被独立判定器（oracle）复用思路。

时间模型：全部使用整数时刻（合成场景里的逻辑时钟）。
凭据时窗为闭区间 ``[valid_from, valid_until]``——时刻恰好等于
``valid_until`` 仍然合格，下一个时刻起失效；这就是"恰好到期"边界。
"""

import json

from .errors import DomainError

OPEN_PLAN_STATUSES = ("PREPARED", "ACTIVE")


def _actions(identity_row):
    return set(json.loads(identity_row["actions"]))


def _min_for_actions(service_row):
    return json.loads(service_row["min_for_actions"]) or {}


def open_plan_for_identity(snapshot, identity_id):
    """返回该身份当前挂接的未终结计划（PREPARED/ACTIVE），没有则 None。"""
    for plan_id, idents in snapshot.plan_identities.items():
        if identity_id in idents and snapshot.plans[plan_id]["status"] in OPEN_PLAN_STATUSES:
            return snapshot.plans[plan_id]
    return None


def effective_until(snapshot, cred):
    """凭据在当前计划状态下的实际有效末端（含）。

    若凭据所属身份是某个未终结退役计划的候选，则旧凭据存续到计划
    deadline 为止：实际末端取 ``min(valid_until, deadline)``。
    计划取消后该限制自动消失（快照里不再是开放计划）。
    """
    until = cred["valid_until"]
    plan = open_plan_for_identity(snapshot, cred["identity_id"])
    if plan is not None:
        until = min(until, plan["deadline"])
    return until


def eligibility_reason(snapshot, cred, now):
    """返回 ``(是否合格, 原因码)``；合格时原因为 ``"OK"``。"""
    ident = snapshot.identities.get(cred["identity_id"])
    if ident is None:
        return False, "IDENTITY_MISSING"
    if ident["phase"] == "RETIRED":
        return False, "IDENTITY_RETIRED"
    if cred["generation"] != ident["generation"]:
        # 凭据代数与身份当前代数不一致：旧代数凭据不再合格。
        return False, "GENERATION_STALE"
    if cred["generation"] in snapshot.revoked.get(ident["identity_id"], ()):
        # 已撤销代数永不复活。
        return False, "GENERATION_REVOKED"
    if now < cred["valid_from"]:
        return False, "NOT_YET_VALID"
    if now > effective_until(snapshot, cred):
        # 超过时窗末端，或超过退役计划固定的旧凭据存续截止。
        return False, "WINDOW_CLOSED"
    return True, "OK"


def bound_services(snapshot, credential_id, pending_adds=None, pending_removals=None):
    """凭据当前绑定的全部服务。

    迁移语义为"从源服务移除、加入目标服务"，因此::

        当前绑定 = 静态绑定 ∪ 已迁入 - 已迁出 ∪ 本批假设迁入 - 本批假设迁出
    """
    result = set()
    for service_id, creds in snapshot.bindings.items():
        if credential_id in creds:
            result.add(service_id)
    result |= snapshot.extra_bindings.get(credential_id, set())
    result -= snapshot.removed_bindings.get(credential_id, set())
    if pending_adds:
        result |= pending_adds.get(credential_id, set())
    if pending_removals:
        result -= pending_removals.get(credential_id, set())
    return result


def evaluate_service(snapshot, service_id, now, pending_adds=None,
                     pending_removals=None, exclude_identities=None):
    """评估单个服务在给定时刻、给定假设变更下的覆盖。

    返回结构::

        {
          "service_id", "now", "min_seats", "seat_count", "seat_ok",
          "min_for_actions": {action: 需要数},
          "action_counts":   {action: 合格身份数},
          "action_violations": [{"action", "required", "actual"}, ...],
          "eligible_identities": [去重后的身份 id],
          "credential_evidence": [每张绑定凭据的资格判定明细],
          "satisfied": bool
        }

    覆盖计数按**合格身份去重**：同一身份的两张凭据只贡献一个席位。
    """
    exclude_identities = exclude_identities or set()
    service = snapshot.services[service_id]
    min_seats = service["min_seats"]
    min_for_actions = _min_for_actions(service)

    bound_cred_ids = set(snapshot.bindings.get(service_id, ()))
    # 已提交迁移：迁入本服务的凭据加入，迁出本服务的凭据移除。
    for cred_id, svcs in snapshot.extra_bindings.items():
        if service_id in svcs:
            bound_cred_ids.add(cred_id)
    for cred_id, svcs in snapshot.removed_bindings.items():
        if service_id in svcs:
            bound_cred_ids.discard(cred_id)
    # 本批假设变更（尚未提交的迁移批次）。
    if pending_adds:
        for cred_id, svcs in pending_adds.items():
            if service_id in svcs:
                bound_cred_ids.add(cred_id)
    if pending_removals:
        for cred_id, svcs in pending_removals.items():
            if service_id in svcs:
                bound_cred_ids.discard(cred_id)

    evidence = []
    eligible_identities = set()
    # 身份 -> 该身份在此服务上合格的授权动作并集（去重计数用）。
    eligible_identity_actions = {}
    for cred_id in sorted(bound_cred_ids):
        cred = snapshot.credentials[cred_id]
        ident_id = cred["identity_id"]
        if ident_id in exclude_identities:
            eligible, reason = False, "IDENTITY_RETIRED"
        else:
            eligible, reason = eligibility_reason(snapshot, cred, now)
        evidence.append(
            {
                "credential_id": cred_id,
                "identity_id": ident_id,
                "generation": cred["generation"],
                "eligible": eligible,
                "reason": reason,
                "valid_from": cred["valid_from"],
                "valid_until": cred["valid_until"],
                "effective_until": effective_until(snapshot, cred),
            }
        )
        if eligible and ident_id not in eligible_identities:
            eligible_identities.add(ident_id)
            ident = snapshot.identities[ident_id]
            eligible_identity_actions[ident_id] = _actions(ident)

    seat_count = len(eligible_identities)
    action_counts = {}
    action_violations = []
    for action, required in sorted(min_for_actions.items()):
        actual = sum(1 for acts in eligible_identity_actions.values() if action in acts)
        action_counts[action] = actual
        if actual < required:
            action_violations.append({"action": action, "required": required, "actual": actual})

    seat_ok = seat_count >= min_seats
    satisfied = seat_ok and not action_violations
    return {
        "service_id": service_id,
        "now": now,
        "min_seats": min_seats,
        "seat_count": seat_count,
        "seat_ok": seat_ok,
        "min_for_actions": min_for_actions,
        "action_counts": action_counts,
        "action_violations": action_violations,
        "eligible_identities": sorted(eligible_identities),
        "credential_evidence": evidence,
        "satisfied": satisfied,
    }


def evaluate_union(snapshot, service_ids, now, pending_adds=None,
                   pending_removals=None, exclude_identities=None):
    """整批覆盖检查：对服务并集逐一评估，返回每个服务的证据列表。"""
    return [
        evaluate_service(snapshot, sid, now, pending_adds, pending_removals, exclude_identities)
        for sid in sorted(set(service_ids))
    ]


def assert_coverage(snapshot, service_ids, now, pending_adds=None,
                    pending_removals=None, exclude_identities=None):
    """对服务并集做整批覆盖断言；不达标抛 :class:`DomainError`。

    只要存在席位缺口就报 ``COVERAGE_SEAT_GAP``；席位全部达标后才报
    ``COVERAGE_ACTION_GAP``（对应"数量达标但动作不足"样例）。
    """
    results = evaluate_union(snapshot, service_ids, now, pending_adds,
                             pending_removals, exclude_identities)
    seat_gaps = [r for r in results if not r["seat_ok"]]
    if seat_gaps:
        raise DomainError(
            "COVERAGE_SEAT_GAP",
            "合格身份去重席位数低于服务最低席位要求",
            {"services": [{"service_id": r["service_id"], "required": r["min_seats"],
                           "actual": r["seat_count"]} for r in seat_gaps]},
        )
    action_gaps = [r for r in results if r["action_violations"]]
    if action_gaps:
        raise DomainError(
            "COVERAGE_ACTION_GAP",
            "席位达标但授权动作的合格身份数不足",
            {"services": [{"service_id": r["service_id"], "violations": r["action_violations"]}
                          for r in action_gaps]},
        )
    return results


def next_credential_boundary(snapshot, service_id, now):
    """返回严格晚于 ``now`` 的下一个凭据状态变化时刻；没有则 ``None``。

    对**当前仍绑定**本服务的每张凭据，资格可能在以下时刻翻转：
    - ``valid_from``：进入时窗（变合格）；
    - ``effective_until + 1``：闭区间结束的下一时刻（变不合格）；
    - 代数轮换属于状态变更事件而非静态时窗边界，不出现在静态边界里。
    已通过迁移从本服务迁出的凭据不再参与。
    """
    bound_cred_ids = set(snapshot.bindings.get(service_id, ()))
    for cred_id, svcs in snapshot.extra_bindings.items():
        if service_id in svcs:
            bound_cred_ids.add(cred_id)
    for cred_id, svcs in snapshot.removed_bindings.items():
        if service_id in svcs:
            bound_cred_ids.discard(cred_id)

    candidates = []
    for cred_id in bound_cred_ids:
        cred = snapshot.credentials[cred_id]
        if cred["valid_from"] > now:
            candidates.append(cred["valid_from"])
        end_plus_one = effective_until(snapshot, cred) + 1
        if end_plus_one > now:
            candidates.append(end_plus_one)
    return min(candidates) if candidates else None


def coverage_evidence(snapshot, service_id, now):
    """读者接口：从 ``now`` 到下一个已知凭据边界为止的覆盖证据。"""
    if service_id not in snapshot.services:
        raise DomainError("UNKNOWN_SERVICE", f"服务不存在: {service_id}")
    evaluation = evaluate_service(snapshot, service_id, now)
    boundary = next_credential_boundary(snapshot, service_id, now)
    return {
        "service_id": service_id,
        "from_time": now,
        "next_boundary": boundary,  # None 表示到开放时界为止无已知翻转
        "stable_until": boundary,
        "coverage": {
            "satisfied": evaluation["satisfied"],
            "seat_count": evaluation["seat_count"],
            "min_seats": evaluation["min_seats"],
            "action_counts": evaluation["action_counts"],
            "min_for_actions": evaluation["min_for_actions"],
            "eligible_identities": evaluation["eligible_identities"],
        },
        "credential_evidence": evaluation["credential_evidence"],
    }
