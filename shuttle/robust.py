"""出车前的稳健排班与末班返程分析。

出车前答复预约时尚不知谁会临时到场，因此一份可签发的上下车安排必须
对资料中全部指定情景同时守得住：同一份 run 选择在任何情景组合下都
不破容、不违反锁定承诺；其余座位只能留作现场递补，随确认变化。
"""

from .solver import (evaluate_trip, reservation_map, _consume, _empty_pools,
                     _release)
from .timeutil import to_minutes


def _option_signature(option):
    return (tuple((ride.run, ride.seq_from, ride.seq_to)
                  for ride in option.outbound.rides),
            tuple((ride.run, ride.seq_from, ride.seq_to)
                  for ride in option.inbound.rides))


def _probe_across_scenarios(option, row, networks, scenario_pools):
    """尝试在各情景同时落位；无论成败都完整回滚，返回是否全部成功。"""
    succeeded = []
    for scenario_id, network in networks.items():
        pool = scenario_pools[scenario_id]
        ok_out, _ = _consume(pool, network, option.outbound,
                             row["wheelchair"])
        if not ok_out:
            break
        ok_in, _ = _consume(pool, network, option.inbound,
                            row["wheelchair"])
        if not ok_in:
            _release(pool, option.outbound, row["wheelchair"])
            break
        succeeded.append(scenario_id)
    feasible = len(succeeded) == len(networks)
    for scenario_id in succeeded:
        pool = scenario_pools[scenario_id]
        _release(pool, option.inbound, row["wheelchair"])
        _release(pool, option.outbound, row["wheelchair"])
    return feasible


def _robust_maximal(chosen, common, trip_order, source, networks,
                    scenario_pools):
    """全情景极大：不存在另一个未固定需求，能在每个情景都加入当前排班。"""
    for trip_id in trip_order:
        if trip_id in chosen:
            continue
        row = source.trip(trip_id)
        for _, option in common[trip_id]:
            if _probe_across_scenarios(option, row, networks,
                                       scenario_pools):
                return False
    return True


