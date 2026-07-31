"""Report generation for reference-time analysis.

Produces deterministic JSON and human-readable HTML reports.
HTML escapes all user-provided content to prevent stored XSS.
No physical paths, tickets, tokens, stack traces or credentials in output.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from .models import MappingType, ReferenceTimeAnalysis, Warning


def generate_json_report(analysis: ReferenceTimeAnalysis) -> str:
    """Generate deterministic JSON report.

    Timestamps are included but excluded from canonical equality
    by the cache key (which uses input identities only).
    """
    data = analysis.model_dump(mode="json")
    return json.dumps(
        data,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    )


def _escape(text: str | None) -> str:
    """HTML-escape user-provided text."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def _confidence_class(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    elif confidence >= 0.5:
        return "medium"
    elif confidence >= 0.3:
        return "low"
    return "very-low"


def _mapping_type_label(mt: MappingType) -> str:
    labels = {
        MappingType.ONE_TO_ONE: "1:1",
        MappingType.SOURCE_GAP: "Source Gap",
        MappingType.GP_GAP: "GP Gap",
        MappingType.REPEAT: "Repeat",
        MappingType.AMBIGUOUS: "Ambiguous",
    }
    return labels.get(mt, str(mt))


def _render_warnings(warnings: list[Warning]) -> str:
    if not warnings:
        return ""
    items = "".join(
        f"<li><code>{_escape(w.code.value)}</code>: {_escape(w.message)}</li>"
        for w in warnings
    )
    return f"<ul class='warnings'>{items}</ul>"


