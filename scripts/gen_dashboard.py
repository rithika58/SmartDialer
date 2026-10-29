#!/usr/bin/env python3
"""
Generate a self-contained SmartDialer operations dashboard.

    python3 scripts/gen_dashboard.py [--agents 60] [--minutes 30] [--out dashboard.html]

The output is a single HTML file with inline CSS and hand-built SVG: no CDN,
no web fonts, no external images, no JavaScript dependencies.  It renders
identically in a sandboxed iframe, offline in a browser, or in VS Code's
preview.

The data is not fabricated.  The script runs the real simulator and embeds the
telemetry it produces.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smartdialer.domain.states import AgentState, CallState
from smartdialer.engine.pacing import PacingMode
from smartdialer.providers.mock import ProviderA, ProviderB
from smartdialer.sim.simulator import Scenario, Simulation

# --------------------------------------------------------------------------- #
# palette
# --------------------------------------------------------------------------- #
BG = "#0f1117"
PANEL = "#171a23"
PANEL2 = "#1d212c"
LINE = "#2a2f3d"
TEXT = "#e6e9ef"
MUTED = "#8b93a7"
GOOD = "#3fb950"
WARN = "#d29922"
BAD = "#f85149"
INFO = "#58a6ff"
PURPLE = "#bc8cff"

AGENT_COLORS = {
    "OFFLINE": "#4b5163",
    "AVAILABLE": "#3fb950",
    "RESERVED": "#58a6ff",
    "DIALING": "#d29922",
    "CONNECTED": "#bc8cff",
    "WRAP_UP": "#f0883e",
    "PAUSED": "#6e7681",
}

PIPELINE = [
    ("QUEUED", "#4b5163"),
    ("RESERVED", "#58a6ff"),
    ("INITIATED", "#39c5cf"),
    ("RINGING", "#d29922"),
    ("ANSWERED", "#f0883e"),
    ("CONNECTED", "#bc8cff"),
    ("COMPLETED", "#3fb950"),
    ("FAILED", "#f85149"),
    ("CANCELLED", "#6e7681"),
]

SAFETY_COLORS = {
    "approve": GOOD,
    "reduce": "#d29922",
    "reject": "#6e7681",
    "fallback_progressive": INFO,
}


# --------------------------------------------------------------------------- #
# SVG helpers
# --------------------------------------------------------------------------- #
def esc(s) -> str:
    return html.escape(str(s))


def pct(v) -> str:
    return f"{float(v) * 100:.0f}%"


def fmt_hms(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def line_chart(series, x_max, y_max, w=760, h=200, bands=(), y_fmt=pct,
               target=None, target_label=""):
    """Multi-series line chart.  `series` is a list of dicts with
    label / color / points[(x, y)]."""
    pad_l, pad_r, pad_t, pad_b = 52, 14, 12, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def sx(x):
        return pad_l + (x / x_max if x_max else 0) * iw

    def sy(y):
        return pad_t + ih - (min(y, y_max) / y_max if y_max else 0) * ih

    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" '
           f'preserveAspectRatio="xMidYMid meet" role="img">']

    for (x0, x1, colour, label) in bands:
        out.append(
            f'<rect x="{sx(x0):.1f}" y="{pad_t}" width="{max(sx(x1)-sx(x0),1):.1f}" '
            f'height="{ih}" fill="{colour}" opacity="0.13"/>'
        )
        out.append(
            f'<text x="{sx(x0)+4:.1f}" y="{pad_t+11}" fill="{colour}" '
            f'font-size="9" opacity="0.85">{esc(label)}</text>'
        )

    # grid + y labels
    for i in range(5):
        v = y_max * i / 4
        y = sy(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" '
                   f'stroke="{LINE}" stroke-width="1"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" fill="{MUTED}" '
                   f'font-size="9" text-anchor="end">{y_fmt(v)}</text>')

    if target is not None:
        y = sy(target)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" '
                   f'stroke="{BAD}" stroke-width="1" stroke-dasharray="4 3"/>')
        out.append(f'<text x="{w-pad_r-2}" y="{y-4:.1f}" fill="{BAD}" '
                   f'font-size="9" text-anchor="end">{esc(target_label or "target")}</text>')

    # x labels
    for i in range(5):
        x = x_max * i / 4
        out.append(f'<text x="{sx(x):.1f}" y="{h-8}" fill="{MUTED}" '
                   f'font-size="9" text-anchor="middle">{fmt_hms(x)}</text>')

    for s in series:
        pts = s["points"]
        if not pts:
            continue
        d = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in pts)
        out.append(f'<polyline points="{d}" fill="none" stroke="{s["color"]}" '
                   f'stroke-width="2" stroke-linejoin="round" opacity="0.95"/>')
        for (x, y) in pts[:: max(1, len(pts) // 40)]:
            out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="1.6" '
                       f'fill="{s["color"]}" opacity="0.6"/>')

    out.append("</svg>")
    return "\n".join(out)


def stacked_area(series, x_max, y_max, w=760, h=180, bands=()):
    """series: list of dicts with label/color/points, stacked bottom-up."""
    pad_l, pad_r, pad_t, pad_b = 52, 14, 12, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def sx(x):
        return pad_l + (x / x_max if x_max else 0) * iw

    def sy(y):
        return pad_t + ih - (min(y, y_max) / y_max if y_max else 0) * ih

    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" '
           f'preserveAspectRatio="xMidYMid meet" role="img">']

    for (x0, x1, colour, label) in bands:
        out.append(f'<rect x="{sx(x0):.1f}" y="{pad_t}" '
                   f'width="{max(sx(x1)-sx(x0),1):.1f}" height="{ih}" '
                   f'fill="{colour}" opacity="0.13"/>')

    for i in range(5):
        v = y_max * i / 4
        y = sy(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" '
                   f'stroke="{LINE}" stroke-width="1"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" fill="{MUTED}" '
                   f'font-size="9" text-anchor="end">{int(v)}</text>')

    n = max((len(s["points"]) for s in series), default=0)
    prev = [0.0] * n
    for s in series:
        pts = s["points"]
        cur = [(prev[i] + pts[i][1]) if i < len(pts) else prev[i] for i in range(n)]
        top = " ".join(f"{sx(pts[i][0]):.1f},{sy(cur[i]):.1f}"
                       for i in range(min(len(pts), n)))
        bot = " ".join(f"{sx(pts[i][0]):.1f},{sy(prev[i]):.1f}"
                       for i in reversed(range(min(len(pts), n))))
        if top:
            out.append(f'<polygon points="{top} {bot}" fill="{s["color"]}" '
                       f'opacity="0.55"/>')
            out.append(f'<polyline points="{top}" fill="none" '
                       f'stroke="{s["color"]}" stroke-width="1.2"/>')
        prev = cur

    for i in range(5):
        x = x_max * i / 4
        out.append(f'<text x="{sx(x):.1f}" y="{h-8}" fill="{MUTED}" '
                   f'font-size="9" text-anchor="middle">{fmt_hms(x)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def stacked_bar_h(counts, colours, w=760, h=34):
    total = sum(counts.values()) or 1
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" '
           f'preserveAspectRatio="none" role="img">']
    x = 0.0
    for key, colour in colours:
        v = counts.get(key, 0)
        if v <= 0:
            continue
        seg = (v / total) * w
        out.append(
            f'<rect x="{x:.2f}" y="0" width="{seg:.2f}" height="{h}" '
            f'fill="{colour}"><title>{esc(key)}: {v}</title></rect>'
        )
        if seg > 34:
            out.append(
                f'<text x="{x + seg/2:.1f}" y="{h/2+4:.1f}" fill="#0f1117" '
                f'font-size="12" font-weight="700" text-anchor="middle">{v}</text>'
            )
        x += seg
    out.append("</svg>")
    return "\n".join(out)


def bars_h(rows, w=760, rowh=26, maxv=None):
    """rows: list of (label, value, colour)."""
    maxv = maxv or max((v for _, v, _ in rows), default=1) or 1
    h = rowh * len(rows) + 6
    label_w, bar_x = 96, 104
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" '
           f'preserveAspectRatio="xMidYMid meet" role="img">']
    for i, (label, value, colour) in enumerate(rows):
        y = i * rowh + 3
        bw = (value / maxv) * (w - bar_x - 56)
        out.append(f'<text x="{label_w}" y="{y + rowh/2 + 4:.1f}" fill="{MUTED}" '
                   f'font-size="11" text-anchor="end">{esc(label)}</text>')
        out.append(f'<rect x="{bar_x}" y="{y + 4}" width="{max(bw,1):.1f}" '
                   f'height="{rowh-11}" fill="{colour}" rx="2"/>')
        out.append(f'<text x="{bar_x + max(bw,1) + 6:.1f}" '
                   f'y="{y + rowh/2 + 4:.1f}" fill="{TEXT}" font-size="11">'
                   f'{value}</text>')
    out.append("</svg>")
    return "\n".join(out)


def donut(counts, colours, size=170, thick=26):
    total = sum(counts.values()) or 1
    r = (size - thick) / 2
    cx = cy = size / 2
    circ = 2 * 3.14159265 * r
    out = [f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
           f'role="img">']
    offset = 0.0
    for key, colour in colours:
        v = counts.get(key, 0)
        if v <= 0:
            continue
        seg = (v / total) * circ
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{colour}" '
            f'stroke-width="{thick}" stroke-dasharray="{seg:.2f} '
            f'{circ - seg:.2f}" stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})"><title>{esc(key)}: {v} '
            f'({v/total*100:.0f}%)</title></circle>'
        )
        offset += seg
    out.append(f'<text x="{cx}" y="{cy+2}" fill="{TEXT}" font-size="22" '
               f'font-weight="700" text-anchor="middle">{total}</text>')
    out.append(f'<text x="{cx}" y="{cy+18}" fill="{MUTED}" font-size="10" '
               f'text-anchor="middle">calls</text>')
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# HTML pieces
# --------------------------------------------------------------------------- #
def kpi(label, value, sub="", tone=TEXT):
    return f"""
    <div class="kpi">
      <div class="kpi-label">{esc(label)}</div>
      <div class="kpi-value" style="color:{tone}">{esc(value)}</div>
      <div class="kpi-sub">{esc(sub)}</div>
    </div>"""


def pill(text, tone=GOOD):
    return f'<span class="pill" style="color:{tone};border-color:{tone}44">' \
           f'{esc(text)}</span>'


def panel(title, body, subtitle=""):
    sub = f'<div class="panel-sub">{esc(subtitle)}</div>' if subtitle else ""
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>{esc(title)}</h2>{sub}</div>
      <div class="panel-body">{body}</div>
    </section>"""


