"""The dashboard's look: page shell, CSS, the small script that draws charts, and
HTML helpers. No third-party code, fonts or network requests: everything the
browser needs is served from this file.

Charts are drawn in the browser from JSON the server puts in the page, so they
fit a phone as well as a desktop, and each one carries a plain table of the same
numbers (the "Table" disclosure under it) so nothing depends on colour or hover.
Colours are a validated palette (blue, orange, aqua, yellow, magenta in that
fixed order; blue/red for gain/loss), checked for colour-blind separation on
both the light and the dark surface.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from typing import Any

NAV = (("/", "Overview"), ("/edge", "Edge"), ("/wallets", "Wallets"), ("/trades", "Trades"),
       ("/breakdowns", "Breakdowns"), ("/health", "Health"))


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(x: float | None) -> str:
    return "-" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.1%}"


def cents(x: float | None) -> str:
    return "-" if x is None else f"{x:+.3f}"


def tone(x: float | None) -> str:
    return "" if x is None or x == 0 else ("up" if x > 0 else "down")


def tile(label: str, value: str, sub: str = "", tone_: str = "") -> str:
    return (f'<div class="tile"><div class="tile-label">{esc(label)}</div>'
            f'<div class="tile-value {tone_}">{esc(value)}</div><div class="tile-sub">{esc(sub)}</div></div>')


def card(title: str, body: str, *, note: str = "", wide: bool = False) -> str:
    note_html = f'<p class="card-note">{esc(note)}</p>' if note else ""
    return (f'<section class="card{" wide" if wide else ""}"><h2>{esc(title)}</h2>{note_html}{body}</section>')


def banner(text: str, kind: str = "warn") -> str:
    icon = {"warn": "!", "info": "i", "bad": "×", "good": "✓"}[kind]
    return f'<div class="banner {kind}"><span class="banner-icon" aria-hidden="true">{icon}</span>{esc(text)}</div>'


Cell = Any      # str | (display, sort value) | (display, sort value, css class)


def table(headers: Sequence[str | tuple[str, str]], rows: Sequence[Sequence[Cell]], *, sortable: bool = True,
          empty: str = "Nothing in this range.", numeric_from: int = 1) -> str:
    """``headers``: text, or (text, tooltip). A cell is text, or (text, sort value[, class]).
    Cell text is escaped unless it is a ``Raw``."""
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = []
    for i, h in enumerate(headers):
        text, tip = (h, "") if isinstance(h, str) else h
        cls = "num" if i >= numeric_from else ""
        head.append(f'<th class="{cls}"{f" title={chr(34)}{esc(tip)}{chr(34)}" if tip else ""}>{esc(text)}</th>')
    body = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            text, sort, cls = (c, None, "") if not isinstance(c, tuple) else (c + (None, ""))[:3] if len(c) < 3 else c
            classes = " ".join(x for x in ("num" if i >= numeric_from else "", cls or "") if x)
            sort_attr = "" if sort is None else f' data-sort="{esc(sort)}"'
            shown = text if isinstance(text, Raw) else esc(text)
            cells.append(f'<td class="{classes}"{sort_attr}>{shown}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class="table-wrap"><table class="{"sortable" if sortable else ""}"><thead><tr>'
            + "".join(head) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>")


class Raw(str):
    """Markup that is already safe to put in the page as is."""


def link(href: str, text: str, cls: str = "") -> Raw:
    return Raw(f'<a href="{esc(href)}"{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>{esc(text)}</a>')


def chart(kind: str, data: dict[str, Any], *, table_headers: Sequence[str], table_rows: Sequence[Sequence[Cell]],
          legend: Sequence[tuple[str, str]] = (), empty: str = "No data in this range yet.") -> str:
    """A chart drawn by app.js from ``data``, its legend, and the same numbers as a table."""
    if not table_rows:
        return f'<p class="empty">{esc(empty)}</p>'
    legend_html = ""
    if len(legend) >= 2:
        legend_html = '<div class="legend">' + "".join(
            f'<span class="key"><span class="swatch {esc(cls)}"></span>{esc(name)}</span>' for name, cls in legend) + "</div>"
    return (f'{legend_html}<div class="chart" data-kind="{esc(kind)}" data-chart="{esc(json.dumps(data))}"></div>'
            f'<details class="chart-table"><summary>Table</summary>{table(table_headers, table_rows, sortable=False)}'
            "</details>")


def hbar(value: float, scale: float, cls: str = "") -> Raw:
    """A small signed bar for a table cell: right of centre for a gain, left for a loss."""
    if not scale:
        return Raw("")
    w = min(50.0, abs(value) / scale * 50.0)
    x = 50.0 if value >= 0 else 50.0 - w
    kind = cls or ("pos" if value >= 0 else "neg")
    return Raw(f'<svg class="hbar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">'
               f'<rect class="hbar-axis" x="49.7" y="0" width="0.6" height="8"/>'
               f'<rect class="{kind}" x="{x:.2f}" y="1" width="{w:.2f}" height="6" rx="1"/></svg>')


def page(title: str, active: str, filters_html: str, body: str, *, qs: str = "", updated: str = "") -> str:
    nav = "".join(f'<a href="{href}{("?" + qs) if qs else ""}" class="{"on" if href == active else ""}">{name}</a>'
                  for href, name in NAV)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{esc(title)} · Whale Watcher</title>
<link rel="stylesheet" href="/assets/app.css">
</head><body>
<header class="top"><div class="brand"><span class="logo" aria-hidden="true"></span>Whale Watcher
<span class="tag">paper trading</span></div><nav>{nav}</nav></header>
<main>
{filters_html}
{body}
<footer>Paper trading research. No orders are placed and this page only reads the database. {esc(updated)}</footer>
</main>
<div id="tip" class="tip" hidden></div>
<script src="/assets/app.js"></script>
</body></html>"""


