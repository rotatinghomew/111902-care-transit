"""方案服务能力求解。

一项预约是一个往返需求：去程与返程必须同时走通才算满足。容量按
(run, seq) 区段登记，轮椅位与普通座位分别记账、互不替代；同一 run
跨线或经多站只产生一份区段容量，不会在统计表里重复计算。

枚举该情景下全部可行排班，并只保留极大可行排班（再加任一需求都会
破容），作为会议上可对照的取舍，而不是只给一个最优解。
"""

from dataclasses import dataclass, field
from itertools import product

from .routing import CAPACITY, Journey, primary_reason, unreachable_trace


@dataclass
class RoundTripOption:
    trip: str
    outbound: Journey
    inbound: Journey

    @property
    def uses_alt_window(self):
        return self.outbound.uses_alt_window

    @property
    def segments(self):
        return self.outbound.segments + self.inbound.segments


@dataclass
class TripEvaluation:
    trip: str
    community: str
    wheelchair: bool
    locked: bool
    options: list                       # list[RoundTripOption]
    blocked_reason: object = None       # (code, detail) 或 None
    trace_detail: list = field(default_factory=list)


@dataclass
class Assignment:
    served: dict                        # trip -> RoundTripOption
    unserved: list                      # trip
    community_ratios: dict
    floor_pass: bool
    floor_detail: list
    unserved_reasons: dict              # trip -> (code, detail)
    delay_notes: dict                   # trip -> 说明

    @property
    def served_count(self):
        return len(self.served)


def _empty_pools():
    # (run, seq) -> {"standard": used, "accessible": used, "cap": leg}
    return {}


def _consume(pools, network, journey, wheelchair):
    """成功返回 (True, None)；破容时回滚本次占用并返回 (False, 区段)。"""
    touched = []
    for run_id, seq in journey.segments:
        leg = next(item for item in network.runs[run_id].legs
                   if item.seq == seq)
        slot = pools.setdefault((run_id, seq),
                                {"standard": 0, "accessible": 0, "leg": leg})
        kind = "accessible" if wheelchair else "standard"
        if slot[kind] + 1 > getattr(leg, kind):
            for prev_run, prev_seq in touched:
                prev_kind = "accessible" if wheelchair else "standard"
                pools[(prev_run, prev_seq)][prev_kind] -= 1
            return False, (run_id, seq, kind)
        slot[kind] += 1
        touched.append((run_id, seq))
    return True, None


def _release(pools, journey, wheelchair):
    for run_id, seq in journey.segments:
        kind = "accessible" if wheelchair else "standard"
        pools[(run_id, seq)][kind] -= 1


def evaluate_trip(network, trip_row):
    out_journeys, out_trace = unreachable_trace(network, trip_row, "out")
    in_journeys, in_trace = unreachable_trace(network, trip_row, "in")
    options = [RoundTripOption(trip=trip_row["trip"], outbound=out,
                               inbound=back)
               for out, back in product(out_journeys, in_journeys)]
    trace_detail = [(f"去程:{code}", detail)
                    for code, detail in out_trace.reasons]
    trace_detail += [(f"返程:{code}", detail)
                     for code, detail in in_trace.reasons]
    blocked = None
    if not options:
        if not out_journeys:
            blocked = (primary_reason(out_trace),
                       "去程无可行路径："
                       + "；".join(detail for _, detail in out_trace.reasons
                                   or [("x", "无可用班次或步行连接")]))
        else:
            blocked = (primary_reason(in_trace),
                       "返程无可行路径："
                       + "；".join(detail for _, detail in in_trace.reasons
                                   or [("x", "无可用班次或步行连接")]))
    return TripEvaluation(trip=trip_row["trip"],
                          community=trip_row["community"],
                          wheelchair=trip_row["wheelchair"],
                          locked=trip_row["locked"],
                          options=options, blocked_reason=blocked,
                          trace_detail=trace_detail)