def generate_html_report(analysis: ReferenceTimeAnalysis) -> str:
    """Generate human-readable HTML report.

    All user-provided names/markers are escaped. Deterministic row ordering.
    """
    now = datetime.now(timezone.utc).isoformat()

    consensus_html = ""
    if analysis.midi_consensus:
        c = analysis.midi_consensus
        consensus_html = f"""
        <div class="section">
            <h2>MIDI Consensus</h2>
            <table>
                <tr><td>Decision</td><td><span class="badge {c.decision.value}">{_escape(c.decision.value)}</span></td></tr>
                <tr><td>Source count</td><td>{c.source_count}</td></tr>
                <tr><td>Primary SHA</td><td><code>{_escape(c.primary_sha256 or 'N/A')}</code></td></tr>
                <tr><td>Selection reason</td><td>{_escape(c.selection_reason)}</td></tr>
            </table>
            {_render_conflicts(c.conflict_regions)}
        </div>
        """

    source_rows = ""
    for m in analysis.source_measures:
        conf_cls = _confidence_class(m.confidence)
        source_rows += f"""
        <tr class="{conf_cls}">
            <td>{m.index}</td>
            <td>{m.seconds_start:.3f}</td>
            <td>{m.seconds_end:.3f}</td>
            <td>{m.numerator}/{m.denominator}</td>
            <td>{m.tempo_bpm:.1f}</td>
            <td>{m.confidence:.2f}</td>
            <td>{_render_warnings(m.warnings)}</td>
        </tr>
        """

    mapping_rows = ""
    for m in analysis.mappings:
        conf_cls = _confidence_class(m.confidence)
        gp_display = (
            f"M{m.gp_measure_number}" if m.gp_measure_number else "—"
        )
        mapping_rows += f"""
        <tr class="{conf_cls}">
            <td>{m.source_measure_index}</td>
            <td>{gp_display}</td>
            <td><span class="badge {m.mapping_type.value}">{_mapping_type_label(m.mapping_type)}</span></td>
            <td>{m.source_seconds_start:.3f}–{m.source_seconds_end:.3f}</td>
            <td>{m.gp_tick_start or '—'}–{m.gp_tick_end or '—'}</td>
            <td>{m.confidence:.2f}</td>
            <td>{', '.join(_escape(e) for e in m.evidence)}</td>
            <td>{_render_warnings(m.warnings)}</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Reference-Time Analysis Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2em; color: #333; background: #fafafa; }}
h1 {{ color: #1a1a2e; border-bottom: 2px solid #16213e; padding-bottom: 0.5em; }}
h2 {{ color: #16213e; margin-top: 1.5em; }}
table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.9em; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
th {{ background: #16213e; color: white; }}
tr:nth-child(even) {{ background: #f5f5f5; }}
tr.high {{ }}
tr.medium {{ background: #fff8e1; }}
tr.low {{ background: #fff3e0; }}
tr.very-low {{ background: #ffebee; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }}
.badge.one_to_one {{ background: #c8e6c9; color: #2e7d32; }}
.badge.source_gap {{ background: #ffe0b2; color: #e65100; }}
.badge.gp_gap {{ background: #e1bee7; color: #6a1b9a; }}
.badge.repeat {{ background: #b3e5fc; color: #01579b; }}
.badge.ambiguous {{ background: #ffcdd2; color: #b71c1c; }}
.badge.agreed {{ background: #c8e6c9; color: #2e7d32; }}
.badge.conflict {{ background: #ffcdd2; color: #b71c1c; }}
.badge.single_source {{ background: #e0e0e0; color: #424242; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1em; margin: 1em 0; }}
.summary-card {{ background: white; border: 1px solid #ddd; border-radius: 8px; padding: 1em; }}
.summary-card h3 {{ margin: 0 0 0.5em; font-size: 1em; color: #666; }}
.summary-card .value {{ font-size: 1.5em; font-weight: 700; color: #16213e; }}
.warnings {{ margin: 0; padding-left: 1.2em; font-size: 0.85em; color: #bf360c; }}
.section {{ margin-bottom: 2em; }}
code {{ background: #f5f5f5; padding: 1px 4px; border-radius: 3px; font-size: 0.9em; }}
.filter-bar {{ margin: 1em 0; }}
.filter-bar label {{ margin-right: 1em; cursor: pointer; }}
.meta {{ color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>Reference-Time Analysis Report</h1>

<div class="meta">
    <p>Analysis ID: <code>{_escape(analysis.analysis_id)}</code></p>
    <p>Project ID: <code>{_escape(analysis.project_id)}</code></p>
    <p>Schema version: {_escape(analysis.schema_version)}</p>
    <p>Generated: {_escape(now)}</p>
</div>

<div class="summary">
    <div class="summary-card">
        <h3>GP Revision</h3>
        <div class="value"><code>{_escape(analysis.gp_revision_sha256[:12])}</code></div>
    </div>
    <div class="summary-card">
        <h3>Source Measures</h3>
        <div class="value">{len(analysis.source_measures)}</div>
    </div>
    <div class="summary-card">
        <h3>GP Measures</h3>
        <div class="value">{len(analysis.gp_measures)}</div>
    </div>
    <div class="summary-card">
        <h3>Mappings</h3>
        <div class="value">{len(analysis.mappings)}</div>
    </div>
    <div class="summary-card">
        <h3>Global Confidence</h3>
        <div class="value">{analysis.global_confidence:.2f}</div>
    </div>
</div>

{_render_warnings(analysis.global_warnings)}

{consensus_html}

<div class="section">
<h2>Source Measures</h2>
<table>
<thead>
<tr><th>#</th><th>Start (s)</th><th>End (s)</th><th>Time Sig</th><th>BPM</th><th>Confidence</th><th>Warnings</th></tr>
</thead>
<tbody>
{source_rows}
</tbody>
</table>
</div>

<div class="section">
<h2>Measure Mappings</h2>
<div class="filter-bar">
    <label><input type="checkbox" id="filter-ambiguous" onchange="filterRows()"> Show only ambiguous/unmapped</label>
    <label><input type="checkbox" id="filter-low" onchange="filterRows()"> Show only low confidence (&lt;0.5)</label>
</div>
<table id="mapping-table">
<thead>
<tr><th>Source #</th><th>GP Measure</th><th>Type</th><th>Source (s)</th><th>GP (ticks)</th><th>Confidence</th><th>Evidence</th><th>Warnings</th></tr>
</thead>
<tbody>
{mapping_rows}
</tbody>
</table>
</div>

<div class="section">
<h2>Input Identities</h2>
<table>
<thead><tr><th>Input</th><th>SHA-256</th></tr></thead>
<tbody>
{"".join(f"<tr><td>{_escape(k)}</td><td><code>{_escape(v)}</code></td></tr>" for k, v in sorted(analysis.input_identities.items()))}
</tbody>
</table>
</div>

<script>
function filterRows() {{
    const showAmbiguous = document.getElementById('filter-ambiguous').checked;
    const showLow = document.getElementById('filter-low').checked;
    const rows = document.querySelectorAll('#mapping-table tbody tr');
    rows.forEach(row => {{
        const type = row.querySelector('.badge')?.textContent || '';
        const conf = parseFloat(row.children[5]?.textContent || '1');
        let show = true;
        if (showAmbiguous && !['Source Gap', 'GP Gap', 'Ambiguous'].includes(type)) show = false;
        if (showLow && conf >= 0.5) show = false;
        row.style.display = show ? '' : 'none';
    }});
}}
</script>
</body>
</html>"""


def _render_conflicts(conflict_regions: list[dict]) -> str:
    if not conflict_regions:
        return "<p>No conflicts detected.</p>"
    rows = ""
    for cr in conflict_regions:
        cr_type = _escape(str(cr.get("type", "")))
        sources = ", ".join(_escape(s[:12]) for s in cr.get("sources", []))
        detail = _escape(str({k: v for k, v in cr.items() if k not in ("type", "sources")}))
        rows += f"<tr><td>{cr_type}</td><td>{sources}</td><td>{detail}</td></tr>"
    return f"""
    <h3>Conflict Regions</h3>
    <table>
    <thead><tr><th>Type</th><th>Sources</th><th>Detail</th></tr></thead>
    <tbody>{rows}</tbody>
    </table>
    """