CSS = """
:root{color-scheme:light;--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--wash:rgba(11,11,11,.04);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--pos:#2a78d6;--neg:#e34948;
--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;--up:#006300;--down:#b42c2c;--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--plane:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;
--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--wash:rgba(255,255,255,.05);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--pos:#3987e5;
--neg:#e66767;--up:#0ca30c;--down:#e66767;--accent:#3987e5}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--plane);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.top{position:sticky;top:0;z-index:5;background:var(--surface);border-bottom:1px solid var(--border);
display:flex;align-items:center;gap:24px;padding:0 20px;min-height:52px}
.brand{font-weight:650;letter-spacing:-.01em;display:flex;align-items:center;gap:9px;white-space:nowrap}
.logo{width:18px;height:18px;border-radius:6px 6px 6px 2px;background:var(--accent);display:inline-block}
.tag{font-weight:500;font-size:11px;color:var(--ink2);border:1px solid var(--border);border-radius:99px;padding:1px 8px}
nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;margin-left:auto}
nav::-webkit-scrollbar{display:none}
nav a{color:var(--ink2);padding:15px 12px 13px;border-bottom:2px solid transparent;white-space:nowrap;font-weight:500}
nav a:hover{color:var(--ink);text-decoration:none}
nav a.on{color:var(--ink);border-bottom-color:var(--accent)}
main{max-width:1180px;margin:0 auto;padding:18px 20px 40px}
.filters{display:flex;flex-wrap:wrap;gap:10px 12px;align-items:end;margin:0 0 16px}
.filters label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--ink2);font-weight:500;
text-transform:uppercase;letter-spacing:.04em}
.filters select,.filters input[type=date]{font:inherit;font-size:14px;color:var(--ink);background:var(--surface);
border:1px solid var(--border);border-radius:8px;padding:6px 9px;min-height:36px;text-transform:none;letter-spacing:0}
.filters .check{flex-direction:row;align-items:center;gap:7px;min-height:36px;text-transform:none;letter-spacing:0;
font-size:14px;color:var(--ink)}
.filters button{font:inherit;font-weight:600;font-size:14px;color:#fff;background:var(--accent);border:0;
border-radius:8px;padding:0 16px;min-height:36px;cursor:pointer}
.presets{display:flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--surface)}
.presets a{padding:7px 12px;color:var(--ink2);font-size:14px;font-weight:500;border-right:1px solid var(--border);
text-transform:none;letter-spacing:0;white-space:nowrap}
.presets a:last-child{border-right:0}.presets a:hover{background:var(--wash);text-decoration:none}
.presets a.on{background:var(--accent);color:#fff}
.banner{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--border);border-radius:10px;
padding:10px 14px;margin:0 0 16px;background:var(--surface);font-size:14px}
.banner-icon{flex:none;width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font-weight:700;
font-size:12px;color:#fff;background:var(--muted)}
.banner.warn .banner-icon{background:var(--warning);color:#0b0b0b}.banner.bad .banner-icon{background:var(--critical)}
.banner.good .banner-icon{background:var(--good)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.tile-label{font-size:12px;color:var(--ink2)}
.tile-value{font-size:24px;font-weight:650;letter-spacing:-.02em;margin:2px 0 1px}
.tile-sub{font-size:12px;color:var(--muted)}
.up{color:var(--up)}.down{color:var(--down)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px 12px;min-width:0}
.card.wide{grid-column:1/-1}
.card h2{font-size:15px;font-weight:650;margin:0 0 2px}
.card-note{margin:0 0 8px;color:var(--ink2);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--ink2);margin:6px 0 2px}
.key{display:inline-flex;align-items:center;gap:6px}
.swatch{width:14px;height:3px;border-radius:2px;display:inline-block;background:var(--muted)}
.swatch.s1,.hbar .s1{background:var(--s1);fill:var(--s1)}.swatch.s2{background:var(--s2)}.swatch.s3{background:var(--s3)}
.swatch.s4{background:var(--s4)}.swatch.s5{background:var(--s5)}.swatch.pos{background:var(--pos)}
.swatch.neg{background:var(--neg)}
.chart{width:100%;min-height:60px;margin-top:6px;position:relative}
.chart svg{display:block;width:100%;overflow:visible}
.chart text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.chart .gridline{stroke:var(--grid);stroke-width:1}.chart .axisline{stroke:var(--axis);stroke-width:1}
.chart .band{fill:var(--muted);opacity:.14}
.chart .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.chart .dot{stroke:var(--surface);stroke-width:2}
.chart .s1{stroke:var(--s1);fill:var(--s1)}.chart .s2{stroke:var(--s2);fill:var(--s2)}
.chart .s3{stroke:var(--s3);fill:var(--s3)}.chart .s4{stroke:var(--s4);fill:var(--s4)}
.chart .s5{stroke:var(--s5);fill:var(--s5)}
.chart .line.s1,.chart .line.s2,.chart .line.s3,.chart .line.s4,.chart .line.s5{fill:none}
.chart .bar{stroke:none}.chart .bar.pos{fill:var(--pos)}.chart .bar.neg{fill:var(--neg)}
.chart .bar.s1{fill:var(--s1)}.chart .bar.s3{fill:var(--s3)}
.chart .bar,.chart .line,.chart .dot,.chart .ref,.chart .band{pointer-events:none}
.chart .hit{fill:transparent;stroke:none;outline:none}.chart .hit:hover+.bar,.chart .bar.hover{opacity:.75}
.chart .cross{stroke:var(--axis);stroke-width:1}
.chart .ref{stroke:var(--ink2);stroke-width:1}
.chart-table{margin-top:6px;font-size:12px}.chart-table summary{cursor:pointer;color:var(--ink2);width:max-content}
.table-wrap{overflow-x:auto;margin:6px -4px 0}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--grid);white-space:nowrap}
th{font-weight:600;color:var(--ink2);font-size:12px;position:relative}
table.sortable th{cursor:pointer;user-select:none}
table.sortable th[aria-sort=ascending]::after{content:" ↑"}table.sortable th[aria-sort=descending]::after{content:" ↓"}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.wrap{white-space:normal;min-width:180px;max-width:420px}
tbody tr:hover{background:var(--wash)}
.pill{display:inline-block;border-radius:99px;padding:1px 8px;font-size:11px;font-weight:600;border:1px solid var(--border);
color:var(--ink2)}
.pill.good{color:var(--up)}.pill.bad{color:var(--down)}
.hbar{width:120px;height:8px;display:block}.hbar .pos{fill:var(--pos)}.hbar .neg{fill:var(--neg)}
.hbar .hbar-axis{fill:var(--axis)}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:5px 16px;font-size:14px;margin:6px 0 0}
.kv dt{color:var(--ink2)}.kv dd{margin:0;font-variant-numeric:tabular-nums}
.empty{color:var(--muted);font-size:14px;margin:10px 0 6px}
.pager{display:flex;gap:12px;align-items:center;font-size:13px;color:var(--ink2);margin-top:10px}
.tip{position:fixed;z-index:20;pointer-events:none;background:var(--ink);color:var(--plane);border-radius:8px;
padding:7px 10px;font-size:12px;line-height:1.5;box-shadow:0 4px 14px rgba(0,0,0,.18);max-width:260px}
.tip b{font-weight:650}.tip .tk{display:inline-block;width:10px;height:2px;border-radius:1px;margin-right:6px;vertical-align:middle}
.tip .tk.s1{background:var(--s1)}.tip .tk.s2{background:var(--s2)}.tip .tk.s3{background:var(--s3)}
.tip .tk.s4{background:var(--s4)}.tip .tk.s5{background:var(--s5)}.tip .tl{opacity:.75}
footer{color:var(--muted);font-size:12px;margin-top:22px}
@media (max-width:760px){.top{flex-wrap:wrap;gap:0 12px;padding:8px 14px 0}.brand{padding:4px 0}
nav{margin-left:-6px;width:100%}nav a{padding:9px 10px 8px}main{padding:14px 12px 32px}
.grid{grid-template-columns:minmax(0,1fr)}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}
.tile-value{font-size:20px}.filters label{flex:1 1 44%}.filters .check,.filters button{flex:1 1 100%}
.presets{width:100%}.presets a{flex:1;text-align:center}}
"""


