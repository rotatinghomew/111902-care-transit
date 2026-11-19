"""分析层业务规则测试（标准库 unittest）。

运行：python3 -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shuttle.audit import announced_plan, contraction_audit, rebooking_lookup
from shuttle.model import PLANS, build_network, load_source
from shuttle.robust import last_return_audit, robust_bundles
from shuttle.routing import (CAPACITY, CONNECTION, UNREACHABLE_STATION,
                             WALK_LIMIT, enumerate_journeys,
                             unreachable_trace)
from shuttle.solver import (reservation_map, solve, _consume, _empty_pools,
                            _release)

ROOT = Path(__file__).resolve().parents[1]


class AnalysisCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads((ROOT / "data" / "supplies.json").read_text(
            encoding="utf-8"))
        cls.source = load_source(cls.raw)
        cls.reservations = reservation_map(cls.source)
        cls.networks = {
            plan: build_network(cls.source, plan, apply_outage=False)
            for plan in PLANS}

    def solve(self, plan, scenario, outage=False):
        network = build_network(self.source, plan, apply_outage=outage)
        return solve(self.source, network, scenario, self.reservations)

    def served_sets(self, result):
        return sorted(
            tuple(sorted(assignment.served))
            for assignment in result["assignments"])

    # ---- 往返一体：去程返程必须同时走通 ----

    def test_round_trip_is_one_demand(self):
        for plan in PLANS:
            for scenario in ("常态", "复诊增多"):
                result = self.solve(plan, scenario)
                for assignment in result["assignments"]:
                    for trip_id, option in assignment.served.items():
                        self.assertTrue(option.outbound.rides)
                        self.assertTrue(option.inbound.rides)

    # ---- BASE 旧线能力 ----

    def test_base_normal_serves_all_and_meets_floors(self):
        result = self.solve("BASE", "常态")
        self.assertEqual(self.served_sets(result),
                         [("T1", "T2", "T3")])
        self.assertTrue(result["assignments"][0].floor_pass)

    def test_base_surge_t4_unreachable_and_floor_holds(self):
        result = self.solve("BASE", "复诊增多")
        assignment = result["assignments"][0]
        self.assertEqual(assignment.unserved, ["T4"])
        self.assertEqual(assignment.unserved_reasons["T4"][0],
                         UNREACHABLE_STATION)
        self.assertTrue(assignment.floor_pass)

    # ---- VIA 绕行：共享运力挤占与改约窗口 ----

    def test_via_normal_two_tradeoffs_both_break_a_floor(self):
        result = self.solve("VIA", "常态")
        self.assertEqual(self.served_sets(result),
                         [("T1", "T2"), ("T1", "T3")])
        self.assertFalse(any(a.floor_pass for a in result["assignments"]))
        # 被挤出的需求必须解释为 D2#1 共享运力挤占
        for assignment in result["assignments"]:
            for trip_id in assignment.unserved:
                code, detail = assignment.unserved_reasons[trip_id]
                self.assertEqual(code, CAPACITY)
                self.assertIn("D2", detail)

    def test_via_t2_uses_only_consented_alt_window(self):
        result = self.solve("VIA", "常态")
        assignment = next(a for a in result["assignments"]
                          if "T2" in a.served)
        option = assignment.served["T2"]
        self.assertTrue(option.uses_alt_window)
        self.assertIn("改约备选", assignment.delay_notes["T2"])
        # T3 没有 alt_arrival，任何选项都不得标记备选窗口
        option_t3 = next(a.served["T3"] for a in result["assignments"]
                         if "T3" in a.served)
        self.assertFalse(option_t3.uses_alt_window)

    def test_via_surge_floor_feasible_squeezes_t2(self):
        result = self.solve("VIA", "复诊增多")
        passing = [a for a in result["assignments"] if a.floor_pass]
        self.assertEqual(len(passing), 1)
        self.assertEqual(passing[0].unserved, ["T2"])
        self.assertEqual(passing[0].unserved_reasons["T2"][0], CAPACITY)
        # 另一个取舍保住柳桥但梧桐破线
        failing = [a for a in result["assignments"] if not a.floor_pass]
        self.assertTrue(any(a.unserved == ["T3"] for a in failing))

    # ---- JOIN 共享接驳：零缓冲步行不可接续，柳桥两情景破线 ----

    def test_join_zero_buffer_transfer_rejected(self):
        network = self.networks["JOIN"]
        for trip_id in ("T2", "T4"):
            row = self.source.trip(trip_id)
            journeys, trace = unreachable_trace(network, row, "out")
            self.assertEqual(journeys, [])
            self.assertTrue(any(code == CONNECTION
                                for code, _ in trace.reasons))

    def test_join_floor_fails_both_scenarios(self):
        for scenario in ("常态", "复诊增多"):
            result = self.solve("JOIN", scenario)
            self.assertFalse(any(a.floor_pass
                                 for a in result["assignments"]))
            for assignment in result["assignments"]:
                liu = next(x for x in assignment.floor_detail
                           if x["community"] == "柳桥")
                self.assertFalse(liu["pass"])

    # ---- 轮椅位与普通座位分账，不互相替代 ----

    def test_wheelchair_and_standard_pools_separate(self):
        network = self.networks["VIA"]
        # D1#2 普通座 1、轮椅位 1：T2（普通）与 T4（轮椅）可同时占用
        t2 = self.source.trip("T2")
        t4 = self.source.trip("T4")
        j2 = next(j for j in enumerate_journeys(network, t2, "out")
                  if j.rides[0].run == "D1")
        j4 = next(j for j in enumerate_journeys(network, t4, "out")
                  if j.rides[0].run == "D1")
        pools = _empty_pools()
        self.assertTrue(_consume(pools, network, j2, False)[0])
        self.assertTrue(_consume(pools, network, j4, True)[0])
        # 再放一个普通需求到 D1#2 必须破容
        again, conflict = _consume(pools, network, j2, False)
        self.assertFalse(again)
        self.assertEqual(conflict[2], "standard")
        _release(pools, j4, True)
        _release(pools, j2, False)

    def test_wheelchair_requires_accessible_every_segment(self):
        network = self.networks["VIA"]
        t4 = self.source.trip("T4")
        journeys = enumerate_journeys(network, t4, "out")
        self.assertTrue(journeys)
        for journey in journeys:
            for run_id, seq in journey.segments:
                leg = next(leg for leg in network.runs[run_id].legs
                           if leg.seq == seq)
                self.assertGreaterEqual(leg.accessible, 1)

    # ---- 同 run 多站只产生一份区段容量；同车时间冲突可查验 ----

    def test_same_run_segment_counted_once(self):
        network = self.networks["VIA"]
        t2 = self.source.trip("T2")
        journey = next(iter(enumerate_journeys(network, t2, "out")))
        # D1 跨柳桥→梧桐→医院，占用两个区段，但只属于同一个 run
        self.assertEqual({ride.run for ride in journey.rides}, {"D1"})
        self.assertEqual(len(journey.segments), 2)
        pools = _empty_pools()
        _consume(pools, network, journey, False)
        self.assertEqual(pools[("D1", 1)]["standard"], 1)
        self.assertEqual(pools[("D1", 2)]["standard"], 1)

    def test_no_vehicle_time_conflicts_in_source(self):
        for plan in PLANS:
            self.assertEqual(self.networks[plan].vehicle_conflicts, [],
                             plan)

    # ---- 步行限制 ----

    def test_walk_limit_enforced(self):
        # 资料步行 4 分钟；构造一个超过限制的需求视角
        network = self.networks["JOIN"]
        row = dict(self.source.trip("T2"))
        row["max_walk_min"] = 3
        journeys, trace = unreachable_trace(network, row, "out")
        self.assertTrue(any(code == WALK_LIMIT
                            for code, _ in trace.reasons))

    # ---- 情景不拼接：各自完整分析 ----

    def test_scenarios_analyzed_separately(self):
        normal = self.solve("BASE", "常态")
        surge = self.solve("BASE", "复诊增多")
        normal_trips = set(normal["trips"])
        surge_trips = set(surge["trips"])
        self.assertEqual(normal_trips, {"T1", "T2", "T3"})
        self.assertEqual(surge_trips, {"T1", "T2", "T3", "T4"})
        # 不允许把两情景最大数拼成 4/3 之类的总量
        self.assertLessEqual(
            max(a.served_count for a in normal["assignments"]), 3)

    # ---- 锁定承诺先占容量且不可被当作取消 ----

    def test_locked_reservation_forced_and_never_unserved(self):
        for plan in PLANS:
            for scenario in ("常态", "复诊增多"):
                result = self.solve(plan, scenario)
                self.assertFalse(result["breaches"])
                for assignment in result["assignments"]:
                    self.assertIn("T1", assignment.served)
                    option = assignment.served["T1"]
                    self.assertEqual(
                        [r.run for r in option.outbound.rides], ["B1"])
                    self.assertEqual(
                        [r.run for r in option.inbound.rides], ["B1R"])

    # ---- 稳健排班：一份安排对全部情景守得住 ----

    def test_robust_bundles_hold_across_scenarios(self):
        for plan in PLANS:
            network_map = {row["scenario"]: build_network(
                self.source, plan) for row in
                self.source.tables["scenarios"]}
            robust = robust_bundles(self.source, network_map)
            self.assertTrue(robust["bundles"])
            for bundle in robust["bundles"]:
                self.assertIn("T1", bundle)  # 强制承诺必在
            if plan == "VIA":
                # T2、T3 在 D2#1 互斥，任何稳健组合都不能同时固定
                for bundle in robust["bundles"]:
                    self.assertFalse("T2" in bundle and "T3" in bundle)
            if plan == "JOIN":
                self.assertEqual(robust["not_guaranteeable"], ["T2", "T4"])

    # ---- 末班返程逐社区，不许总体平均掩盖 ----

    def test_last_return_per_community(self):
        join = self.solve("JOIN", "复诊增多")
        audit = last_return_audit(self.source, join)
        self.assertTrue(audit["communities"]["柳桥"]["lost_last_return"])
        self.assertEqual(
            set(audit["communities"]["柳桥"]["trips"]), {"T2", "T4"})
        self.assertFalse(audit["communities"]["梧桐"]["lost_last_return"])

    # ---- 容量收缩：公告不批准失效安排，留座/改约分类 ----

    def test_contraction_audit_announced_join(self):
        plan, at = announced_plan(self.source)
        self.assertEqual(plan, "JOIN")
        audit = contraction_audit(self.source, plan, apply_outage=True)
        self.assertIn("F1", audit["blocked_runs"])
        self.assertEqual([x["trip"] for x in audit["held_seats"]], ["T1"])
        self.assertTrue(all(x["status"] == "继续留座"
                            for x in audit["held_seats"]))
        # 公告只作历史信息
        self.assertIn("不得据此继续派车", audit["bulletin"]["note"])

    def test_contraction_held_seat_lookup(self):
        audit = contraction_audit(self.source, "JOIN", apply_outage=True)
        lookup = rebooking_lookup(audit)
        self.assertEqual([x["trip"] for x in lookup["seats"]], ["T1"])
        t2 = rebooking_lookup(audit, "T2")
        self.assertTrue(any(s["decision"] == "告知暂缓"
                            for s in t2["suggestions"]))

    def test_contraction_liu_community_suspended_not_rebooked(self):
        audit = contraction_audit(self.source, "JOIN", apply_outage=True)
        for item in audit["suggestions"]:
            if item["community"] == "柳桥":
                self.assertEqual(item["decision"], "告知暂缓")
                self.assertNotEqual(item["decision"], "建议改约（待同意）")
        # 梧桐 T3 在 V1 运力下仍可兑现
        self.assertTrue(any(
            item["trip"] == "T3" and item["decision"].startswith("可兑现")
            for item in audit["suggestions"]))

    def test_contraction_does_not_treat_locked_as_cancel(self):
        audit = contraction_audit(self.source, "JOIN", apply_outage=True)
        held_trips = {x["trip"] for x in audit["held_seats"]}
        invalid_trips = {x["trip"] for x in audit["invalid_arrangements"]}
        self.assertIn("T1", held_trips)
        self.assertNotIn("T1", invalid_trips)

    # ---- 同车 run 阻塞按不可用窗口精确判定 ----

    def test_outage_blocks_only_overlapping_runs(self):
        network = build_network(self.source, "BASE", apply_outage=True)
        self.assertIn("B2", network.blocked_runs)       # 08:50 落入窗口
        self.assertNotIn("B2R", network.blocked_runs)   # 11:15 已恢复
        self.assertNotIn("B1", network.blocked_runs)    # V1 不受影响

    def test_outage_scenarios_keep_v1_commitments_only(self):
        for plan in PLANS:
            for scenario in ("常态", "复诊增多"):
                result = self.solve(plan, scenario, outage=True)
                self.assertTrue(result["blocked_runs"])
                for assignment in result["assignments"]:
                    # T1 的 V1 双程在任何方案收缩后仍被保障
                    self.assertIn("T1", assignment.served)
                    for trip_id in assignment.served:
                        option = assignment.served[trip_id]
                        used_runs = {ride.run for ride in
                                     option.outbound.rides + option.inbound.rides}
                        self.assertFalse(
                            used_runs & set(result["blocked_runs"]))

    def test_outage_join_liu_all_suspended_and_floors_break(self):
        result = self.solve("JOIN", "复诊增多", outage=True)
        assignment = result["assignments"][0]
        self.assertEqual(set(assignment.unserved), {"T2", "T4"})
        self.assertFalse(assignment.floor_pass)
        for trip_id in assignment.unserved:
            self.assertEqual(assignment.unserved_reasons[trip_id][0],
                             UNREACHABLE_STATION)


if __name__ == "__main__":
    unittest.main()
