"""资料装载与运力网络建模。

一个 run 是同一车辆连续开行的同一趟，由若干 seq 相邻的 leg 组成；
容量按 leg（区段）分别登记，乘客占用一趟 run 时占用其经过的每个
区段。同车 run 若时间跨度重合则不能同批使用，建模时直接暴露冲突。
"""

from dataclasses import dataclass, field

from .timeutil import overlap, to_minutes


PLANS = ("BASE", "VIA", "JOIN")
PLAN_NAMES = {"BASE": "维持旧线", "VIA": "临时绕行", "JOIN": "共享接驳"}


@dataclass(frozen=True)
class Leg:
    plan: str
    run: str
    seq: int
    origin: str
    dest: str
    depart: str
    arrive: str
    vehicle: str
    standard: int
    accessible: int

    @property
    def depart_min(self):
        return to_minutes(self.depart)

    @property
    def arrive_min(self):
        return to_minutes(self.arrive)


@dataclass
class Run:
    run: str
    plan: str
    vehicle: str
    legs: list  # list[Leg]，按 seq 排序

    @property
    def start_min(self):
        return min(leg.depart_min for leg in self.legs)

    @property
    def end_min(self):
        return max(leg.arrive_min for leg in self.legs)

    def leg_after(self, seq):
        following = [leg for leg in self.legs if leg.seq == seq + 1]
        return following[0] if following else None


@dataclass
class Network:
    plan: str
    runs: dict = field(default_factory=dict)          # run -> Run
    walks: dict = field(default_factory=dict)         # (from,to) -> minutes
    blocked_runs: dict = field(default_factory=dict)  # run -> 原因
    vehicle_conflicts: list = field(default_factory=list)

    def legs_from(self, node):
        return [leg for run in self.runs.values() for leg in run.legs
                if leg.origin == node]


@dataclass
class Source:
    raw: dict
    tables: dict
    vehicle_windows: dict          # vehicle -> [(start_min, end_min)] 开放窗口
    outage_windows: dict           # vehicle -> [(start_min, end_min)] 不可用窗口

    def trip(self, trip_id):
        for row in self.tables["trips"]:
            if row["trip"] == trip_id:
                return row
        raise KeyError(trip_id)

    def floor(self, community):
        for row in self.tables["community_floors"]:
            if row["community"] == community:
                return row["floor"]
        raise KeyError(community)


def _windows(rows, vehicle_field, start_field, end_field):
    windows = {}
    for row in rows:
        windows.setdefault(row[vehicle_field], []).append(
            (to_minutes(row[start_field]), to_minutes(row[end_field]))
        )
    return windows


def load_source(raw):
    tables = raw["tables"]
    vehicle_windows = _windows(tables["vehicles"], "vehicle", "from", "to")
    outage_windows = {}
    for event in tables["dispatch_log"]:
        if event["event"] == "不可用" and event["vehicle"]:
            outage_windows.setdefault(event["vehicle"], []).append(
                (to_minutes(event["from"]), to_minutes(event["to"]))
            )
    return Source(raw=raw, tables=tables,
                  vehicle_windows=vehicle_windows,
                  outage_windows=outage_windows)


def _run_blocked_reason(run, source):
    """返回 run 不可用的原因；可用时返回 None。"""
    for leg in run.legs:
        for start, end in source.outage_windows.get(run.vehicle, []):
            if overlap(leg.depart_min, leg.arrive_min, start, end):
                return f"车辆{run.vehicle}不可用窗口{_span(start, end)}"
        for start, end in source.vehicle_windows.get(run.vehicle, []):
            if leg.depart_min < start or leg.arrive_min > end:
                return f"车辆{run.vehicle}开放窗口外"
    return None


def _span(start, end):
    from .timeutil import hhmm
    return f"{hhmm(start)}-{hhmm(end)}"


def build_network(source, plan, apply_outage=False):
    legs = [Leg(plan=row["plan"], run=row["run"], seq=row["seq"],
                origin=row["from"], dest=row["to"], depart=row["depart"],
                arrive=row["arrive"], vehicle=row["vehicle"],
                standard=row["standard"], accessible=row["accessible"])
           for row in source.tables["legs"] if row["plan"] == plan]
    grouped = {}
    for leg in legs:
        grouped.setdefault(leg.run, []).append(leg)
    runs = {}
    blocked = {}
    for run_id, run_legs in grouped.items():
        run_legs.sort(key=lambda leg: leg.seq)
        run = Run(run=run_id, plan=plan, vehicle=run_legs[0].vehicle,
                  legs=run_legs)
        runs[run_id] = run
        if apply_outage:
            reason = _run_blocked_reason(run, source)
            if reason:
                blocked[run_id] = reason
    walks = {(row["from"], row["to"]): row["minutes"]
             for row in source.tables["walks"]}
    network = Network(plan=plan, runs=runs, walks=walks, blocked_runs=blocked)
    network.vehicle_conflicts = _vehicle_conflicts(runs)
    return network


def _vehicle_conflicts(runs):
    by_vehicle = {}
    for run in runs.values():
        by_vehicle.setdefault(run.vehicle, []).append(run)
    conflicts = []
    for vehicle, vehicle_runs in by_vehicle.items():
        ordered = sorted(vehicle_runs, key=lambda item: item.start_min)
        for index, run in enumerate(ordered):
            for other in ordered[index + 1:]:
                if other.start_min >= run.end_min:
                    break
                if overlap(run.start_min, run.end_min,
                           other.start_min, other.end_min):
                    conflicts.append((vehicle, run.run, other.run))
    return conflicts


def active_runs(network):
    return {run_id: run for run_id, run in network.runs.items()
            if run_id not in network.blocked_runs}
