"""命令行入口：python3 -I -m shuttle.cli <子命令> [--data PATH] ..."""

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .audit import contraction_audit, rebooking_lookup
from .model import PLANS, PLAN_NAMES, build_network, load_source
from .report import (render_assignment_text, render_audit_text, render_html,
                     render_last_returns_text, render_robust_text,
                     run_analysis, render_text)
from .robust import conditional_fill_ins, robust_bundles
from .routing import unreachable_trace
from .solver import reservation_map, solve

DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data" / "supplies.json"


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _jsonable(value):
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _result_to_json(result):
    payload = {"plan": result["plan"], "scenario": result["scenario"],
               "blocked_runs": result["blocked_runs"],
               "breaches": result["breaches"],
               "network_conflicts": result["network_conflicts"],
               "assignments": []}
    for assignment in result["assignments"]:
        payload["assignments"].append({
            "served": {trip: _jsonable(option)
                       for trip, option in assignment.served.items()},
            "unserved": assignment.unserved,
            "floor_pass": assignment.floor_pass,
            "floor_detail": assignment.floor_detail,
            "unserved_reasons": dict(assignment.unserved_reasons),
            "delay_notes": assignment.delay_notes,
        })
    return payload


def cmd_scenarios(args):
    raw = _load(args.data)
    analysis = run_analysis(raw, apply_outage=args.outage)
    if args.format == "json":
        out = {}
        for plan in PLANS:
            out[plan] = {
                scenario: _result_to_json(
                    analysis["plans"][plan]["scenarios"][scenario])
                for scenario in analysis["scenario_ids"]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    for plan in PLANS:
        for scenario in analysis["scenario_ids"]:
            print(render_assignment_text(
                analysis["source"],
                analysis["plans"][plan]["scenarios"][scenario]))


def cmd_compare(args):
    raw = _load(args.data)
    analysis = run_analysis(raw, apply_outage=args.outage)
    lines = ["方案对照（每方案 × 每情景，含社区底线与末班返程）", "=" * 50]
    for plan in PLANS:
        block = analysis["plans"][plan]
        lines.append(f"\n## {plan} {PLAN_NAMES[plan]}")
        for scenario, result in block["scenarios"].items():
            best = result["assignments"][0]
            floor = "；".join(
                f"{x['community']}{x['served']}/{x['required']}"
                f"{'达标' if x['pass'] else '破线'}"
                for x in best.floor_detail)
            lines.append(f"  {scenario}: 最多服务{best.served_count}/"
                         f"{len(result['trips'])}；{floor}")
            lost = [trip for summary in
                    block["last_returns"][scenario]["communities"].values()
                    for trip in summary["trips"]]
            lines.append("    丧失末班返程：" + ("、".join(lost) or "无"))
        lines.append("  " + render_robust_text(plan, block).split("\n", 1)[1]
                     .replace("\n", "\n  "))
    print("\n".join(lines))


def cmd_robust(args):
    raw = _load(args.data)
    source = load_source(raw)
    network_map = {row["scenario"]: build_network(
        source, args.plan, apply_outage=args.outage)
        for row in source.tables["scenarios"]}
    result = robust_bundles(source, network_map)
    if args.format == "json":
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    else:
        block = {"robust": result}
        print(render_robust_text(args.plan, block))


def cmd_fillins(args):
    raw = _load(args.data)
    source = load_source(raw)
    network_map = {row["scenario"]: build_network(
        source, args.plan, apply_outage=args.outage)
        for row in source.tables["scenarios"]}
    robust_result = robust_bundles(source, network_map)
    merged = {}
    for scenario_id, network in network_map.items():
        result = solve(source, network, scenario_id,
                       reservation_map(source))
        for item in conditional_fill_ins(source, robust_result, result):
            entry = merged.setdefault(item["trip"], {
                "trip": item["trip"], "community": item["community"],
                "scenarios": {}})
            entry["scenarios"][scenario_id] = {
                "has_feasible_round_trip": item["has_option_in_scenario"],
                "reason": item["reason"],
            }
    print(json.dumps(sorted(merged.values(), key=lambda x: x["trip"]),
                     ensure_ascii=False, indent=2))


def cmd_drilldown(args):
    """点开一项差异：给出该需求在指定方案/情景下的路径级原因。"""
    raw = _load(args.data)
    source = load_source(raw)
    network = build_network(source, args.plan, apply_outage=args.outage)
    row = source.trip(args.trip)
    payload = {"trip": args.trip, "plan": args.plan,
               "community": row["community"], "wheelchair": row["wheelchair"],
               "locked": row["locked"],
               "primary_window": row["arrival"],
               "alt_window": row["alt_arrival"],
               "return_window": row["return"],
               "max_walk_min": row["max_walk_min"],
               "blocked_runs": network.blocked_runs}
    for direction, label in (("out", "去程"), ("in", "返程")):
        journeys, trace = unreachable_trace(network, row, direction)
        rejections = [{"code": code, "detail": detail}
                      for code, detail in trace.reasons]
        payload[label] = {
            "feasible_paths": [_jsonable(journey) for journey in journeys],
            "rejections": rejections,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_lastreturn(args):
    raw = _load(args.data)
    analysis = run_analysis(raw, apply_outage=args.outage)
    for plan in PLANS:
        if args.plan and plan != args.plan:
            continue
        print(render_last_returns_text(plan, analysis["plans"][plan]))


def cmd_audit(args):
    raw = _load(args.data)
    source = load_source(raw)
    audit = contraction_audit(source, args.plan, apply_outage=True)
    if args.format == "json":
        payload = {key: _jsonable(value) for key, value in audit.items()
                   if key != "scenario_results"}
        payload["scenario_results"] = {
            scenario: _result_to_json(result)
            for scenario, result in audit["scenario_results"].items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_audit_text(audit))


def cmd_held(args):
    raw = _load(args.data)
    source = load_source(raw)
    audit = contraction_audit(source, args.plan, apply_outage=True)
    print(json.dumps(_jsonable(rebooking_lookup(audit, args.trip)),
                     ensure_ascii=False, indent=2))


def cmd_report(args):
    raw = _load(args.data)
    analysis = run_analysis(raw, apply_outage=args.outage)
    if args.text:
        print(render_text(analysis))
        return
    path = render_html(analysis, args.output)
    print(f"报告已写出：{path}")


def build_parser():
    parser = argparse.ArgumentParser(description="卫生接驳停运方案分析")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scenarios", help="逐方案逐情景的可行排班与挤出原因")
    p.add_argument("--outage", action="store_true")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("compare", help="三方案服务能力与底线对照")
    p.add_argument("--outage", action="store_true")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("robust", help="出车前全情景稳健排班")
    p.add_argument("--plan", choices=PLANS, default="JOIN")
    p.add_argument("--outage", action="store_true")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_robust)

    p = sub.add_parser("last-return", help="末班返程逐社区核对")
    p.add_argument("--plan", choices=PLANS)
    p.add_argument("--outage", action="store_true")
    p.set_defaults(func=cmd_lastreturn)

    p = sub.add_parser("fillins", help="固定留座之外的现场递补候选")
    p.add_argument("--plan", choices=PLANS, default="JOIN")
    p.add_argument("--outage", action="store_true")
    p.set_defaults(func=cmd_fillins)

    p = sub.add_parser("drilldown", help="单需求路径级差异钻取")
    p.add_argument("--trip", required=True)
    p.add_argument("--plan", choices=PLANS, default="JOIN")
    p.add_argument("--outage", action="store_true")
    p.set_defaults(func=cmd_drilldown)

    p = sub.add_parser("audit", help="容量收缩审计与改约建议")
    p.add_argument("--plan", default=None)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("held", help="查询当前仍为谁留座")
    p.add_argument("--plan", default=None)
    p.add_argument("--trip")
    p.set_defaults(func=cmd_held)

    p = sub.add_parser("report", help="生成 HTML/文本报告")
    p.add_argument("--output", default="reports/analysis.html")
    p.add_argument("--outage", action="store_true")
    p.add_argument("--text", action="store_true")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