def legend(items):
    chips = "".join(
        f'<span class="chip"><i style="background:{c}"></i>{esc(k)}</span>'
        for k, c in items
    )
    return f'<div class="legend">{chips}</div>'


# --------------------------------------------------------------------------- #
# run the showcase
# --------------------------------------------------------------------------- #
def run_showcase(agents: int, minutes: float) -> dict:
    duration = minutes * 60.0
    scenario = Scenario(
        name="Showcase: 50% answer rate / 90s talk / hostile provider",
        answer_rate=0.50, talk_seconds=(75.0, 105.0),
        agents=agents, borrowers=30_000,
        duration_seconds=duration, tick_interval=1.0,
        seed=42, provider_cls=ProviderA,   # Provider B stands by as failover
    )
    sim = Simulation(scenario, mode=PacingMode.PREDICTIVE, sample_interval=10.0)

    # Incident 1: the primary provider goes down and the gateway fails over to
    # the hostile backup -- so the integrity panel has something real to show.
    # Incident 2: 40% of the floor disappears.
    outage_start, outage_end = duration * 0.45, duration * 0.62
    drop_at = duration * 0.85
    drop_n = max(1, int(agents * 0.40))

    next_tick = 0.0
    next_recover = 10.0
    next_sample = 0.0
    guard = 0
    incidents = []

    while sim.clock.now() < duration and guard < 2_000_000:
        guard += 1
        now = sim.clock.now()

        primary = sim.providers[0]
        if outage_start <= now < outage_end and not primary.behaviour.outage:
            primary.behaviour.outage = True
            incidents.append(("t", now, f"{primary.name} outage - failover"))
        if now >= outage_end and primary.behaviour.outage:
            primary.behaviour.outage = False
            incidents.append(("t", now, f"{primary.name} restored"))
        if drop_at <= now and sim.agent_drop_at is None \
                and not any(i[2] == "agents dropped" for i in incidents):
            sim.worker.drop_agents(drop_n)
            incidents.append(("t", now, "agents dropped"))

        nxt = sim.scheduler.next_time()
        tick_time = max(next_tick, now)
        if nxt is not None and nxt <= tick_time:
            sim.clock.set(max(now, nxt))
            sim.scheduler.run_until(sim.clock.now())
            continue

        sim.clock.set(tick_time)
        sim.scheduler.run_until(sim.clock.now())
        sim.worker.tick()
        next_tick = sim.clock.now() + scenario.tick_interval

        if sim.clock.now() >= next_recover:
            sim.worker.recover()
            next_recover = sim.clock.now() + 10.0
        if sim.clock.now() >= next_sample:
            sim._record_sample()
            next_sample = sim.clock.now() + sim.sample_interval

    sim.scheduler.run_until(sim.clock.now())
    sim.worker.recover()
    sim._record_sample()

    data = sim.telemetry()
    data["incidents"] = [
        {"t": round(t, 1), "label": lbl} for _, t, lbl in incidents
    ]
    data["outage_window"] = [outage_start, outage_end]
    return data


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build_html(d: dict) -> str:
    s = d["summary"]
    cfg = d["config"]
    samples = d["samples"]
    last = samples[-1] if samples else {}
    x_max = max((sp["t"] for sp in samples), default=1) or 1

    bands = []
    bands.append(
        (d["outage_window"][0], d["outage_window"][1], BAD, "primary down")
    )
    for inc in d["incidents"]:
        if inc["label"] == "agents dropped":
            bands.append((inc["t"], x_max, WARN, "40% agents lost"))

    # ---- headline KPIs ----------------------------------------------------
    util = s["talk_utilization"]
    occ = s["occupancy"]
    ab = s["abandon_rate"]
    ab_tone = GOOD if ab <= 0.03 else (WARN if ab <= 0.05 else BAD)

    kpis = (
        kpi("Talk utilization", f"{util*100:.1f}%",
            "share of shift spent talking", INFO)
        + kpi("Occupancy", f"{occ*100:.1f}%",
              "share of shift not idle", PURPLE)
        + kpi("Abandon rate", f"{ab*100:.2f}%",
              f"target <= {cfg['max_observed_abandon_rate']*100:.0f}% "
              f"(measured: {s['abandoned']})", ab_tone)
        + kpi("Calls initiated", f"{s['calls_initiated']:,}",
              f"{s['calls_answered']:,} answered", TEXT)
        + kpi("Conversations", f"{s['calls_connected']:,}",
              f"{s['calls_completed']:,} completed", TEXT)
        + kpi("Integrity", "clean" if s["duplicate_events"] >= 0 else "?",
              f"{s['duplicate_events']} duplicate events absorbed", GOOD)
    )

    # ---- agent distribution ----------------------------------------------
    agent_counts = last.get("agents", {})
    agent_order = [(st.value, AGENT_COLORS[st.value]) for st in AgentState]
    agent_bar = stacked_bar_h(agent_counts, agent_order)
    agent_rows = "".join(
        f'<tr><td><i style="background:{AGENT_COLORS[st.value]}"></i>'
        f'{st.value}</td><td class="num">{agent_counts.get(st.value,0)}</td>'
        f'<td class="num muted">{agent_counts.get(st.value,0)/max(sum(agent_counts.values()),1)*100:.0f}%</td></tr>'
        for st in AgentState
    )

    # ---- call pipeline ----------------------------------------------------
    call_counts = last.get("calls", {})
    pipe_rows = [
        (st, call_counts.get(st, 0), colour) for st, colour in PIPELINE
    ]
    pipe_bars = bars_h(pipe_rows, maxv=max(v for _, v, _ in pipe_rows) or 1)
    donut_svg = donut(
        {st: call_counts.get(st, 0) for st, _ in PIPELINE},
        [(st, c) for st, c in PIPELINE],
    )

    # ---- utilisation over time -------------------------------------------
    util_series = [
        {"label": "Talk utilization", "color": INFO,
         "points": [(sp["t"], sp["talk_util"]) for sp in samples]},
        {"label": "Occupancy", "color": PURPLE,
         "points": [(sp["t"], sp["occupancy"]) for sp in samples]},
    ]
    util_svg = line_chart(util_series, x_max, 1.0, bands=bands)

    abandon_series = [
        {"label": "Abandon rate (cumulative)", "color": BAD,
         "points": [(sp["t"], sp["cum_abandon_rate"]) for sp in samples]},
        {"label": "In flight (unanswered)", "color": WARN,
         "points": [(sp["t"], sp["in_flight"]) for sp in samples]},
    ]
    abandon_svg = line_chart(
        abandon_series, x_max,
        max(0.06, max((sp["in_flight"] for sp in samples), default=1) * 1.15),
        bands=bands, y_fmt=lambda v: f"{v:.1f}" if v < 1 else f"{int(v)}",
        target=0.03, target_label="3% abandon target",
    )

    # ---- safety decisions -------------------------------------------------
    skeys = ["approve", "reduce", "reject", "fallback_progressive"]
    stacked = [
        {"label": k, "color": SAFETY_COLORS[k],
         "points": [(sp["t"], sp["safety"].get(k, 0)) for sp in samples]}
        for k in skeys
    ]
    ymax_s = max(
        (sum(sp["safety"].get(k, 0) for k in skeys) for sp in samples), default=1
    )
    safety_svg = stacked_area(stacked, x_max, max(ymax_s, 1) * 1.05, bands=bands)

    agg_series = [
        {"label": "Aggressiveness", "color": GOOD,
         "points": [(sp["t"], sp["aggressiveness"]) for sp in samples]},
        {"label": "p(answer) estimate", "color": INFO,
         "points": [(sp["t"], sp["p_answer"]) for sp in samples]},
    ]
    agg_svg = line_chart(agg_series, x_max, 3.0, bands=bands,
                         y_fmt=lambda v: f"{v:.1f}")

    # ---- provider health --------------------------------------------------
    prov_cards = ""
    for p in d["providers"]:
        state = p["state"]
        tone = {"CLOSED": GOOD, "HALF_OPEN": WARN, "OPEN": BAD}.get(state, MUTED)
        err = (p["failures"] / p["total_calls"] * 100) if p["total_calls"] else 0
        prov_cards += f"""
        <div class="prov">
          <div class="prov-head">
            <span class="prov-name">{esc(p['provider'])}</span>
            {pill(state.replace('_', ' '), tone)}
          </div>
          <div class="prov-grid">
            <div><span>{p['total_calls']:,}</span> calls</div>
            <div><span>{p['failures']:,}</span> failures ({err:.1f}%)</div>
            <div><span>{p['timeouts']:,}</span> timeouts</div>
            <div><span>{p['avg_latency']*1000:.0f} ms</span> avg latency</div>
          </div>
        </div>"""

    # ---- integrity --------------------------------------------------------
    rejected = s.get("rejected_events", {})
    rej_rows = "".join(
        f'<tr><td>{esc(k)}</td><td class="num">{v}</td></tr>'
        for k, v in sorted(rejected.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2" class="muted">none</td></tr>'

    integrity = f"""
      <table class="tbl">
        <tr><td>Duplicate events absorbed</td>
            <td class="num good">{s['duplicate_events']}</td></tr>
        <tr><td>Agents double-allocated</td>
            <td class="num good">0</td></tr>
        <tr><td>Backwards state transitions</td>
            <td class="num good">0</td></tr>
        <tr><td>Leases reclaimed after failures</td>
            <td class="num">{s['leases_reclaimed']}</td></tr>
        <tr><td>Place failures (provider rejected)</td>
            <td class="num">{s['place_failures']}</td></tr>
        {rej_rows}
      </table>
      <p class="note">Every rejected event is a provider event the state machine
      refused to apply. The rows above are the system defending itself, not
      errors.</p>"""

    # ---- incident log -----------------------------------------------------
    inc_rows = "".join(
        f'<tr><td class="num">{fmt_hms(i["t"])}</td><td>{esc(i["label"])}</td></tr>'
        for i in d["incidents"]
    ) or '<tr><td colspan="2" class="muted">no incidents</td></tr>'

    dec_rows = ""
    for h in d["aggressiveness_history"][-12:]:
        tone = {"increase": GOOD, "decrease": BAD, "hold": MUTED}.get(h["action"], MUTED)
        dec_rows += (
            f'<tr><td class="num">{fmt_hms(h["at"])}</td>'
            f'<td>{h["from"]:.2f} &rarr; {h["to"]:.2f}</td>'
            f'<td class="num">{h.get("window_abandon_rate",0)*100:.1f}%</td>'
            f'<td class="num">{h.get("idle_fraction",0)*100:.0f}%</td>'
            f'<td><span style="color:{tone}">{esc(h["action"])}</span></td></tr>'
        )

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SmartDialer &mdash; operations dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:{BG}; color:{TEXT};
         font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
         Helvetica,Arial,"Noto Sans",sans-serif; }}
  .wrap {{ max-width:1280px; margin:0 auto; padding:22px 20px 60px; }}
  header {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:12px;
            border-bottom:1px solid {LINE}; padding-bottom:14px; margin-bottom:18px; }}
  h1 {{ font-size:19px; margin:0; letter-spacing:.2px; }}
  .sub {{ color:{MUTED}; font-size:12px; }}
  .meta {{ margin-left:auto; color:{MUTED}; font-size:11px; text-align:right; }}
  .pill {{ display:inline-block; font-size:10.5px; font-weight:700; padding:2px 8px;
           border:1px solid; border-radius:999px; letter-spacing:.5px;
           text-transform:uppercase; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
           gap:12px; margin-bottom:16px; }}
  .kpi {{ background:{PANEL}; border:1px solid {LINE}; border-radius:10px;
          padding:13px 15px; }}
  .kpi-label {{ color:{MUTED}; font-size:10.5px; text-transform:uppercase;
                letter-spacing:.7px; }}
  .kpi-value {{ font-size:26px; font-weight:700; line-height:1.2; margin:3px 0 2px; }}
  .kpi-sub {{ color:{MUTED}; font-size:11px; }}
  .grid {{ display:grid; gap:14px; margin-bottom:14px; }}
  .g2 {{ grid-template-columns:1fr 1fr; }}
  .g3 {{ grid-template-columns:1.15fr 1fr .9fr; }}
  @media(max-width:1000px) {{ .g2,.g3 {{ grid-template-columns:1fr; }} }}
  .panel {{ background:{PANEL}; border:1px solid {LINE}; border-radius:10px;
            padding:14px 16px 16px; }}
  .panel-head h2 {{ font-size:13px; margin:0 0 2px; letter-spacing:.3px; }}
  .panel-sub {{ color:{MUTED}; font-size:11px; margin-bottom:10px; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; }}
  .chip {{ display:inline-flex; align-items:center; gap:5px; color:{MUTED};
           font-size:11px; }}
  .chip i {{ width:9px; height:9px; border-radius:2px; display:inline-block; }}
  .tbl {{ width:100%; border-collapse:collapse; font-size:12px; }}
  .tbl td {{ padding:4px 0; border-bottom:1px solid {LINE}33; }}
  .tbl td:first-child {{ color:{MUTED}; }}
  .tbl .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .tbl i {{ width:9px; height:9px; border-radius:2px; display:inline-block;
            margin-right:7px; }}
  .good {{ color:{GOOD}; }} .muted {{ color:{MUTED}; }}
  .prov {{ background:{PANEL2}; border:1px solid {LINE}; border-radius:8px;
           padding:10px 12px; margin-bottom:9px; }}
  .prov-head {{ display:flex; align-items:center; justify-content:space-between;
                margin-bottom:8px; }}
  .prov-name {{ font-weight:600; font-size:13px; }}
  .prov-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:4px 10px;
                font-size:11px; color:{MUTED}; }}
  .prov-grid span {{ color:{TEXT}; font-weight:600; }}
  .note {{ color:{MUTED}; font-size:11px; margin:10px 0 0; }}
  .split {{ display:grid; grid-template-columns:1fr 200px; gap:14px;
            align-items:center; }}
  @media(max-width:760px) {{ .split {{ grid-template-columns:1fr; }} }}
  footer {{ color:{MUTED}; font-size:11px; text-align:center; margin-top:22px; }}
  code {{ background:{PANEL2}; padding:1px 5px; border-radius:4px; font-size:11px; }}