def _option_matches_reservation(option, reservation):
    out_runs = [ride.run for ride in option.outbound.rides]
    in_runs = [ride.run for ride in option.inbound.rides]
    return (out_runs == [reservation["outbound"]]
            and in_runs == [reservation["inbound"]])


def _floor_check(source, served, trip_ids):
    by_community = {}
    for trip_id in trip_ids:
        row = source.trip(trip_id)
        by_community.setdefault(row["community"], []).append(trip_id)
    ratios, detail, all_pass = {}, [], True
    for community, members in sorted(by_community.items()):
        if not members:
            ratios[community] = None
            continue
        served_here = sum(1 for trip_id in members if trip_id in served)
        ratio = served_here / len(members)
        floor = source.floor(community)
        passed = ratio + 1e-9 >= floor
        all_pass = all_pass and passed
        ratios[community] = ratio
        detail.append({"community": community, "served": served_here,
                       "required": len(members), "ratio": ratio,
                       "floor": floor, "pass": passed})
    return ratios, detail, all_pass


def _capacity_block_detail(option, pools):
    """该选项在当前占用下哪些区段破容，用于解释挤占原因。"""
    hits = []
    for run_id, seq in option.segments:
        slot = pools.get((run_id, seq))
        if slot is None:
            continue
        leg = slot["leg"]
        kind = "accessible" if option.outbound.rides[0].wheelchair \
            else "standard"
        if slot[kind] >= getattr(leg, kind):
            hits.append(f"{run_id}#{seq}{'轮椅位' if kind == 'accessible' else '普通座'}已满")
    return hits


def solve(source, network, scenario_id, reservations=None):
    scenario = next(row for row in source.tables["scenarios"]
                    if row["scenario"] == scenario_id)
    trip_ids = list(scenario["required"])
    reservations = reservations or {}
    evaluations = {trip_id: evaluate_trip(network, source.trip(trip_id))
                   for trip_id in trip_ids}

    # 锁定承诺先占其已确认容量；结构上不可兑现则记录违约。
    forced = {}
    breaches = []
    for trip_id, ev in evaluations.items():
        row = source.trip(trip_id)
        if not row["locked"]:
            continue
        reservation = reservations.get(trip_id)
        if reservation is None:
            breaches.append((trip_id, "锁定需求缺少已确认双程载运记录"))
            continue
        match = [opt for opt in ev.options
                 if _option_matches_reservation(opt, reservation)]
        if not match:
            breaches.append((trip_id,
                             f"已确认载运{reservation['outbound']}/"
                             f"{reservation['inbound']}在方案"
                             f"{network.plan}不可兑现"))
            continue
        forced[trip_id] = match[0]

    pools = _empty_pools()
    for trip_id, option in forced.items():
        row = source.trip(trip_id)
        ok, conflict = _consume(pools, network, option.outbound,
                                row["wheelchair"])
        if not ok:
            breaches.append((trip_id, f"锁定去程与既有承诺冲突{conflict}"))
        ok, conflict = _consume(pools, network, option.inbound,
                                row["wheelchair"])
        if not ok:
            breaches.append((trip_id, f"锁定返程与既有承诺冲突{conflict}"))

    flexible = [trip_id for trip_id in trip_ids if trip_id not in forced]
    feasible = []

    def walk(index, served, work_pools):
        if index == len(flexible):
            feasible.append(dict(served))
            return
        trip_id = flexible[index]
        row = source.trip(trip_id)
        # 该需求也可以不排（被挤出/暂缓），先记录空位分支
        walk(index + 1, served, work_pools)
        for option in evaluations[trip_id].options:
            ok_out, _ = _consume(work_pools, network, option.outbound,
                                 row["wheelchair"])
            if not ok_out:
                continue
            ok_in, _ = _consume(work_pools, network, option.inbound,
                                row["wheelchair"])
            if not ok_in:
                _release(work_pools, option.outbound, row["wheelchair"])
                continue
            served[trip_id] = option
            walk(index + 1, served, work_pools)
            del served[trip_id]
            _release(work_pools, option.inbound, row["wheelchair"])
            _release(work_pools, option.outbound, row["wheelchair"])

    walk(0, dict(forced), pools)

    assignments = []
    for served in feasible:
        if not _is_maximal(served, evaluations, flexible, source, network):
            continue
        ratios, floor_detail, floor_pass = _floor_check(source, served,
                                                        trip_ids)
        unserved = [trip_id for trip_id in trip_ids if trip_id not in served]
        reasons, delays = {}, {}
        for trip_id in unserved:
            ev = evaluations[trip_id]
            if ev.blocked_reason:
                reasons[trip_id] = ev.blocked_reason
            else:
                hits = _capacity_block_detail_for(
                    ev, served, evaluations, source, network)
                reasons[trip_id] = (CAPACITY,
                                    "所有可行往返均被共享运力挤占："
                                    + ("；".join(hits) or "区段容量不足"))
        for trip_id, option in served.items():
            if option.uses_alt_window:
                row = source.trip(trip_id)
                delays[trip_id] = (f"改约备选到院窗口"
                                   f"{row['arrival']}→{row['alt_arrival']}")
            else:
                delays[trip_id] = "准点（原窗口）"
        assignments.append(Assignment(
            served=served, unserved=unserved, community_ratios=ratios,
            floor_pass=floor_pass, floor_detail=floor_detail,
            unserved_reasons=reasons, delay_notes=delays))

    # 稳定排序：底线通过优先，其次服务量，再按需求编号便于对账
    assignments.sort(key=lambda item: (not item.floor_pass,
                                       -item.served_count,
                                       tuple(item.unserved)))
    return {
        "plan": network.plan,
        "scenario": scenario_id,
        "trips": trip_ids,
        "evaluations": evaluations,
        "assignments": assignments,
        "forced": forced,
        "breaches": breaches,
        "network_conflicts": list(network.vehicle_conflicts),
        "blocked_runs": dict(network.blocked_runs),
    }


