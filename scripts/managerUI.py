#!/usr/bin/env python3
"""
Generate a self-contained HTML prototype UI for the Telescope YAML manager.

This is intentionally a single dependency-light script. It reads the same
template/recipes pair as makeYaml.py and writes one static HTML dashboard.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import webbrowser
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import makeYaml  # noqa: E402


DEFAULT_OUT = PROJECT_ROOT / "UI" / "manager_dashboard.html"


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower() or "item"


def yaml_text(value: Any) -> str:
    return makeYaml._simple_yaml_dump(value).rstrip()


def message_rows(messages: list[Any]) -> str:
    if not messages:
        return '<div class="empty">None</div>'
    rows = []
    for msg in messages:
        rows.append(
            f"""
            <div class="message {e(msg.level.lower())}">
              <div class="message-code">{e(msg.code)}</div>
              <div class="message-body">{e(msg.message)}</div>
              <div class="message-context">{e(msg.context)}</div>
            </div>
            """
        )
    return "\n".join(rows)


def badge(text: str, kind: str = "neutral") -> str:
    return f'<span class="badge {e(kind)}">{e(text)}</span>'


def value_list(values: list[str], limit: int = 9) -> str:
    if not values:
        return '<span class="muted">None detected</span>'
    shown = values[:limit]
    extra = len(values) - len(shown)
    body = "".join(f"<li>{e(v)}</li>" for v in shown)
    if extra:
        body += f"<li class='muted'>+ {extra} more</li>"
    return f"<ul>{body}</ul>"


def upload_cards(template: dict[str, Any], result: makeYaml.CompileResult) -> str:
    uploads = template.get("upload_cohorts", []) or []
    if not uploads:
        return '<div class="empty">No upload cohorts defined.</div>'
    upload_errors = " ".join(f"{m.context} {m.message}" for m in result.errors)
    cards = []
    for upload in uploads:
        name = upload.get("name")
        dest = upload.get("dest_table", name)
        has_error = str(name) in upload_errors or str(dest) in upload_errors
        kind = "error" if has_error else "ok"
        cards.append(
            f"""
            <details class="card" open>
              <summary>
                <span>{e(name)}</span>
                {badge("error" if has_error else "registered", kind)}
              </summary>
              <dl>
                <dt>Destination</dt><dd>{e(dest)}</dd>
                <dt>Type</dt><dd>{e(upload.get("file_type", ""))}</dd>
                <dt>Scope</dt><dd>{e(upload.get("scope", "global"))}</dd>
                <dt>Push This Cycle</dt><dd>{e(upload.get("push_this_cycle", True))}</dd>
                <dt>File</dt><dd>{e(upload.get("file_loc", ""))}</dd>
              </dl>
            </details>
            """
        )
    return "\n".join(cards)


def cohort_cards(result: makeYaml.CompileResult) -> str:
    cohorts = result.finished_yaml.get("cohorts", []) or []
    analysis = result.analysis or {}
    required_vars = analysis.get("required_vars", {})
    required_cols = analysis.get("required_table_columns", {})
    outputs = analysis.get("output_columns", {})
    if not cohorts:
        return '<div class="empty">No cohorts available.</div>'
    cards = []
    for cohort in cohorts:
        name = cohort.get("name", "")
        dest = cohort.get("dest_table", name)
        ctype = str(cohort.get("type", "fact"))
        kind = "pk" if ctype.lower() == "pk" else "neutral"
        req_var_names = sorted((required_vars.get(name) or {}).keys())
        table_inputs = required_cols.get(name) or {}
        table_bits = []
        for table_var, cols in table_inputs.items():
            table_bits.append(f"<h4>{e(table_var)}</h4>{value_list(cols)}")
        split = cohort.get("split_after_build")
        batching = cohort.get("batching")
        cards.append(
            f"""
            <details class="card cohort-card" id="cohort-{slug(str(name))}">
              <summary>
                <span>{e(name)}</span>
                <span>{badge(ctype, kind)} {badge(str(dest), "neutral")}</span>
              </summary>
              <div class="grid two">
                <section>
                  <h4>Required Variables</h4>
                  {value_list(req_var_names)}
                </section>
                <section>
                  <h4>Output Columns</h4>
                  {value_list(outputs.get(dest, []), 12)}
                </section>
              </div>
              <section>
                <h4>Input Table Contracts</h4>
                {''.join(table_bits) if table_bits else '<div class="empty">None</div>'}
              </section>
              <section>
                <h4>Split After Build</h4>
                <pre>{e(yaml_text(split) if split else "None")}</pre>
              </section>
              <section>
                <h4>Batching</h4>
                <pre>{e(yaml_text(batching) if batching else "None")}</pre>
              </section>
            </details>
            """
        )
    return "\n".join(cards)


def recipe_cards(recipes_doc: dict[str, Any]) -> str:
    recipes = recipes_doc.get("recipes", []) or []
    if not recipes:
        return '<div class="empty">No recipes found.</div>'
    cards = []
    for recipe in recipes:
        name = recipe.get("name", "")
        outputs = makeYaml.output_columns(recipe)
        req_vars = sorted(makeYaml.infer_required_vars(recipe).keys())
        inputs = makeYaml.infer_table_inputs(recipe)
        input_bits = []
        for table_var, meta in inputs.items():
            input_bits.append(f"<h4>{e(table_var)} as {e(meta.get('alias'))}</h4>{value_list(meta.get('required_columns', []))}")
        cards.append(
            f"""
            <details class="card">
              <summary>
                <span>{e(name)}</span>
                {badge(e(recipe.get("type", "fact")), "pk" if str(recipe.get("type", "")).lower() == "pk" else "neutral")}
              </summary>
              <p>{e(recipe.get("description", ""))}</p>
              <div class="grid three">
                <section><h4>Required Variables</h4>{value_list(req_vars)}</section>
                <section><h4>Output Columns</h4>{value_list(outputs, 12)}</section>
                <section><h4>Table Inputs</h4>{''.join(input_bits) if input_bits else '<div class="empty">None</div>'}</section>
              </div>
            </details>
            """
        )
    return "\n".join(cards)


def graph_panel(result: makeYaml.CompileResult) -> str:
    cohorts = result.finished_yaml.get("cohorts", []) or []
    analysis = result.analysis or {}
    required_cols = analysis.get("required_table_columns", {})
    nodes = []
    edges = []
    for cohort in cohorts:
        name = str(cohort.get("name", ""))
        nodes.append(name)
        resolved_vars = cohort.get("_resolved_vars", {})
        for table_var in (required_cols.get(name) or {}):
            target = resolved_vars.get(table_var) or table_var
            edges.append((str(target), name, table_var))
    if not nodes:
        return '<div class="empty">No dependency graph available.</div>'
    unique_nodes = []
    for item in nodes + [edge[0] for edge in edges]:
        if item and item not in unique_nodes:
            unique_nodes.append(item)
    node_html = "".join(f'<div class="node" data-node="{e(n)}">{e(n)}</div>' for n in unique_nodes)
    edge_html = "".join(f'<li><button class="linklike" data-target="{e(dst)}">{e(src)} -> {e(dst)}</button> <span class="muted">({e(label)})</span></li>' for src, dst, label in edges)
    return f"""
      <div class="graph-layout">
        <div class="node-list">{node_html}</div>
        <div>
          <h3>Dependencies</h3>
          <ul class="edge-list">{edge_html or '<li class="muted">No table-input edges detected.</li>'}</ul>
        </div>
      </div>
    """


def pipeline_panel(result: makeYaml.CompileResult) -> str:
    steps = [
        ("Load YAML", True),
        ("Import Recipes", not any(m.code == "missing_recipe" for m in result.errors)),
        ("Infer Variables", bool(result.analysis)),
        ("Validate Uploads", not any(m.code.startswith("missing_upload") or m.code == "upload_read_error" for m in result.errors)),
        ("Validate Columns", not any("column" in m.code for m in result.errors)),
        ("Render Output", result.ok),
        ("Ready For VM", result.ok),
    ]
    return "".join(
        f'<div class="pipeline-step {"pass" if ok else "fail"}"><span>{idx}</span><strong>{e(label)}</strong><em>{"pass" if ok else "blocked"}</em></div>'
        for idx, (label, ok) in enumerate(steps, 1)
    )


def summary_cards(template: dict[str, Any], result: makeYaml.CompileResult) -> str:
    cohorts = result.finished_yaml.get("cohorts", []) or []
    uploads = template.get("upload_cohorts", []) or []
    status = "Ready" if result.ok else "Blocked"
    return f"""
      <div class="summary">
        <div class="metric {('ok' if result.ok else 'error')}"><span>Status</span><strong>{status}</strong></div>
        <div class="metric"><span>Errors</span><strong>{len(result.errors)}</strong></div>
        <div class="metric"><span>Warnings</span><strong>{len(result.warnings)}</strong></div>
        <div class="metric"><span>Cohorts</span><strong>{len(cohorts)}</strong></div>
        <div class="metric"><span>Uploads</span><strong>{len(uploads)}</strong></div>
      </div>
    """


def build_html(template_path: Path, recipes_path: Path, result: makeYaml.CompileResult) -> str:
    template = makeYaml.load_yaml(template_path) or {}
    recipes_doc = makeYaml.load_yaml(recipes_path) or {}
    source_text = template_path.read_text(encoding="utf-8")
    finished_text = yaml_text(result.finished_yaml)
    data_json = html.escape(json.dumps({
        "errors": [m.to_dict() for m in result.errors],
        "warnings": [m.to_dict() for m in result.warnings],
        "analysis": result.analysis,
    }, indent=2), quote=False)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telescope YAML Manager</title>
  <style>{CSS}</style>
</head>
<body>
  <header>
    <div>
      <h1>Telescope YAML Manager</h1>
      <p>{e(template_path)} | {e(recipes_path)}</p>
    </div>
    <button id="themeToggle" title="Toggle dark mode">Theme</button>
  </header>

  <main>
    {summary_cards(template, result)}

    <nav class="tabs" aria-label="Dashboard sections">
      <button class="tab active" data-tab="validation">Validation</button>
      <button class="tab" data-tab="pipeline">Pipeline</button>
      <button class="tab" data-tab="cohorts">Cohorts</button>
      <button class="tab" data-tab="uploads">Uploads</button>
      <button class="tab" data-tab="recipes">Recipes</button>
      <button class="tab" data-tab="graph">Graph</button>
      <button class="tab" data-tab="yaml">YAML</button>
    </nav>

    <section id="validation" class="panel active">
      <div class="grid two">
        <section class="block">
          <h2>Errors</h2>
          {message_rows(result.errors)}
        </section>
        <section class="block">
          <h2>Warnings</h2>
          {message_rows(result.warnings)}
        </section>
      </div>
    </section>

    <section id="pipeline" class="panel">
      <section class="block">
        <h2>Compiler Pipeline</h2>
        <div class="pipeline">{pipeline_panel(result)}</div>
      </section>
    </section>

    <section id="cohorts" class="panel">
      <div class="toolbar">
        <input id="cohortSearch" type="search" placeholder="Filter cohorts">
        <button data-expand="cohorts">Expand All</button>
        <button data-collapse="cohorts">Collapse All</button>
      </div>
      <div id="cohortCards">{cohort_cards(result)}</div>
    </section>

    <section id="uploads" class="panel">
      <section class="block">
        <h2>Upload Cohorts</h2>
        {upload_cards(template, result)}
      </section>
    </section>

    <section id="recipes" class="panel">
      <section class="block">
        <h2>Recipes</h2>
        {recipe_cards(recipes_doc)}
      </section>
    </section>

    <section id="graph" class="panel">
      <section class="block">
        <h2>Dependency Graph</h2>
        {graph_panel(result)}
      </section>
    </section>

    <section id="yaml" class="panel">
      <div class="grid two">
        <section class="block">
          <h2>Source YAML</h2>
          <pre>{e(source_text)}</pre>
        </section>
        <section class="block">
          <h2>Finished YAML Preview</h2>
          <pre>{e(finished_text)}</pre>
        </section>
      </div>
      <section class="block">
        <h2>Analysis JSON</h2>
        <pre>{data_json}</pre>
      </section>
    </section>
  </main>

  <script>{JS}</script>
</body>
</html>
"""