def robust_bundles(source, networks):
    """networks: {scenario: Network}（同一方案、按情景建网）。

    返回该方案可在出车前发出的全部稳健安排（固定留座部分）。
    """
    scenario_ids = [row["scenario"] for row in source.tables["scenarios"]]
    all_trips = sorted({trip_id for scenario in source.tables["scenarios"]
                        for trip_id in scenario["required"]})
    reservations = reservation_map(source)
    evaluations = {}
    for scenario_id in scenario_ids:
        network = networks[scenario_id]
        scenario_rows = next(row for row in source.tables["scenarios"]
                             if row["scenario"] == scenario_id)["required"]
        evaluations[scenario_id] = {
            trip_id: evaluate_trip(network, source.trip(trip_id))
            for trip_id in scenario_rows}

    # 每个需求在每个情景都要存在同一签名的往返选项，才可固定留座
    common = {}
    for trip_id in all_trips:
        option_sets = []
        for scenario_id in scenario_ids:
            if trip_id not in evaluations[scenario_id]:
                option_sets = None
                break
            option_sets.append({_option_signature(opt): opt
                                for opt in evaluations[scenario_id][trip_id].options})
        if not option_sets:
            continue
        shared = set(option_sets[0])
        for table in option_sets[1:]:
            shared &= set(table)
        if not shared:
            continue
        # 用任一情景下的选项对象承载，容量在各情景分别校验
        common[trip_id] = [(signature, option_sets[0][signature])
                           for signature in sorted(shared)]

    bundles = []
    trip_order = list(common)

    def can_extend(option, row, scenario_pools):
        """该选项在每个情景的占用下都能双程落位。"""
        for scenario_id in scenario_ids:
            network = networks[scenario_id]
            pool = scenario_pools[scenario_id]
            ok_out, _ = _consume(pool, network, option.outbound,
                                 row["wheelchair"])
            if not ok_out:
                return False
            ok_in, _ = _consume(pool, network, option.inbound,
                                row["wheelchair"])
            if not ok_in:
                _release(pool, option.outbound, row["wheelchair"])
                return False
        return True

    def rollback(option, row, scenario_pools):
        for scenario_id in scenario_ids:
            pool = scenario_pools[scenario_id]
            _release(pool, option.inbound, row["wheelchair"])
            _release(pool, option.outbound, row["wheelchair"])

    def walk(index, chosen, scenario_pools):
        if index == len(trip_order):
            if _robust_maximal(chosen, common, trip_order, source,
                               networks, scenario_pools):
                bundles.append(dict(chosen))
            return
        trip_id = trip_order[index]
        row = source.trip(trip_id)
        walk(index + 1, chosen, scenario_pools)
        for _, option in common[trip_id]:
            if not can_extend(option, row, scenario_pools):
                continue
            chosen[trip_id] = option
            walk(index + 1, chosen, scenario_pools)
            del chosen[trip_id]
            rollback(option, row, scenario_pools)

    walk(0, {}, {scenario_id: _empty_pools() for scenario_id in scenario_ids})

    # 锁定且已确认的承诺必须在每份稳健安排中，且使用其确认的 run
    mandatory = []
    for trip_id, reservation in reservations.items():
        row = source.trip(trip_id)
        if not row["locked"]:
            continue
        kept = []
        for bundle in bundles:
            option = bundle.get(trip_id)
            if option and [ride.run for ride in option.outbound.rides] == \
                    [reservation["outbound"]] and \
                    [ride.run for ride in option.inbound.rides] == \
                    [reservation["inbound"]]:
                kept.append(bundle)
        mandatory.append((trip_id, reservation))
        bundles = kept

    return {
        "bundles": bundles,
        "guaranteeable": sorted({trip_id for bundle in bundles
                                 for trip_id in bundle}),
        "fixed_seat_trips": trip_order,
        "mandatory": mandatory,
        "not_guaranteeable": [trip_id for trip_id in all_trips
                              if trip_id not in common],
    }


def last_return_audit(source, result):
    """逐社区检查每个需求最晚可用返程，识别丧失末班返程的社区。"""
    evaluations = result["evaluations"]
    lines = []
    for trip_id in result["trips"]:
        ev = evaluations[trip_id]
        row = source.trip(trip_id)
        if not ev.options:
            lines.append({"trip": trip_id, "community": row["community"],
                          "last_inbound_depart": None,
                          "return_window": row["return"],
                          "status": "无可用返程",
                          "reason": ev.blocked_reason})
            continue
        latest = max(ev.options,
                     key=lambda opt: to_minutes(opt.inbound.start_time))
        lines.append({
            "trip": trip_id, "community": row["community"],
            "last_inbound_depart": latest.inbound.start_time,
            "last_run": "+".join(ride.run for ride in latest.inbound.rides),
            "return_window": row["return"],
            "status": "有返程",
            "uses_alt": latest.uses_alt_window,
        })
    communities = sorted({item["community"] for item in lines})
    summary = {}
    for community in communities:
        items = [item for item in lines if item["community"] == community]
        summary[community] = {
            "lost_last_return": any(item["status"] == "无可用返程"
                                    for item in items),
            "trips": [item["trip"] for item in items
                      if item["status"] == "无可用返程"],
        }
    return {"per_trip": lines, "communities": summary}


def conditional_fill_ins(source, robust_result, solve_result):
    """固定留座之外、可随现场确认递补的需求。"""
    fixed = set(robust_result["guaranteeable"])
    fill = []
    for trip_id in solve_result["trips"]:
        if trip_id in fixed:
            continue
        ev = solve_result["evaluations"][trip_id]
        fill.append({
            "trip": trip_id,
            "community": source.trip(trip_id)["community"],
            "has_option_in_scenario": bool(ev.options),
            "reason": None if ev.options else ev.blocked_reason,
        })
    return fill
