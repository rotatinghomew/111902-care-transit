"""分析编排与报告渲染（文本与单文件 HTML，无第三方依赖）。"""

import html
from pathlib import Path

from .audit import announced_plan, contraction_audit
from .model import PLANS, PLAN_NAMES, build_network, load_source
from .robust import last_return_audit, robust_bundles
from .solver import reservation_map, solve


def run_analysis(raw, apply_outage=False):
    source = load_source(raw)
    scenario_ids = [row["scenario"] for row in source.tables["scenarios"]]
    plans = {}
    for plan in PLANS:
        networks = {scenario_id: build_network(source, plan, apply_outage)
                    for scenario_id in scenario_ids}
        plan_block = {"scenarios": {}, "robust": None, "last_returns": {}}
        for scenario_id in scenario_ids:
            result = solve(source, networks[scenario_id], scenario_id,
                           reservation_map(source))
            plan_block["scenarios"][scenario_id] = result
            plan_block["last_returns"][scenario_id] = last_return_audit(
                source, result)
        plan_block["robust"] = robust_bundles(source, networks)
        plans[plan] = plan_block
    audit = None
    if apply_outage:
        audit = contraction_audit(source, announced_plan(source)[0], True)
    return {"source": source, "plans": plans, "audit": audit,
            "scenario_ids": scenario_ids, "apply_outage": apply_outage}


# ----------------------------- 文本渲染 -----------------------------

def _journey_text(journey):
    parts = []
    for index, ride in enumerate(journey.rides):
        if index == 0 and journey.walks and \
                journey.walks[0][0] == ride.board:
            pass
        parts.append(f"{ride.board}{ride.depart}→{ride.alight}{ride.arrive}"
                     f"[{ride.run}/{ride.vehicle}"
                     f"{'轮椅' if ride.wheelchair else '普通'}]")
    walk_text = "；步行".join(
        f"{w_from}→{w_to}{minutes}分"
        for w_from, w_to, minutes in journey.walks)
    if walk_text:
        parts.append("步行" + walk_text)
    return " + ".join(parts)


def _option_text(option):
    tag = "（备选窗口改约）" if option.uses_alt_window else ""
    return (f"去程 {_journey_text(option.outbound)} ｜ 返程 "
            f"{_journey_text(option.inbound)}{tag}")


def render_assignment_text(source, result):
    lines = []
    lines.append(f"方案 {result['plan']}（{PLAN_NAMES[result['plan']]}）"
                 f" × 情景「{result['scenario']}」"
                 f" 需求{len(result['trips'])}项")
    if result["blocked_runs"]:
        lines.append("  停用 run："
                     + "，".join(f"{run}（{why}）" for run, why in
                                 result["blocked_runs"].items()))
    if result["network_conflicts"]:
        lines.append("  ⚠ 同车时间冲突："
                     + "，".join(f"{v}:{a}/{b}" for v, a, b in
                                 result["network_conflicts"]))
    if result["breaches"]:
        lines.append("  ⚠ 锁定承诺违约："
                     + "；".join(f"{t} {why}" for t, why in result["breaches"]))
    lines.append(f"  极大可行排班 {len(result['assignments'])} 个：")
    for index, assignment in enumerate(result["assignments"], 1):
        head = (f"  [{index}] 服务{assignment.served_count}/"
                f"{len(result['trips'])}  社区底线："
                + ("全部守住" if assignment.floor_pass else "未全部守住"))
        lines.append(head)
        for item in assignment.floor_detail:
            n_a = "不适用" if item["required"] == 0 else \
                f"{item['ratio']:.0%}"
            lines.append(f"      - {item['community']}："
                         f"{item['served']}/{item['required']}={n_a}"
                         f"（底线{item['floor']:.0%}，"
                         f"{'达标' if item['pass'] else '破线'}）")
        for trip_id, option in sorted(assignment.served.items()):
            lines.append(f"      √ {trip_id}：{_option_text(option)}"
                         f"；{assignment.delay_notes[trip_id]}")
        for trip_id in assignment.unserved:
            code, detail = assignment.unserved_reasons[trip_id]
            lines.append(f"      × {trip_id}：【{code}】{detail}")
    lines.append("")
    return "\n".join(lines)