CSS = r"""
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #20242a;
  --muted: #69717d;
  --line: #d9dee5;
  --accent: #256f8f;
  --ok: #26734d;
  --warn: #9a6200;
  --err: #a13737;
  --chip: #eef2f5;
}
body.dark {
  color-scheme: dark;
  --bg: #16181b;
  --panel: #20242a;
  --ink: #edf0f3;
  --muted: #a7b0bb;
  --line: #343a42;
  --accent: #6fb1cf;
  --ok: #74c69d;
  --warn: #e4b363;
  --err: #ff8a8a;
  --chip: #2b3138;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 18px 24px; border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 4; }
h1 { margin: 0; font-size: 22px; }
h2 { margin: 0 0 14px; font-size: 18px; }
h3 { margin: 0 0 12px; font-size: 15px; }
h4 { margin: 12px 0 8px; font-size: 13px; }
p { color: var(--muted); margin: 4px 0 0; }
button, input { font: inherit; }
button { border: 1px solid var(--line); background: var(--panel); color: var(--ink); padding: 8px 11px; border-radius: 7px; cursor: pointer; }
button:hover { border-color: var(--accent); }
main { padding: 22px; max-width: 1500px; margin: 0 auto; }
.summary { display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 12px; margin-bottom: 18px; }
.metric { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
.metric span { display: block; color: var(--muted); font-size: 12px; }
.metric strong { display: block; font-size: 24px; margin-top: 6px; }
.metric.ok strong { color: var(--ok); }
.metric.error strong { color: var(--err); }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; }
.tab.active { background: var(--accent); color: white; border-color: var(--accent); }
.panel { display: none; }
.panel.active { display: block; }
.grid { display: grid; gap: 14px; }
.grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.block, .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; margin-bottom: 12px; }
.card summary { display: flex; justify-content: space-between; align-items: center; gap: 16px; cursor: pointer; font-weight: 700; }
.badge { display: inline-flex; align-items: center; gap: 4px; border-radius: 999px; padding: 3px 8px; background: var(--chip); color: var(--ink); font-size: 12px; font-weight: 600; }
.badge.ok, .badge.pk { color: var(--ok); }
.badge.error { color: var(--err); }
.message { border-left: 4px solid var(--line); padding: 10px 12px; background: var(--chip); border-radius: 6px; margin-bottom: 9px; }
.message.error { border-left-color: var(--err); }
.message.warn { border-left-color: var(--warn); }
.message-code { font-weight: 800; font-size: 13px; }
.message-context, .muted { color: var(--muted); font-size: 12px; }
dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 8px 14px; }
dt { color: var(--muted); }
dd { margin: 0; overflow-wrap: anywhere; }
ul { padding-left: 18px; margin: 8px 0; }
pre { white-space: pre-wrap; overflow: auto; background: var(--chip); border: 1px solid var(--line); border-radius: 7px; padding: 12px; max-height: 520px; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.toolbar input { flex: 1; border: 1px solid var(--line); border-radius: 7px; padding: 9px 11px; background: var(--panel); color: var(--ink); }
.pipeline { display: grid; gap: 10px; }
.pipeline-step { display: grid; grid-template-columns: 34px 1fr auto; align-items: center; gap: 10px; border: 1px solid var(--line); border-radius: 8px; padding: 10px; }
.pipeline-step span { display: grid; place-items: center; height: 26px; width: 26px; border-radius: 50%; background: var(--chip); }
.pipeline-step.pass em { color: var(--ok); }
.pipeline-step.fail em { color: var(--err); }
.graph-layout { display: grid; grid-template-columns: minmax(220px, 360px) 1fr; gap: 18px; }
.node-list { display: grid; gap: 8px; }
.node { border: 1px solid var(--line); background: var(--chip); border-radius: 8px; padding: 10px; font-weight: 650; }
.edge-list { margin-top: 0; }
.linklike { border: 0; background: transparent; padding: 0; color: var(--accent); text-decoration: underline; }
.empty { color: var(--muted); padding: 8px 0; }
.hidden { display: none; }
@media (max-width: 900px) {
  .summary, .grid.two, .grid.three, .graph-layout { grid-template-columns: 1fr; }
  header { position: static; align-items: flex-start; }
}
"""


