"""Report exporters. A report is ``sections -> blocks``; block types:
paragraph{text} · heading{text} · bullets{items} · code{text} · table{title?,columns,rows}
· chart{kind: bar|line, title, x_label, y_label, categories?, series:[{name, values|points}]}
"""
from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime
from typing import BinaryIO

from mizan.plugins.base import Exporter, ReportModel


def fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.4g}" if abs(v) >= 1000 or (v != 0 and abs(v) < 0.001) else f"{v:.3f}".rstrip("0").rstrip(".") or "0"
    return str(v)


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _md_cell(v) -> str:
    return fmt(v).replace("|", "\\|").replace("\n", " ")


class MarkdownExporter(Exporter):
    kind = "markdown"
    extension = "md"

    def export(self, report: ReportModel, out: BinaryIO) -> str:
        L = [f"# {report.title}"]
        meta = report.meta.get("header") or []
        head = []
        if report.question:
            head.append(f"**Question:** {report.question}")
        head += meta
        if report.generated_at:
            head.append(f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
        if head:
            L += ["", " · ".join(head)]
        for n, sec in enumerate(report.sections, 1):
            L += ["", f"## {n}. {sec['title']}"]
            for b in sec.get("blocks", []):
                L += [""] + self._block(b)
        out.write(("\n".join(L) + "\n").encode("utf-8"))
        return "text/markdown; charset=utf-8"

    def _block(self, b: dict) -> list[str]:
        t = b["type"]
        if t == "paragraph":
            return [b["text"]]
        if t == "heading":
            return [f"### {b['text']}"]
        if t == "bullets":
            return [f"- {i}" for i in b["items"]]
        if t == "code":
            return ["```", b["text"], "```"]
        if t == "table":
            cols = b["columns"]
            rows = [f"| {' | '.join(_md_cell(c) for c in cols)} |", f"|{'|'.join(['---'] * len(cols))}|"]
            rows += [f"| {' | '.join(_md_cell(c) for c in r)} |" for r in b["rows"]]
            return ([f"**{b['title']}**", ""] if b.get("title") else []) + rows
        if t == "chart":
            return [f"**{b['title']}** _(chart data; see the HTML export for the plot)_", ""] + self._chart_table(b)
        return []

    @staticmethod
    def _chart_table(b: dict) -> list[str]:
        if b["kind"] == "bar":
            cols = [b.get("x_label", "")] + [s["name"] for s in b["series"]]
            rows = [[c] + [s["values"][i] for s in b["series"]] for i, c in enumerate(b["categories"])]
        else:
            xs = sorted({p[0] for s in b["series"] for p in s["points"]})
            cols = [b.get("x_label", "x")] + [s["name"] for s in b["series"]]
            rows = [[x] + [dict(map(tuple, s["points"])).get(x) for s in b["series"]] for x in xs]
        return [f"| {' | '.join(_md_cell(c) for c in cols)} |", f"|{'|'.join(['---'] * len(cols))}|"] + [
            f"| {' | '.join(_md_cell(c) for c in r)} |" for r in rows]


_PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1", "#9c755f"]


def _svg_bar(b: dict) -> str:
    cats, series = b["categories"], b["series"]
    W, H, L, B = 640, 260, 48, 50
    vmax = max([v for s in series for v in s["values"] if v is not None] + [1e-9])
    gw = (W - L - 10) / max(len(cats), 1)
    bw = gw * 0.8 / max(len(series), 1)
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(b["title"])}" class="chart">']
    p.append(f'<line x1="{L}" y1="{H-B}" x2="{W-5}" y2="{H-B}" stroke="currentColor" opacity=".4"/>')
    for gi, c in enumerate(cats):
        x0 = L + gi * gw + gw * 0.1
        for si, s in enumerate(series):
            v = s["values"][gi]
            if v is None:
                continue
            h = (H - B - 20) * v / vmax
            x = x0 + si * bw
            p.append(f'<rect x="{x:.1f}" y="{H-B-h:.1f}" width="{bw-2:.1f}" height="{h:.1f}" fill="{_PALETTE[si % 8]}"><title>{html.escape(s["name"])}: {fmt(v)}</title></rect>')
            p.append(f'<text x="{x+bw/2-1:.1f}" y="{H-B-h-3:.1f}" font-size="9" text-anchor="middle" fill="currentColor">{fmt(v)}</text>')
        p.append(f'<text x="{x0+gw*0.4:.1f}" y="{H-B+14}" font-size="10" text-anchor="middle" fill="currentColor">{html.escape(str(c))}</text>')
    return "".join(p) + "</svg>"