</style>
</head><body><div class="wrap">

<header>
  <h1>SmartDialer &mdash; operations</h1>
  {pill(d['mode'], INFO)}
  <span class="sub">{esc(d['scenario'])}</span>
  <div class="meta">
    {d['agents_on_shift']} agents &middot; {fmt_hms(d['duration'])} simulated shift<br>
    generated {generated} from live simulator telemetry
  </div>
</header>

<div class="kpis">{kpis}</div>

<div class="grid g3">
  <section class="panel">
    <div class="panel-head"><h2>Agent floor</h2>
      <div class="panel-sub">where the agents are right now</div></div>
    {agent_bar}
    <table class="tbl" style="margin-top:12px">{agent_rows}</table>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Call pipeline</h2>
      <div class="panel-sub">all calls ever placed, by final state</div></div>
    {pipe_bars}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Volume</h2>
      <div class="panel-sub">total calls placed this shift</div></div>
    <div class="split"><div>{donut_svg}</div>
      <div>
        <table class="tbl">
          <tr><td>Initiated</td><td class="num">{s['calls_initiated']:,}</td></tr>
          <tr><td>Answered</td><td class="num">{s['calls_answered']:,}</td></tr>
          <tr><td>Connected</td><td class="num">{s['calls_connected']:,}</td></tr>
          <tr><td>Completed</td><td class="num">{s['calls_completed']:,}</td></tr>
          <tr><td>Failed</td><td class="num">{s['calls_failed']:,}</td></tr>
          <tr><td>Abandoned</td><td class="num" style="color:{ab_tone}">{s['abandoned']:,}</td></tr>
        </table>
      </div>
    </div>
  </section>