JS = r"""
document.querySelectorAll('.tab').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    button.classList.add('active');
    document.getElementById(button.dataset.tab).classList.add('active');
  });
});

document.getElementById('themeToggle').addEventListener('click', () => {
  document.body.classList.toggle('dark');
});

document.querySelectorAll('[data-expand]').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll(`#${button.dataset.expand} details`).forEach(d => d.open = true);
  });
});

document.querySelectorAll('[data-collapse]').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll(`#${button.dataset.collapse} details`).forEach(d => d.open = false);
  });
});

const cohortSearch = document.getElementById('cohortSearch');
if (cohortSearch) {
  cohortSearch.addEventListener('input', () => {
    const q = cohortSearch.value.toLowerCase();
    document.querySelectorAll('.cohort-card').forEach(card => {
      card.classList.toggle('hidden', !card.innerText.toLowerCase().includes(q));
    });
  });
}

document.querySelectorAll('[data-target]').forEach(button => {
  button.addEventListener('click', () => {
    const id = 'cohort-' + button.dataset.target.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-|-$/g, '');
    const target = document.getElementById(id);
    if (!target) return;
    document.querySelector('[data-tab="cohorts"]').click();
    target.open = true;
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a static Telescope manager UI.")
    parser.add_argument("--template", default=str(makeYaml.default_template_path()))
    parser.add_argument("--recipes", default=str(makeYaml.default_recipes_path()))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--open", action="store_true", help="Open the generated dashboard in a browser.")
    args = parser.parse_args(argv)

    template_path = Path(args.template)
    recipes_path = Path(args.recipes)
    out_path = Path(args.out)
    result = makeYaml.compile_yaml(template_path=template_path, recipes_path=recipes_path, write=False)
    html_text = build_html(template_path, recipes_path, result)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")

    print(f"Wrote {out_path}")
    print(f"Status: {'Ready' if result.ok else 'Blocked'}")
    print(f"Errors: {len(result.errors)}")
    print(f"Warnings: {len(result.warnings)}")
    if args.open:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