def render_robust_text(plan, block):
    robust = block["robust"]
    lines = [f"方案 {plan} 出车前稳健安排（须同时守住全部情景）："]
    lines.append("  可固定留座需求："
                 + ("、".join(robust["guaranteeable"]) or "无"))
    for trip_id, reservation in robust["mandatory"]:
        lines.append(f"  强制承诺：{trip_id} 已确认 "
                     f"{reservation['outbound']}/{reservation['inbound']}")
    lines.append("  不可固定（仅能现场递补或暂缓）："
                 + ("、".join(robust["not_guaranteeable"]) or "无"))
    lines.append(f"  稳健排班组合 {len(robust['bundles'])} 个：")
    for index, bundle in enumerate(robust["bundles"], 1):
        desc = "，".join(
            f"{trip_id}（" + _option_text(option) + "）"
            for trip_id, option in sorted(bundle.items())) or "空（仅强制承诺之外不发安排）"
        lines.append(f"    [{index}] {desc}")
    lines.append("")
    return "\n".join(lines)


def render_last_returns_text(plan, block):
    lines = [f"方案 {plan} 末班返程核对："]
    for scenario_id, audit in block["last_returns"].items():
        lines.append(f"  情景「{scenario_id}」")
        for item in audit["per_trip"]:
            if item["status"] == "无可用返程":
                reason = item["reason"][0] if item["reason"] else ""
                lines.append(f"      × {item['trip']}（{item['community']}）"
                             f"丧失末班返程：{reason}")
            else:
                lines.append(f"      √ {item['trip']}（{item['community']}）"
                             f"最晚离院{item['last_inbound_depart']}"
                             f"[{item['last_run']}]"
                             + ("（备选窗口）" if item["uses_alt"] else ""))
        for community, summary in audit["communities"].items():
            if summary["lost_last_return"]:
                lines.append(f"      ⚠ 社区「{community}」有需求丧失末班返程："
                             + "、".join(summary["trips"]))
    lines.append("")
    return "\n".join(lines)


def render_audit_text(audit):
    if not audit:
        return ""
    lines = ["容量收缩审计（车辆不可用已生效）：",
             f"  停用 run：" + "，".join(f"{k}（{v}）" for k, v in
                                       audit["blocked_runs"].items())]
    lines.append("  当前仍留座："
                 + ("；".join(f"{x['trip']}/{x['record']}"
                              f"{x['outbound']}+{x['inbound']}"
                              for x in audit["held_seats"]) or "无"))
    if audit["invalid_arrangements"]:
        lines.append("  失效安排（历史公告不得批准其继续）：")
        for item in audit["invalid_arrangements"]:
            lines.append(f"      × {item['record']}/{item['trip']}"
                         f"{item['outbound']}+{item['inbound']}："
                         f"{item['reason']}")
    lines.append("  逐需求处置建议：")
    for item in audit["suggestions"]:
        lines.append(f"      「{item['scenario']}」{item['trip']}"
                     f"（{item['community']}）：{item['decision']} —— "
                     f"{item['detail']}")
    if audit["bulletin"]:
        lines.append(f"  公告 {audit['bulletin']['announced_at']} "
                     f"{audit['bulletin']['plan']}：{audit['bulletin']['note']}")
    lines.append("")
    return "\n".join(lines)


def render_text(analysis):
    lines = ["卫生接驳停运方案评估（2026-11-24，本地合成资料）", "=" * 56, ""]
    for plan in PLANS:
        for scenario_id in analysis["scenario_ids"]:
            lines.append(render_assignment_text(
                analysis["source"].raw and analysis["source"],
                analysis["plans"][plan]["scenarios"][scenario_id]))
        lines.append(render_robust_text(plan, analysis["plans"][plan]))
        lines.append(render_last_returns_text(plan, analysis["plans"][plan]))
    lines.append(render_audit_text(analysis["audit"]))
    return "\n".join(lines)


# ----------------------------- HTML 渲染 -----------------------------

def _esc(value):
    return html.escape(str(value))


