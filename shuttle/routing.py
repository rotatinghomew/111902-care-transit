"""可行路径枚举：在某个方案网络上为一项往返需求找出所有合规单程路径。

路径最多两段乘车、一次步行换乘（资料中的网络规模）。判定规则：
- 同 run 内可沿 seq 续乘，跨 run 接续要求后段发车严格晚于前段到达；
- 步行换乘时长不得超过该需求的 max_walk_min，且须能接续后段班次，
  即后段发车严格晚于“前段到达 + 步行时长”，零缓冲不算可接续；
- 去程到院时刻落入约诊窗口（备选窗口需资料明确同意才可用）；
- 返程从医院的首段发车时刻落入返程窗口；
- 轮椅需求要求途经每个区段都有轮椅位，普通需求只占普通座。
"""

from dataclasses import dataclass, field

from .model import active_runs
from .timeutil import hhmm, in_window, to_minutes

MAX_RIDES = 2

# 失败原因代码
UNREACHABLE_STATION = "站点不通"
WALK_LIMIT = "步行超限"
ACCESSIBILITY = "轮椅位不足"
TIME_WINDOW = "时间窗不合"
CONNECTION = "接续时间不合"
CAPACITY = "共享运力挤占"


@dataclass(frozen=True)
class Ride:
    run: str
    vehicle: str
    board: str
    alight: str
    seq_from: int
    seq_to: int
    depart: str
    arrive: str
    wheelchair: bool
    segments: tuple  # (run, seq) 元组，乘客占用的每个区段


@dataclass(frozen=True)
class Journey:
    rides: tuple
    walks: tuple                      # ((from, to, minutes), ...)
    window_kind: str                  # "primary" | "alt"
    end_time: str                     # 去程=到院时刻；返程=回到社区时刻
    start_time: str                   # 去程=离社区时刻；返程=离院时刻

    @property
    def segments(self):
        result = []
        for ride in self.rides:
            result.extend(ride.segments)
        return tuple(result)

    @property
    def uses_alt_window(self):
        return self.window_kind == "alt"


@dataclass
class SearchTrace:
    reasons: list = field(default_factory=list)

    def add(self, code, detail):
        self.reasons.append((code, detail))


def _ride_chain(run, board_node, wheelchair):
    """从某 run 的某站上车，给出所有可能的下车选择（同 run 续乘）。

    一旦后续区段缺少合适应座位即停止；返回 (选项, 是否曾被缺座截断)。
    """
    board_legs = [leg for leg in run.legs if leg.origin == board_node]
    options = []
    broken = False
    for board_leg in board_legs:
        segments = []
        for leg in run.legs:
            if leg.seq < board_leg.seq:
                continue
            capacity = leg.accessible if wheelchair else leg.standard
            if capacity < 1:
                broken = True
                break
            segments.append((run.run, leg.seq))
            options.append(Ride(
                run=run.run, vehicle=run.vehicle, board=board_node,
                alight=leg.dest, seq_from=board_leg.seq, seq_to=leg.seq,
                depart=board_leg.depart, arrive=leg.arrive,
                wheelchair=wheelchair, segments=tuple(segments)))
    return options, broken


def enumerate_journeys(network, trip, direction, trace=None):
    """返回 Journey 列表。direction: 'out' 去程 / 'in' 返程。"""
    origin = trip["community"] if direction == "out" else "医院"
    target = "医院" if direction == "out" else trip["community"]
    wheelchair = trip["wheelchair"]
    walks = network.walks
    runs = active_runs(network)
    found = {}
    if trace is None:
        trace = SearchTrace()

    def accept(chain_rides, chain_walks, end_time, start_time, window_kind):
        key = (tuple((ride.run, ride.seq_from, ride.seq_to)
                     for ride in chain_rides),
               tuple(chain_walks), window_kind)
        if key not in found:
            found[key] = Journey(rides=tuple(chain_rides),
                                 walks=tuple(chain_walks),
                                 window_kind=window_kind,
                                 end_time=end_time, start_time=start_time)

    def search(node, ride_count, walk_used, chain_rides, chain_walks,
               earliest_min, start_time):
        """earliest_min：在 node 可接续后续班次的最早时刻。"""
        if node == target and ride_count > 0:
            moment = chain_rides[-1].arrive
            if direction == "out":
                if in_window(moment, trip["arrival"]):
                    accept(chain_rides, chain_walks, moment, start_time,
                           "primary")
                if trip["alt_arrival"] and in_window(moment, trip["alt_arrival"]):
                    accept(chain_rides, chain_walks, moment, start_time,
                           "alt")
            else:
                accept(chain_rides, chain_walks, moment, start_time,
                       "primary")
        # 乘车推进（只允许时间向前的班次）
        if ride_count < MAX_RIDES:
            for run in runs.values():
                options, broken = _ride_chain(run, node, wheelchair)
                if broken:
                    trace.add(ACCESSIBILITY,
                              f"{run.run}在{node}之后缺少合适应座位区段")
                for ride in options:
                    if to_minutes(ride.depart) <= earliest_min:
                        trace.add(CONNECTION,
                                  f"{node}最早可接续{hhmm(earliest_min)}，"
                                  f"{ride.run}发{ride.depart}无缓冲")
                        continue
                    if direction == "in" and ride_count == 0 and not walk_used:
                        if not in_window(ride.depart, trip["return"]):
                            trace.add(TIME_WINDOW,
                                      f"离院{ride.depart}不在返程窗口"
                                      f"{trip['return']}")
                            continue
                    chain_start = start_time or ride.depart
                    search(ride.alight, ride_count + 1, walk_used,
                           chain_rides + [ride], chain_walks,
                           to_minutes(ride.arrive), chain_start)
        # 步行推进：到达时刻 = 最早可接续时刻 + 步行时长
        if not walk_used:
            for (w_from, w_to), minutes in walks.items():
                if w_from != node:
                    continue
                if minutes > trip["max_walk_min"]:
                    trace.add(WALK_LIMIT,
                              f"{w_from}->{w_to}步行{minutes}分钟>"
                              f"{trip['max_walk_min']}分钟")
                    continue
                walk = (w_from, w_to, minutes)
                search(w_to, ride_count, 1, chain_rides,
                       chain_walks + [walk],
                       earliest_min + minutes, start_time)

    search(origin, 0, 0, [], [], -1, None)

    # 去程补记到院时间窗不合（存在路径但没有任何窗口接收）
    if direction == "out" and found:
        if all(not in_window(journey.end_time, trip["arrival"])
               and not (trip["alt_arrival"]
                        and in_window(journey.end_time, trip["alt_arrival"]))
               for journey in found.values()):
            trace.add(TIME_WINDOW,
                      "可到院时刻均不在"
                      f"{trip['arrival']}"
                      + (f"/{trip['alt_arrival']}"
                         if trip["alt_arrival"] else ""))
    return list(found.values())


def primary_reason(trace):
    """按结构性优先级归纳单需求不可达原因。"""
    codes = [code for code, _ in trace.reasons]
    for code in (UNREACHABLE_STATION, WALK_LIMIT, ACCESSIBILITY,
                 CONNECTION, TIME_WINDOW):
        if code in codes:
            return code
    if not codes:
        return UNREACHABLE_STATION
    return codes[0]


def unreachable_trace(network, trip, direction):
    """没有路径时补充站点不通判定。"""
    trace = SearchTrace()
    journeys = enumerate_journeys(network, trip, direction, trace)
    if not journeys and not trace.reasons:
        origin = trip["community"] if direction == "out" else "医院"
        trace.add(UNREACHABLE_STATION, f"{origin}无可用班次或步行连接")
    return journeys, trace