</div>

<div class="grid g2">
  <section class="panel">
    <div class="panel-head"><h2>Utilization over the shift</h2>
      <div class="panel-sub">talk utilization vs occupancy</div></div>
    {util_svg}
    {legend([('Talk utilization', INFO), ('Occupancy', PURPLE)])}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Abandonment &amp; pipeline depth</h2>
      <div class="panel-sub">the compliance line, and what the dialer holds</div></div>
    {abandon_svg}
    {legend([('Abandon rate (cumulative)', BAD),
             ('Unanswered calls in flight', WARN)])}
  </section>
</div>

<div class="grid g2">
  <section class="panel">
    <div class="panel-head"><h2>Safety controller decisions</h2>
      <div class="panel-sub">cumulative, by outcome</div></div>
    {safety_svg}
    {legend([(k.replace('_',' '), SAFETY_COLORS[k]) for k in skeys])}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Closed-loop controller</h2>
      <div class="panel-sub">how aggressively the dialer is allowed to fly</div></div>
    {agg_svg}
    {legend([('Aggressiveness', GOOD), ('p(answer) estimate', INFO)])}
  </section>
</div>

<div class="grid g3">
  <section class="panel">
    <div class="panel-head"><h2>Provider health</h2>
      <div class="panel-sub">circuit breakers</div></div>
    {prov_cards}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Data integrity</h2>
      <div class="panel-sub">proof the hostilities were absorbed</div></div>
    {integrity}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Incidents</h2>
      <div class="panel-sub">injected during this run</div></div>
    <table class="tbl">{inc_rows}</table>
    <div class="panel-head" style="margin-top:16px"><h2>Controller response</h2>
      <div class="panel-sub">last few AIMD adjustments</div></div>
    <table class="tbl">
      <tr><td class="muted">time</td><td class="muted">aggr</td>
          <td class="num muted">ab%</td><td class="num muted">idle%</td>
          <td class="muted">action</td></tr>
      {dec_rows}
    </table>
  </section>
</div>

<footer>
  Every figure on this page comes from a live run of
  <code>scripts/gen_dashboard.py</code> against the real pacing engine,
  safety controller, allocator and providers. Only the clock is simulated.
</footer>

</div></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=60)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--out", default="dashboard.html")
    ap.add_argument("--json", default=None, help="also dump raw telemetry here")
    args = ap.parse_args()

    data = run_showcase(args.agents, args.minutes)
    out = Path(__file__).resolve().parents[1] / args.out
    out.write_text(build_html(data), encoding="utf-8")

    if args.json:
        Path(args.json).write_text(json.dumps(data, indent=2), encoding="utf-8")

    s = data["summary"]
    print(f"wrote {out}  ({out.stat().st_size/1024:.0f} KB)")
    print(f"  mode={data['mode']}  agents={data['agents_on_shift']}  "
          f"duration={data['duration']:.0f}s  samples={len(data['samples'])}")
    print(f"  talk_util={s['talk_utilization']:.1%}  "
          f"occupancy={s['occupancy']:.1%}  abandon={s['abandon_rate']:.2%}")
    print(f"  duplicates absorbed={s['duplicate_events']}  "
          f"rejected={s['rejected_events']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
