"""容量收缩（车辆不可用）下的承诺审计与改约建议。

区分三类对象，不混为"可重排的空位"：
1. 已确认且新运力下仍可兑现的载运——继续留座；
2. 已确认但原接送已无法兑现——标记失效，历史公告不能批准其继续执行；
3. 未确认新时刻、或仅同意备选窗口但未登记载运的——只能提出改约建议，
   新窗口未获同意不得视为已接替，锁定原约也不能当作主动取消。
"""

from .model import build_network
from .solver import evaluate_trip, reservation_map, solve


def announced_plan(source):
    for event in source.tables["dispatch_log"]:
        if event["event"] == "公告" and event["plan"]:
            return event["plan"], event["at"]
    return None, None


def contraction_audit(source, plan=None, apply_outage=True):
    plan = plan or announced_plan(source)[0]
    nominal = build_network(source, plan, apply_outage=False)
    contracted = build_network(source, plan, apply_outage=apply_outage)
    reservations = reservation_map(source)

    # 1) 现有留座逐条核对
    held, invalid = [], []
    for trip_id, reservation in reservations.items():
        row = source.trip(trip_id)
        if reservation["outbound"] in contracted.blocked_runs or \
                reservation["inbound"] in contracted.blocked_runs:
            invalid.append({
                "record": reservation["record"], "trip": trip_id,
                "outbound": reservation["outbound"],
                "inbound": reservation["inbound"],
                "reason": "原接送班次落入车辆不可用窗口",
                "locked": row["locked"],
            })
            continue
        ev_nominal = evaluate_trip(nominal, row)
        ev = evaluate_trip(contracted, row)
        match_nominal = any(
            [ride.run for ride in opt.outbound.rides]
            == [reservation["outbound"]]
            and [ride.run for ride in opt.inbound.rides]
            == [reservation["inbound"]]
            for opt in ev_nominal.options)
        match = any(
            [ride.run for ride in opt.outbound.rides]
            == [reservation["outbound"]]
            and [ride.run for ride in opt.inbound.rides]
            == [reservation["inbound"]]
            for opt in ev.options)
        item = {"record": reservation["record"], "trip": trip_id,
                "outbound": reservation["outbound"],
                "inbound": reservation["inbound"],
                "locked": row["locked"]}
        if match:
            item["status"] = "继续留座"
            held.append(item)
        elif match_nominal:
            item["status"] = "失效安排"
            item["reason"] = contracted.blocked_runs.get(
                reservation["outbound"],
                contracted.blocked_runs.get(reservation["inbound"],
                                            "容量收缩后不可兑现"))
            invalid.append(item)
        else:
            item["status"] = "资料不一致"
            invalid.append(item)

    # 2) 收缩网络下逐情景重算可兑现能力
    scenario_results = {}
    for scenario in source.tables["scenarios"]:
        scenario_results[scenario["scenario"]] = solve(
            source, contracted, scenario["scenario"], reservations)

    # 3) 改约/暂缓建议：只使用资料明确同意的备选窗口
    suggestions = []
    for scenario_id, result in scenario_results.items():
        for trip_id in result["trips"]:
            row = source.trip(trip_id)
            ev = result["evaluations"][trip_id]
            entry = {"scenario": scenario_id, "trip": trip_id,
                     "community": row["community"],
                     "locked": row["locked"],
                     "alt_agreed": row["alt_arrival"] is not None}
            if trip_id in result["forced"]:
                entry["decision"] = "锁定承诺已保障"
                entry["detail"] = "占用已确认双程容量，不参与重排"
            elif any(trip_id == item["trip"] for item in invalid):
                entry["decision"] = "原接送失效，须重新安排"
                entry["detail"] = next(item["reason"] for item in invalid
                                       if item["trip"] == trip_id)
            elif ev.options:
                alt = [opt for opt in ev.options if opt.uses_alt_window]
                primary = [opt for opt in ev.options
                           if not opt.uses_alt_window]
                if primary:
                    entry["decision"] = "可兑现（原窗口）"
                    entry["detail"] = "现运力下仍有原窗口往返"
                elif alt and row["alt_arrival"]:
                    entry["decision"] = "建议改约（待同意）"
                    entry["detail"] = (f"仅备选窗口{row['alt_arrival']}"
                                       "可行；未获同意前不视为接替")
                else:
                    entry["decision"] = "告知暂缓"
                    entry["detail"] = "无合规往返，且无已同意备选窗口"
            else:
                entry["decision"] = "告知暂缓"
                entry["detail"] = ev.blocked_reason[1] \
                    if ev.blocked_reason else "运力挤占"
            suggestions.append(entry)

    # 4) 公告效力：公告方案中的受影响 run
    announced, announced_at = announced_plan(source)
    bulletin = None
    if announced == plan:
        bulletin = {
            "announced_at": announced_at, "plan": plan,
            "blocked_runs": dict(contracted.blocked_runs),
            "note": "公告仅为历史信息；受阻塞 run 不得据此继续派车",
        }

    return {
        "plan": plan,
        "held_seats": held,
        "invalid_arrangements": invalid,
        "scenario_results": scenario_results,
        "suggestions": suggestions,
        "bulletin": bulletin,
        "blocked_runs": dict(contracted.blocked_runs),
    }


def rebooking_lookup(audit_result, trip_id=None):
    """查询当前仍为谁留座，以及某需求的改约状态。"""
    rows = []
    for item in audit_result["held_seats"]:
        rows.append({"trip": item["trip"], "record": item["record"],
                     "status": "仍留座", "outbound": item["outbound"],
                     "inbound": item["inbound"]})
    for item in audit_result["invalid_arrangements"]:
        rows.append({"trip": item["trip"], "record": item["record"],
                     "status": "已失效/未接替",
                     "outbound": item["outbound"],
                     "inbound": item["inbound"]})
    if trip_id:
        rows = [row for row in rows if row["trip"] == trip_id]
        notes = [s for s in audit_result["suggestions"]
                 if s["trip"] == trip_id]
        return {"seats": rows, "suggestions": notes}
    return {"seats": rows}