def _svg_line(b: dict) -> str:
    W, H, L, B = 640, 260, 48, 40
    pts = [p for s in b["series"] for p in s["points"] if p[1] is not None]
    if not pts:
        return ""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y1 = min(xs), max(xs), max(ys) or 1
    sx = lambda x: L + (W - L - 10) * ((x - x0) / ((x1 - x0) or 1))  # noqa: E731
    sy = lambda y: H - B - (H - B - 15) * (y / y1)  # noqa: E731
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(b["title"])}" class="chart">',
         f'<line x1="{L}" y1="{H-B}" x2="{W-5}" y2="{H-B}" stroke="currentColor" opacity=".4"/>',
         f'<line x1="{L}" y1="{H-B}" x2="{L}" y2="10" stroke="currentColor" opacity=".4"/>',
         f'<text x="{L-4}" y="18" font-size="9" text-anchor="end" fill="currentColor">{fmt(y1)}</text>']
    for x in sorted(set(xs)):
        p.append(f'<text x="{sx(x):.1f}" y="{H-B+14}" font-size="9" text-anchor="middle" fill="currentColor">{fmt(x)}</text>')
    for si, s in enumerate(b["series"]):
        d = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in s["points"] if y is not None)
        p.append(f'<polyline fill="none" stroke="{_PALETTE[si % 8]}" stroke-width="2" points="{d}"/>')
        for x, y in s["points"]:
            if y is not None:
                p.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3" fill="{_PALETTE[si % 8]}"><title>{html.escape(s["name"])} @ {fmt(x)}: {fmt(y)}</title></circle>')
    return "".join(p) + "</svg>"


def _legend(series) -> str:
    return "".join(f'<span class="lg"><i style="background:{_PALETTE[i % 8]}"></i>{html.escape(s["name"])}</span>' for i, s in enumerate(series))