JS = r"""
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var tip = document.getElementById("tip");
  function el(name, attrs, parent) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function fmt(v, kind) {
    if (v === null || v === undefined || isNaN(v)) return "-";
    var a = Math.abs(v);
    if (kind === "usd") return (v < 0 ? "-" : "") + "$" + (a >= 10000 ? (a / 1000).toFixed(1) + "K" : a.toFixed(a >= 100 ? 0 : 2));
    if (kind === "pct") return (v * 100).toFixed(1) + "%";
    if (kind === "cents") return (v >= 0 ? "+" : "") + v.toFixed(3);
    if (kind === "sec") return v.toFixed(1) + "s";
    return a >= 100 ? v.toFixed(0) : String(Math.round(v * 100) / 100);
  }
  function ticks(lo, hi, n) {
    if (lo === hi) { lo -= 1; hi += 1; }
    var span = hi - lo, step = Math.pow(10, Math.floor(Math.log10(span / n)));
    var err = span / n / step;
    step *= err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
    var out = [], t = Math.ceil(lo / step) * step;
    for (; t <= hi + step * 1e-9; t += step) out.push(Math.abs(t) < step * 1e-9 ? 0 : t);
    return out;
  }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function day(ts) { var d = new Date(ts * 1000); return MONTHS[d.getUTCMonth()] + " " + d.getUTCDate(); }
  function showTip(evt, lines) {
    while (tip.firstChild) tip.removeChild(tip.firstChild);
    lines.forEach(function (l) {
      var row = document.createElement("div");
      if (l.cls) { var k = document.createElement("span"); k.className = "tk " + l.cls; row.appendChild(k); }
      if (l.value !== undefined) { var b = document.createElement("b"); b.textContent = l.value + " "; row.appendChild(b); }
      var s = document.createElement("span"); s.className = "tl"; s.textContent = l.label; row.appendChild(s);
      tip.appendChild(row);
    });
    tip.hidden = false;
    var x = evt.clientX + 14, y = evt.clientY + 14, r = tip.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - 14;
    if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - 14;
    tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
  }
  function hideTip() { tip.hidden = true; }

  function frame(box, d, lo, hi) {
    var W = Math.max(260, box.clientWidth), H = W < 480 ? 200 : 240;
    var m = { l: 46, r: 14, t: 10, b: 24 };
    while (box.firstChild) box.removeChild(box.firstChild);
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, height: H, role: "img" }, box);
    if (d.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    var ts = ticks(lo, hi, 4);
    lo = Math.min(lo, ts[0]); hi = Math.max(hi, ts[ts.length - 1]);
    if (lo === hi) { lo -= 1; hi += 1; }
    var y = function (v) { return m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo)); };
    ts.forEach(function (t) {
      el("line", { class: t === 0 ? "axisline" : "gridline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }, svg);
      var tx = el("text", { x: m.l - 6, y: y(t) + 4, "text-anchor": "end" }, svg);
      tx.textContent = d.y === "usd" && Math.abs(t) < 10000 && t === Math.round(t) ? (t < 0 ? "-$" : "$") + Math.abs(t) : fmt(t, d.y);
    });
    return { svg: svg, W: W, H: H, m: m, y: y, lo: lo, hi: hi };
  }

  function renderLine(box, d) {
    var all = [];
    d.series.forEach(function (s) { s.points.forEach(function (p) { all.push(p); }); });
    if (!all.length) return;
    var t0 = Math.min.apply(null, all.map(function (p) { return p[0]; })), t1 = Math.max.apply(null, all.map(function (p) { return p[0]; }));
    if (t0 === t1) { t0 -= 43200; t1 += 43200; }
    var f = frame(box, d, Math.min.apply(null, all.map(function (p) { return p[1]; })), Math.max.apply(null, all.map(function (p) { return p[1]; })));
    var x = function (t) { return f.m.l + (f.W - f.m.l - f.m.r) * (t - t0) / (t1 - t0); };
    (d.bands || []).forEach(function (b) {
      var a = Math.max(b[0], t0), z = Math.min(b[1], t1);
      if (z > a) { var r = el("rect", { class: "band", x: x(a), y: f.m.t, width: Math.max(2, x(z) - x(a)), height: f.H - f.m.t - f.m.b }, f.svg); el("title", {}, r).textContent = "Outage window"; }
    });
    var n = f.W < 480 ? 3 : 5;
    for (var i = 0; i <= n; i++) {
      var t = t0 + (t1 - t0) * i / n;
      var tx = el("text", { x: x(t), y: f.H - 6, "text-anchor": i === 0 ? "start" : i === n ? "end" : "middle" }, f.svg); tx.textContent = day(t);
    }
    d.series.forEach(function (s) {
      if (!s.points.length) return;
      var path = s.points.map(function (p, i) { return (i ? "L" : "M") + x(p[0]).toFixed(1) + " " + f.y(p[1]).toFixed(1); }).join("");
      el("path", { class: "line " + s.cls, d: path }, f.svg);
      var last = s.points[s.points.length - 1];
      el("circle", { class: "dot " + s.cls, cx: x(last[0]), cy: f.y(last[1]), r: 4 }, f.svg);
    });
    var cross = el("line", { class: "cross", y1: f.m.t, y2: f.H - f.m.b, visibility: "hidden" }, f.svg);
    var xs = Array.from(new Set(all.map(function (p) { return p[0]; }))).sort(function (a, b) { return a - b; });
    var hit = el("rect", { class: "hit", x: f.m.l, y: f.m.t, width: f.W - f.m.l - f.m.r, height: f.H - f.m.t - f.m.b }, f.svg);
    function move(evt) {
      var r = f.svg.getBoundingClientRect(), px = (evt.clientX - r.left) * f.W / r.width;
      var t = t0 + (px - f.m.l) / (f.W - f.m.l - f.m.r) * (t1 - t0), best = xs[0];
      xs.forEach(function (v) { if (Math.abs(v - t) < Math.abs(best - t)) best = v; });
      cross.setAttribute("x1", x(best)); cross.setAttribute("x2", x(best)); cross.setAttribute("visibility", "visible");
      var lines = [{ label: day(best) }];
      d.series.forEach(function (s) {
        var v = null, extra = "";
        s.points.forEach(function (p) { if (p[0] <= best) { v = p[1]; extra = p[2] !== undefined ? " (n=" + p[2] + ")" : ""; } });
        if (v !== null) lines.push({ cls: s.cls, value: fmt(v, d.y), label: s.name + extra });
      });
      showTip(evt, lines);
    }
    hit.addEventListener("pointermove", move); hit.addEventListener("pointerdown", move);
    // On a touch screen the finger "leaves" the moment it lifts: keep the readout until the next tap.
    hit.addEventListener("pointerleave", function (evt) { if (evt.pointerType !== "mouse") return; cross.setAttribute("visibility", "hidden"); hideTip(); });
  }

  function renderBars(box, d) {
    var bars = d.bars; if (!bars.length) return;
    var vals = bars.map(function (b) { return b.v; });
    var f = frame(box, { y: d.y, zero: true }, Math.min.apply(null, vals), Math.max.apply(null, vals));
    var plot = f.W - f.m.l - f.m.r, band = plot / bars.length, w = Math.max(2, Math.min(24, band - 2));
    var every = Math.ceil(bars.length / (f.W < 480 ? 4 : 8));
    bars.forEach(function (b, i) {
      var cx = f.m.l + band * (i + 0.5), y0 = f.y(0), y1 = f.y(b.v), top = Math.min(y0, y1), h = Math.abs(y1 - y0);
      var r = Math.min(4, w / 2, h), x0 = cx - w / 2, x1 = cx + w / 2, cls = d.signed ? (b.v >= 0 ? "pos" : "neg") : (d.cls || "s1");
      var hitRect = el("rect", { class: "hit", x: f.m.l + band * i, y: f.m.t, width: band, height: f.H - f.m.t - f.m.b, tabindex: 0 }, f.svg);
      var path = b.v >= 0
        ? "M" + x0 + " " + y0 + "V" + (top + r) + "Q" + x0 + " " + top + " " + (x0 + r) + " " + top + "H" + (x1 - r) + "Q" + x1 + " " + top + " " + x1 + " " + (top + r) + "V" + y0 + "Z"
        : "M" + x0 + " " + y0 + "V" + (top + h - r) + "Q" + x0 + " " + (top + h) + " " + (x0 + r) + " " + (top + h) + "H" + (x1 - r) + "Q" + x1 + " " + (top + h) + " " + x1 + " " + (top + h - r) + "V" + y0 + "Z";
      if (h > 0.2) el("path", { class: "bar " + cls, d: path }, f.svg);
      if (i % every === 0) { var tx = el("text", { x: cx, y: f.H - 6, "text-anchor": "middle" }, f.svg); tx.textContent = b.label; }
      function show(evt) { showTip(evt, [{ value: fmt(b.v, d.y), label: b.label + (b.n !== undefined ? " (n=" + b.n + ")" : "") }]); }
      hitRect.addEventListener("pointermove", show); hitRect.addEventListener("pointerdown", show);
      hitRect.addEventListener("pointerleave", function (evt) { if (evt.pointerType === "mouse") hideTip(); });
      hitRect.addEventListener("focus", function () { var r = hitRect.getBoundingClientRect(); show({ clientX: r.left + r.width / 2, clientY: r.top + 20 }); });
      hitRect.addEventListener("blur", hideTip);
    });
    if (d.ref !== undefined && d.ref !== null) {
      var rx = f.m.l + band * d.ref;
      el("line", { class: "ref", x1: rx, x2: rx, y1: f.m.t, y2: f.H - f.m.b }, f.svg);
    }
  }

  function drawAll() {
    document.querySelectorAll(".chart").forEach(function (box) {
      var d = JSON.parse(box.getAttribute("data-chart"));
      if (box.getAttribute("data-kind") === "line") renderLine(box, d); else renderBars(box, d);
    });
  }
  document.addEventListener("pointerdown", function (evt) { if (!(evt.target.closest && evt.target.closest(".chart"))) hideTip(); });
  var timer; window.addEventListener("resize", function () { clearTimeout(timer); timer = setTimeout(drawAll, 120); });
  drawAll();

  document.querySelectorAll("table.sortable").forEach(function (table) {
    table.querySelectorAll("th").forEach(function (th, col) {
      th.addEventListener("click", function () {
        var asc = th.getAttribute("aria-sort") !== "ascending", body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        var key = function (r) { var c = r.cells[col], s = c.getAttribute("data-sort"); var v = s !== null ? s : c.textContent; var n = parseFloat(v); return s !== null && !isNaN(n) ? n : v.toLowerCase(); };
        rows.sort(function (a, b) { var x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1); });
        table.querySelectorAll("th").forEach(function (o) { o.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", asc ? "ascending" : "descending");
        rows.forEach(function (r) { body.appendChild(r); });
      });
    });
  });
  var form = document.querySelector("form.filters");
  if (form) form.querySelectorAll("select,input[type=checkbox]").forEach(function (c) { c.addEventListener("change", function () { form.submit(); }); });
})();
"""