def _is_maximal(served, evaluations, flexible, source, network):
    """极大：在当前排班的实际占用下，任何未服务需求都无法再加入。

    有合规选项但被共享运力挤占，同样构成极大解；只有“还能再加一个”
    的排班才被剔除。
    """
    pools = _empty_pools()
    for trip_id, option in served.items():
        row = source.trip(trip_id)
        ok, _ = _consume(pools, network, option.outbound, row["wheelchair"])
        if not ok:
            return True
        ok, _ = _consume(pools, network, option.inbound, row["wheelchair"])
        if not ok:
            return True
    for trip_id in flexible:
        if trip_id in served:
            continue
        row = source.trip(trip_id)
        for option in evaluations[trip_id].options:
            ok_out, _ = _consume(pools, network, option.outbound,
                                 row["wheelchair"])
            if not ok_out:
                continue
            ok_in, _ = _consume(pools, network, option.inbound,
                                row["wheelchair"])
            if ok_in:
                _release(pools, option.inbound, row["wheelchair"])
                _release(pools, option.outbound, row["wheelchair"])
                return False
            _release(pools, option.outbound, row["wheelchair"])
    return True


def _capacity_block_detail_for(ev, served, evaluations, source, network):
    """重建当前排班占用，给出该需求各选项的破容区段。"""
    pools = _empty_pools()
    for other_id, option in served.items():
        row = source.trip(other_id)
        _consume(pools, network, option.outbound, row["wheelchair"])
        _consume(pools, network, option.inbound, row["wheelchair"])
    hits = []
    for option in ev.options:
        hits.extend(_capacity_block_detail(option, pools))
    return sorted(set(hits))


def reservation_map(source):
    return {row["trip"]: row for row in source.tables["reservations"]
            if row["state"] == "已确认"}