class HTMLExporter(Exporter):
    kind = "html"
    extension = "html"

    CSS = """
:root{--bg:#fff;--fg:#1c1f24;--mut:#5b6470;--line:#d9dde3;--hd:#f3f5f8}
@media(prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e6e9ee;--mut:#9aa3b0;--line:#2c333c;--hd:#1c2128}}
body{font:16px/1.55 system-ui,"Noto Naskh Arabic","IBM Plex Sans Arabic",sans-serif;background:var(--bg);color:var(--fg);max-width:1000px;margin:2rem auto;padding:0 1rem}
h1,h2,h3{line-height:1.25}h2{margin-top:2.2rem;border-bottom:1px solid var(--line);padding-bottom:.3rem}
table{border-collapse:collapse;width:100%;margin:.6rem 0 1.2rem;font-size:.92rem}
th,td{border:1px solid var(--line);padding:.35rem .6rem;text-align:start;unicode-bidi:plaintext}th{background:var(--hd)}
pre{background:var(--hd);padding:.8rem;overflow:auto;border-radius:6px}.mut{color:var(--mut)}
.chart{width:100%;max-width:640px;color:var(--fg)}.lg{margin-inline-end:1rem;font-size:.85rem}.lg i{display:inline-block;width:.8rem;height:.8rem;margin-inline-end:.3rem;border-radius:2px}
nav{background:var(--hd);padding:.6rem 1rem;border-radius:6px}nav a{margin-inline-end:1rem}
"""

    def export(self, report: ReportModel, out: BinaryIO) -> str:
        e = html.escape
        parts = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                 f"<title>{e(report.title)}</title><style>{self.CSS}</style><body><h1>{e(report.title)}</h1>"]
        meta = ([f"<b>Question:</b> {e(report.question)}"] if report.question else []) + [e(m) for m in report.meta.get("header", [])]
        if report.generated_at:
            meta.append(f"<b>Generated:</b> {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
        parts.append(f"<p class=mut dir=auto>{' · '.join(meta)}</p>")
        parts.append("<nav>" + "".join(f'<a href="#s{i}">{i}. {e(s["title"])}</a>' for i, s in enumerate(report.sections, 1)) + "</nav>")
        for i, sec in enumerate(report.sections, 1):
            parts.append(f'<section id="s{i}"><h2>{i}. {e(sec["title"])}</h2>')
            for b in sec.get("blocks", []):
                parts.append(self._block(b))
            parts.append("</section>")
        out.write("".join(parts).encode("utf-8") + b"</body></html>\n")
        return "text/html; charset=utf-8"

    def _block(self, b: dict) -> str:
        e, t = html.escape, b["type"]
        if t == "paragraph":
            return f"<p dir=auto>{e(b['text'])}</p>"
        if t == "heading":
            return f"<h3>{e(b['text'])}</h3>"
        if t == "bullets":
            return "<ul>" + "".join(f"<li dir=auto>{e(i)}</li>" for i in b["items"]) + "</ul>"
        if t == "code":
            return f"<pre dir=ltr>{e(b['text'])}</pre>"
        if t == "table":
            cap = f"<caption><b>{e(b['title'])}</b></caption>" if b.get("title") else ""
            return (f"<table>{cap}<tr>" + "".join(f"<th>{e(fmt(c))}</th>" for c in b["columns"]) + "</tr>"
                    + "".join("<tr>" + "".join(f"<td dir=auto>{e(fmt(c))}</td>" for c in r) + "</tr>" for r in b["rows"]) + "</table>")
        if t == "chart":
            svg = _svg_bar(b) if b["kind"] == "bar" else _svg_line(b)
            return f"<figure><figcaption><b>{e(b['title'])}</b></figcaption>{svg}<div>{_legend(b['series'])}</div></figure>"
        return ""


class JSONExporter(Exporter):
    kind = "json"
    extension = "json"

    def export(self, report: ReportModel, out: BinaryIO) -> str:
        doc = {"title": report.title, "question": report.question, "generated_at": _iso(report.generated_at),
               "meta": report.meta, "sections": report.sections}
        out.write(json.dumps(doc, ensure_ascii=False, indent=2, default=_iso).encode("utf-8"))
        return "application/json"


class CSVExporter(Exporter):
    """Every table, stacked: a ``# section / table`` comment row, header, rows, blank line."""
    kind = "csv"
    extension = "csv"

    def export(self, report: ReportModel, out: BinaryIO) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for n, sec in enumerate(report.sections, 1):
            for b in sec.get("blocks", []):
                if b["type"] == "table":
                    w.writerow([f"# {n}. {sec['title']}" + (f" / {b['title']}" if b.get("title") else "")])
                    w.writerow(b["columns"])
                    w.writerows([["" if c is None else c for c in r] for r in b["rows"]])
                    w.writerow([])
        out.write(buf.getvalue().encode("utf-8-sig"))
        return "text/csv; charset=utf-8"


class JSONLDatasetExporter(Exporter):
    """One JSON object per line from ``report.meta['records']`` (hypotheses / segments / gold items)."""
    kind = "jsonl_dataset"
    extension = "jsonl"

    def export(self, report: ReportModel, out: BinaryIO) -> str:
        for rec in report.meta.get("records", []):
            out.write((json.dumps(rec, ensure_ascii=False, sort_keys=True, default=_iso) + "\n").encode("utf-8"))
        return "application/x-ndjson"