def _reason_badge(code):
    color = {"站点不通": "#b45309", "步行超限": "#a16207",
             "轮椅位不足": "#b91c1c", "时间窗不合": "#1d4ed8",
             "接续时间不合": "#7c3aed", "共享运力挤占": "#be123c"}.get(code, "#374151")
    return (f'<span class="badge" style="background:{color}">'
            f"{_esc(code)}</span>")


def render_html(analysis, output_path):
    source = analysis["source"]
    parts = ["""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>卫生接驳停运方案评估</title><style>
body{font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif;margin:24px;color:#111827;line-height:1.5}
h1{font-size:22px}h2{font-size:18px;margin-top:28px;border-left:5px solid #2563eb;padding-left:8px}
h3{font-size:15px;margin:16px 0 6px}table{border-collapse:collapse;margin:8px 0;font-size:13px}
th,td{border:1px solid #d1d5db;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f3f4f6}.card{border:1px solid #d1d5db;border-radius:8px;padding:12px;margin:10px 0}
.bad{color:#b91c1c;font-weight:600}.good{color:#047857;font-weight:600}
.badge{color:#fff;border-radius:10px;padding:1px 8px;font-size:12px;white-space:nowrap}
details{margin:6px 0;border:1px solid #e5e7eb;border-radius:6px;padding:6px 10px;background:#fafafa}
summary{cursor:pointer;font-weight:600}.mono{font-family:monospace;font-size:12px}
.pass{background:#d1fae5}.fail{background:#fee2e2}
</style></head><body>""",
             "<h1>卫生接驳停运方案评估（2026-11-24，本地合成资料）</h1>",
             "<p>情景不赋概率、不拼接各情景最大数；轮椅位与普通座位分账；"
             "同一 run 区段容量只计一次。点击各条目可展开差异原因。</p>"]

    for plan in PLANS:
        block = analysis["plans"][plan]
        parts.append(f"<h2>方案 {plan}｜{PLAN_NAMES[plan]}</h2>")
        for scenario_id in analysis["scenario_ids"]:
            result = block["scenarios"][scenario_id]
            parts.append(f'<div class="card"><h3>情景「{_esc(scenario_id)}」'
                         f'（需求 {len(result["trips"])} 项）</h3>')
            if result["blocked_runs"]:
                parts.append("<p class='bad'>停用 run："
                             + _esc(result["blocked_runs"]) + "</p>")
            if result["breaches"]:
                parts.append("<p class='bad'>锁定承诺违约："
                             + _esc("；".join(f"{t} {w}" for t, w in
                                             result["breaches"])) + "</p>")
            parts.append("<table><tr><th>#</th><th>服务量</th><th>社区底线</th>"
                         "<th>明细（点开看原因）</th></tr>")
            for index, assignment in enumerate(result["assignments"], 1):
                floor_cls = "pass" if assignment.floor_pass else "fail"
                floor_cells = []
                for x in assignment.floor_detail:
                    if x["required"] == 0:
                        ratio_text = "不适用"
                    else:
                        ratio_text = f"{x['ratio']:.0%}"
                    floor_cells.append(
                        f"{x['community']} {x['served']}/{x['required']}"
                        f"={ratio_text}（底线{x['floor']:.0%} "
                        f"{'达标' if x['pass'] else '破线'}）")
                floor_text = "；".join(floor_cells)
                parts.append(
                    f'<tr><td>{index}</td><td>{assignment.served_count}/'
                    f'{len(result["trips"])}</td>'
                    f'<td class="{floor_cls}">{_esc(floor_text)}</td><td>')
                for trip_id, option in sorted(assignment.served.items()):
                    parts.append(
                        f"<details><summary class='good'>√ {_esc(trip_id)}"
                        f"（{_esc(assignment.delay_notes[trip_id])}）</summary>"
                        f"<div class='mono'>{_esc(_option_text(option))}"
                        f"</div></details>")
                for trip_id in assignment.unserved:
                    code, detail = assignment.unserved_reasons[trip_id]
                    parts.append(
                        f"<details><summary class='bad'>× {_esc(trip_id)} "
                        f"{_reason_badge(code)}</summary>"
                        f"<div>{_esc(detail)}</div></details>")
                parts.append("</td></tr>")
            parts.append("</table></div>")

        robust = block["robust"]
        parts.append('<div class="card"><h3>出车前稳健安排（全情景共用排班）</h3>')
        parts.append("<p>可固定留座：<b>"
                     + _esc("、".join(robust["guaranteeable"]) or "无")
                     + "</b>；不可固定（现场递补/暂缓）：<b>"
                     + _esc("、".join(robust["not_guaranteeable"]) or "无")
                     + "</b></p>")
        for index, bundle in enumerate(robust["bundles"], 1):
            body = "<br>".join(_esc(_option_text(option))
                               for _, option in sorted(bundle.items())) or "无额外固定安排"
            parts.append(f"<details><summary>稳健组合 {index}："
                         f"{len(bundle)} 项固定留座</summary>"
                         f"<div class='mono'>{body}</div></details>")
        parts.append("</div>")

        parts.append('<div class="card"><h3>末班返程核对（逐社区，不做总体平均）</h3>')
        for scenario_id, ret in block["last_returns"].items():
            parts.append(f"<p>情景「{_esc(scenario_id)}」</p><table>"
                         "<tr><th>需求</th><th>社区</th><th>最晚离院/run</th>"
                         "<th>状态与原因</th></tr>")
            for item in ret["per_trip"]:
                if item["status"] == "无可用返程":
                    code = item["reason"][0] if item["reason"] else ""
                    detail = item["reason"][1] if item["reason"] else ""
                    parts.append(f"<tr><td>{_esc(item['trip'])}</td>"
                                 f"<td>{_esc(item['community'])}</td>"
                                 f"<td>—</td><td class='bad'>"
                                 f"{_reason_badge(code)} {_esc(detail)}</td></tr>")
                else:
                    parts.append(
                        f"<tr><td>{_esc(item['trip'])}</td>"
                        f"<td>{_esc(item['community'])}</td>"
                        f"<td class='mono'>{_esc(item['last_inbound_depart'])}"
                        f" [{_esc(item['last_run'])}]</td>"
                        f"<td class='good'>有返程"
                        + ("（备选窗口）" if item["uses_alt"] else "")
                        + "</td></tr>")
            parts.append("</table>")
        parts.append("</div>")

    if analysis["audit"]:
        audit = analysis["audit"]
        parts.append('<h2>容量收缩审计与改约建议</h2><div class="card">')
        parts.append("<p>停用 run：" + _esc(audit["blocked_runs"]) + "</p>")
        parts.append("<table><tr><th>记录</th><th>需求</th><th>状态</th>"
                     "<th>双程 run</th><th>说明</th></tr>")
        for item in audit["held_seats"]:
            parts.append(f"<tr><td>{_esc(item['record'])}</td>"
                         f"<td>{_esc(item['trip'])}</td>"
                         f"<td class='good'>继续留座</td>"
                         f"<td class='mono'>{_esc(item['outbound'])}+"
                         f"{_esc(item['inbound'])}</td><td>—</td></tr>")
        for item in audit["invalid_arrangements"]:
            parts.append(f"<tr><td>{_esc(item['record'])}</td>"
                         f"<td>{_esc(item['trip'])}</td>"
                         f"<td class='bad'>{_esc(item['status'])}</td>"
                         f"<td class='mono'>{_esc(item['outbound'])}+"
                         f"{_esc(item['inbound'])}</td>"
                         f"<td>{_esc(item['reason'])}</td></tr>")
        parts.append("</table><h3>逐需求处置（未同意的新窗口不视为接替）</h3>"
                     "<table><tr><th>情景</th><th>需求</th><th>处置</th>"
                     "<th>依据</th></tr>")
        for item in audit["suggestions"]:
            parts.append(f"<tr><td>{_esc(item['scenario'])}</td>"
                         f"<td>{_esc(item['trip'])}（{_esc(item['community'])}）"
                         f"</td><td>{_esc(item['decision'])}</td>"
                         f"<td>{_esc(item['detail'])}</td></tr>")
        parts.append("</table></div>")

    parts.append("</body></html>")
    Path(output_path).write_text("".join(parts), encoding="utf-8")
    return output_path
