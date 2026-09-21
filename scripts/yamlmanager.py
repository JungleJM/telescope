#!/usr/bin/env python3
"""
Generate a self-contained HTML prototype UI for YAML Manager.

This is intentionally a dependency-light script. It reads YAML Manager
template/recipes files through a configurable backend and writes one static
HTML dashboard.
"""

from __future__ import annotations

import argparse
import html
import http.server
import importlib
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.pycache_prefix = str(PROJECT_ROOT / "cleanup" / "python_cache")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


DEFAULT_OUT = PROJECT_ROOT / "UI" / "manager_dashboard.html"
BACKEND_MODULE = os.environ.get(
    "YAMLMANAGER_BACKEND_MODULE",
    os.environ.get("TELESCOPE_BACKEND_MODULE", "yamlmanager_backend"),
)
try:
    backend = importlib.import_module(BACKEND_MODULE)
except ImportError as exc:
    raise SystemExit(
        f"[yamlmanager] Could not import backend module {BACKEND_MODULE!r}. "
        "Set YAMLMANAGER_BACKEND_MODULE to a Python module on PYTHONPATH."
    ) from exc


def env_int(names: tuple[str, ...], fallback: int) -> int:
    for name in names:
        value = os.environ.get(name)
        if not value:
            continue
        try:
            return int(value)
        except ValueError:
            print(f"[yamlmanager] Ignoring invalid {name}={value!r}; using {fallback}.", file=sys.stderr)
            return fallback
    return fallback


DEFAULT_HOST = os.environ.get(
    "YAMLMANAGER_HOST",
    os.environ.get("TELESCOPE_MANAGER_HOST", os.environ.get("MANAGER_UI_HOST", "127.0.0.1")),
)
DEFAULT_PORT = env_int(("YAMLMANAGER_PORT", "TELESCOPE_MANAGER_PORT", "MANAGER_UI_PORT"), 8765)


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower() or "item"


def yaml_text(value: Any) -> str:
    return backend.dump_yaml_text(value).rstrip()


def json_payload(value: Any) -> str:
    return (
        json.dumps(value)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def print_messages(result: Any) -> None:
    for msg in getattr(result, "errors", []):
        print(f"ERROR [{msg.code}] {msg.message} {msg.context}".rstrip())
    for msg in getattr(result, "warnings", []):
        print(f"WARN  [{msg.code}] {msg.message} {msg.context}".rstrip())


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
    body = "".join(f"<li>{e(v)}</li>" for v in shown)
    extras = values[limit:]
    if extras:
        body += "".join(f'<li class="extra-item hidden">{e(v)}</li>' for v in extras)
        body += f'<li><button class="linklike" data-show-more-list>+ {len(extras)} more</button></li>'
    return f"<ul>{body}</ul>"


def session_label(name: str, pk_table: str) -> str:
    for suffix in ("Patients", "PKTable"):
        if name.endswith(suffix):
            return name[: -len(suffix)] or name
    for suffix in ("OtherHospitalizations", "Hospitalizations", "OtherDx"):
        if name.endswith(suffix):
            return name[: -len(suffix)] or name
    return pk_table or name


def build_connection_maps(result: backend.CompileResult) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, str]]:
    cohorts = result.finished_yaml.get("cohorts", []) or []
    required_cols = (result.analysis or {}).get("required_table_columns", {})
    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    sessions: dict[str, str] = {}
    current_pk_dest = ""
    color_counter = 0
    for cohort in cohorts:
        dest = str(cohort.get("dest_table", cohort.get("name", "")))
        name = str(cohort.get("name", dest))
        if str(cohort.get("type", "")).lower() == "pk":
            current_pk_dest = dest
        sessions[name] = session_label(name, current_pk_dest)
        resolved_vars = cohort.get("_resolved_vars", {})
        for table_var, cols in (required_cols.get(name) or {}).items():
            target = str(resolved_vars.get(table_var) or table_var)
            if table_var == "PKTable" and current_pk_dest:
                target = current_pk_dest
            item = {
                "source": target,
                "target": name,
                "table_var": table_var,
                "columns": cols,
                "color": color_counter % 8,
            }
            outgoing.setdefault(target, []).append(item)
            incoming.setdefault(name, []).append(item)
            color_counter += 1
    return outgoing, incoming, sessions


def upload_dest(upload: dict[str, Any]) -> str:
    return str(upload.get("dest_table") or upload.get("name") or "")


def upload_columns(upload: dict[str, Any]) -> list[str]:
    schema = upload.get("columns") or upload.get("schema") or []
    if not schema:
        return []
    if isinstance(schema, list) and schema and isinstance(schema[0], dict):
        return [str(column.get("name")) for column in schema if column.get("name")]
    if isinstance(schema, list):
        return [str(column) for column in schema]
    return []


def upload_has_error(upload: dict[str, Any], result: backend.CompileResult) -> bool:
    name = str(upload.get("name") or "")
    dest = upload_dest(upload)
    for msg in result.errors:
        haystack = f"{msg.context} {msg.message}"
        if (name and name in haystack) or (dest and dest in haystack):
            return True
    return False


def connection_chips(items: list[dict[str, Any]], compact: bool = False, side: str = "incoming") -> str:
    if not items:
        return '<span class="muted">None detected</span>'
    chips = []
    max_items = 4 if compact else len(items)
    for item in items[:max_items]:
        cols = item.get("columns") or []
        col_text = ", ".join(cols[:3])
        if len(cols) > 3:
            col_text += f", +{len(cols) - 3} more"
        label = item.get("source") if side == "incoming" else item.get("target")
        chips.append(
            f'<span class="connection-chip c{item.get("color", 0)}">'
            f'{e(label)}: {e(col_text or item.get("table_var"))}</span>'
        )
    if compact and len(items) > max_items:
        chips.append('<span class="muted">(click to expand)</span>')
    return f'<span class="connection-chips">{"".join(chips)}</span>'


def source_connection_columns(items: list[dict[str, Any]]) -> str:
    cols: list[str] = []
    seen: set[str] = set()
    for item in items:
        for col in item.get("columns") or []:
            text = str(col)
            if text not in seen:
                seen.add(text)
                cols.append(text)
    if not cols:
        return '<span class="muted">None detected</span>'
    chips = "".join(f'<span class="connection-chip c0">{e(col)}</span>' for col in cols)
    return f'<span class="connection-chips">{chips}</span>'


def connection_details(items: list[dict[str, Any]], side: str = "outgoing") -> str:
    if not items:
        return '<div class="empty">None</div>'
    blocks = []
    for item in items:
        title = item.get("source") if side == "incoming" else item.get("target")
        blocks.append(
            f"""
            <div class="connection-detail c{item.get("color", 0)}">
              <strong>{e(title)}</strong>
              <span class="muted">via {e(item.get("table_var"))}</span>
              {value_list(item.get("columns") or [], 12)}
            </div>
            """
        )
    return "".join(blocks)


def column_list(values: list[str], connections: list[dict[str, Any]], limit: int = 12) -> str:
    if not values:
        return '<span class="muted">None detected</span>'
    refs_by_col: dict[str, list[dict[str, Any]]] = {}
    for item in connections:
        for col in item.get("columns") or []:
            refs_by_col.setdefault(str(col), []).append(item)
    items = []
    for idx, value in enumerate(values):
        refs = refs_by_col.get(str(value), [])
        ref_marks = "".join(
            f'<span class="column-ref c{ref.get("color", 0)}" title="Referenced by {e(ref.get("target"))} via {e(ref.get("table_var"))}"></span>'
            for ref in refs
        )
        hidden = ' class="extra-item hidden"' if idx >= limit else ""
        items.append(f"<li{hidden}>{ref_marks}{e(value)}</li>")
    if len(values) > limit:
        items.append(f'<li><button class="linklike" data-show-more-list>+ {len(values) - limit} more</button></li>')
    return f"<ul>{''.join(items)}</ul>"


def upload_cards(template: dict[str, Any], result: backend.CompileResult) -> str:
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


def cohort_cards(result: backend.CompileResult) -> str:
    cohorts = result.finished_yaml.get("cohorts", []) or []
    uploads = result.finished_yaml.get("upload_cohorts", []) or []
    analysis = result.analysis or {}
    required_vars = analysis.get("required_vars", {})
    required_cols = analysis.get("required_table_columns", {})
    outputs = analysis.get("output_columns", {})
    outgoing_connections, incoming_connections, sessions = build_connection_maps(result)
    if not cohorts and not uploads:
        return '<div class="empty">No cohorts or uploads available.</div>'
    cards = []
    known_tables: set[str] = set()
    for cohort in cohorts:
        name = cohort.get("name", "")
        dest = cohort.get("dest_table", name)
        known_tables.update({str(name), str(dest)})
        ctype = str(cohort.get("type", "fact"))
        kind = "pk" if ctype.lower() == "pk" else "neutral"
        req_var_names = sorted((required_vars.get(name) or {}).keys())
        table_inputs = required_cols.get(name) or {}
        table_bits = []
        for table_var, cols in table_inputs.items():
            table_bits.append(f"<h4>{e(table_var)}</h4>{value_list(cols)}")
        outgoing = outgoing_connections.get(str(dest), [])
        incoming = incoming_connections.get(str(name), [])
        group = sessions.get(str(name), str(dest))
        summary = source_connection_columns(outgoing) if outgoing else connection_chips(incoming, compact=True, side="incoming")
        split = cohort.get("split_after_build")
        batching = cohort.get("batching")
        cards.append(
            f"""
            <details class="card cohort-card" id="cohort-{slug(str(name))}">
              <summary>
                <span>
                  <strong>{e(name)}</strong>
                  <span class="summary-connections">Cohort: {e(group)}</span>
                  <span class="summary-connections">Connections: {summary}</span>
                </span>
                <span>{badge(ctype, kind)}</span>
              </summary>
              <div class="grid two">
                <section>
                  <h4>Required Variables</h4>
                  {value_list(req_var_names)}
                </section>
                <section>
                  <h4>Output Columns</h4>
                  {column_list(outputs.get(dest, []), outgoing, 12)}
                </section>
              </div>
              <section>
                <h4>Required Input Tables</h4>
                {''.join(table_bits) if table_bits else '<div class="empty">None</div>'}
              </section>
              <section>
                <h4>Connected To</h4>
                {connection_details(incoming, side="incoming") if incoming else '<div class="empty">None</div>'}
              </section>
              <section>
                <h4>Used By Tables</h4>
                {connection_details(outgoing)}
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
    seen_uploads: set[int] = set()
    for upload in uploads:
        if not isinstance(upload, dict):
            continue
        ident = id(upload)
        if ident in seen_uploads:
            continue
        seen_uploads.add(ident)
        name = str(upload.get("name") or upload_dest(upload))
        dest = upload_dest(upload) or name
        known_tables.update({name, dest})
        outgoing = outgoing_connections.get(dest, []) + ([] if name == dest else outgoing_connections.get(name, []))
        cols = upload_columns(upload)
        kind = "error" if upload_has_error(upload, result) else "upload"
        type_badges = badge("upload", kind)
        if str(upload.get("type", "")).lower() == "pk":
            type_badges += " " + badge("PK", "pk")
        detail_bits = [
            ("Destination", dest),
            ("File Type", upload.get("file_type", "")),
            ("Scope", upload.get("scope", "global")),
            ("Push This Cycle", upload.get("push_this_cycle", True)),
            ("File/Table", upload.get("file_loc", "")),
        ]
        cards.append(
            f"""
            <details class="card cohort-card upload-card" id="cohort-{slug(name)}">
              <summary>
                <span>
                  <strong>{e(name)}</strong>
                  <span class="summary-connections">Upload: {e(dest)}</span>
                  <span class="summary-connections">Connections: {connection_chips(outgoing, compact=True, side="outgoing")}</span>
                </span>
                <span>{type_badges}</span>
              </summary>
              <div class="grid two">
                <section>
                  <h4>Upload Details</h4>
                  <dl>{''.join(f'<dt>{e(label)}</dt><dd>{e(value)}</dd>' for label, value in detail_bits if value not in ("", None))}</dl>
                </section>
                <section>
                  <h4>Output Columns</h4>
                  {column_list(cols, outgoing, 12)}
                </section>
              </div>
              <section>
                <h4>Used By Tables</h4>
                {connection_details(outgoing)}
              </section>
            </details>
            """
        )
    unresolved_sources = [
        source for source in outgoing_connections
        if source and source not in known_tables
    ]
    for source in unresolved_sources:
        outgoing = outgoing_connections.get(source, [])
        cards.append(
            f"""
            <details class="card cohort-card unresolved-card" id="cohort-{slug(source)}">
              <summary>
                <span>
                  <strong>{e(source)}</strong>
                  <span class="summary-connections">Required input table</span>
                  <span class="summary-connections">Connections: {connection_chips(outgoing, compact=True, side="outgoing")}</span>
                </span>
                <span>{badge("unresolved", "warn")}</span>
              </summary>
              <section>
                <h4>Used By Tables</h4>
                {connection_details(outgoing)}
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
        outputs = backend.recipe_output_columns(recipe)
        req_vars = sorted(backend.recipe_required_vars(recipe).keys())
        inputs = backend.recipe_table_inputs(recipe)
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


def graph_panel(result: backend.CompileResult) -> str:
    cohorts = result.finished_yaml.get("cohorts", []) or []
    uploads = result.finished_yaml.get("upload_cohorts", []) or []
    _, incoming_connections, _ = build_connection_maps(result)
    nodes = []
    edges = []
    for upload in uploads:
        if isinstance(upload, dict):
            nodes.append(upload_dest(upload) or str(upload.get("name", "")))
    for cohort in cohorts:
        name = str(cohort.get("name", ""))
        nodes.append(name)
        for item in incoming_connections.get(name, []):
            edges.append((str(item.get("source")), name, str(item.get("table_var"))))
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


def pipeline_panel(result: backend.CompileResult) -> str:
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


def export_preview_block(title: str, artifact_id: str, filename: str, result: Any) -> str:
    if getattr(result, "ok", False):
        text = yaml_text(result.finished_yaml)
    else:
        messages = [m.to_dict() for m in getattr(result, "errors", [])]
        text = json.dumps({"errors": messages}, indent=2)
    return f"""
      <section class="block export-block">
        <div class="export-head">
          <h2>{e(title)}</h2>
          <div class="toolbar compact">
            <button data-copy-artifact="{e(artifact_id)}">Copy</button>
            <button data-download-artifact="{e(artifact_id)}" data-filename="{e(filename)}">Download</button>
          </div>
        </div>
        <pre id="{e(artifact_id)}">{e(text)}</pre>
      </section>
    """


def exports_panel(template_path: Path, recipes_path: Path) -> str:
    symbolic = backend.build_preyaml(template_path, recipes_path, mode="symbolic")
    expanded = backend.build_preyaml(template_path, recipes_path, mode="expanded-recipes")
    manifest = backend.build_pullmanifest(template_path, recipes_path)
    return f"""
      <div class="grid three">
        {export_preview_block("pre-YAML", "exportPreyamlSymbolic", "preyaml.yaml", symbolic)}
        {export_preview_block("Expanded Recipes pre-YAML", "exportPreyamlExpanded", "preyaml.expanded.yaml", expanded)}
        {export_preview_block("pullmanifest.yaml", "exportPullmanifest", "pullmanifest.yaml", manifest)}
      </div>
      <section class="block">
        <h2>Handoff</h2>
        <p>Pullmanager handoff remains file-based. Once Pullmanager's CLI contract is available, YAML Manager can call it with the generated manifest path.</p>
        <pre>pullmanager split/pullmanifest.yaml</pre>
      </section>
    """


def summary_cards(template: dict[str, Any], result: backend.CompileResult) -> str:
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


def build_html(template_path: Path, recipes_path: Path, result: backend.CompileResult, auto_refresh: int = 0) -> str:
    template = backend.load_document(template_path) or {}
    recipes_doc = backend.load_document(recipes_path) or {}
    source_text = template_path.read_text(encoding="utf-8")
    finished_text = yaml_text(result.finished_yaml)
    refresh_meta = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh > 0 else ""
    data_json = html.escape(json.dumps({
        "errors": [m.to_dict() for m in result.errors],
        "warnings": [m.to_dict() for m in result.warnings],
        "analysis": result.analysis,
    }, indent=2), quote=False)
    recipe_defs = [recipe for recipe in recipes_doc.get("recipes", []) or [] if recipe.get("name")]
    recipe_names = [recipe.get("name") for recipe in recipe_defs]
    batching_recipes = [recipe for recipe in recipes_doc.get("batching_recipes", []) or [] if recipe.get("name")]
    batching_names = [recipe.get("name") for recipe in batching_recipes]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>YAML Manager</title>
  <style>{CSS}</style>
</head>
<body class="dark">
  <header>
    <div class="header-main">
      <h1>YAML Manager</h1>
      <form id="templatePathForm" class="header-path-form" method="get" action="/">
        <label for="templatePathInput">Template YAML</label>
        <input id="templatePathInput" name="template" type="text" value="{e(template_path)}">
        <input name="recipes" type="hidden" value="{e(recipes_path)}">
        <button id="refreshPage" type="submit" title="Reload dashboard">Refresh</button>
      </form>
    </div>
    <div class="header-actions">
      <button id="themeToggle" title="Toggle dark mode">Theme</button>
    </div>
  </header>

  <main>
    {summary_cards(template, result)}

    <nav class="tabs" aria-label="Dashboard sections">
      <button class="tab active" data-tab="validation">Validation</button>
      <button class="tab" data-tab="builder">Builder</button>
      <button class="tab" data-tab="pipeline">Pipeline</button>
      <button class="tab" data-tab="cohorts">Cohorts</button>
      <button class="tab" data-tab="recipes">Recipes</button>
      <button class="tab" data-tab="graph">Graph</button>
      <button class="tab" data-tab="exports">Exports</button>
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

    <section id="builder" class="panel">
      <div class="builder-layout">
        <aside class="builder-nav">
          <button class="builder-link active" data-builder-section="builderProject">Project</button>
          <button class="builder-link" data-builder-section="builderUploads">Uploads</button>
          <button class="builder-link" data-builder-section="builderBatching">Batching</button>
          <button class="builder-link" data-builder-section="builderCohorts">Cohorts</button>
          <button class="builder-link" data-builder-section="builderDraft">Draft YAML</button>
        </aside>
        <div class="builder-main">
          <section id="builderProject" class="builder-section active block">
            <h2>Project</h2>
            <div class="form-grid">
              <label>Project Folder<input id="builderProjectFolder" type="text"></label>
              <label>Project DB<input id="builderProjectDb" type="text"></label>
              <label>Cosmos DB
                <select id="builderCosmosDb">
                  <option value="COSMOS">COSMOS</option>
                  <option value="COSMOS_SneakPeek">COSMOS_SneakPeek</option>
                  <option value="Dual">Dual</option>
                </select>
              </label>
              <label>Min Date Key<input id="builderMinDate" type="text"></label>
              <label>Max Date Key<input id="builderMaxDate" type="text"></label>
            </div>
            <h3>Test Options</h3>
            <div class="form-grid">
              <label class="checkbox-label"><input id="builderSmallset" type="checkbox"> Small set</label>
              <label>PK Row Limit<input id="builderStopAtPk" type="number" min="0"></label>
              <label>Fact Row Limit<input id="builderStopAtNonPk" type="number" min="0"></label>
              <label class="checkbox-label"><input id="builderRandomPkSample" type="checkbox"> Random PK sample</label>
              <label class="checkbox-label"><input id="builderPrintoutMd" type="checkbox"> Print markdown</label>
            </div>
            <div class="toolbar compact">
              <button id="builderNewTemplate">New Blank Template</button>
              <button id="builderUseCurrent">Reload Current Template</button>
            </div>
          </section>

          <section id="builderUploads" class="builder-section block">
            <h2>Uploads</h2>
            <div class="inline-form">
              <select id="newUploadType">
                <option value="dbtable">dbtable</option>
                <option value="csv">csv</option>
                <option value="parquet">parquet</option>
              </select>
              <input id="newUploadName" type="text" placeholder="name">
              <input id="newUploadPath" type="text" placeholder="file_loc or table hint">
              <button id="addUpload">Add Upload</button>
            </div>
            <div id="uploadEditorRows" class="editor-rows"></div>
          </section>

          <section id="builderBatching" class="builder-section block">
            <h2>Batching</h2>
            <div class="inline-form">
              <select id="newBatchingRecipe"></select>
              <input id="newBatchingValue" type="text" placeholder="value or chunk size" list="batchingValueSuggestions">
              <button id="addBatching">Add Batching</button>
            </div>
            <datalist id="batchingValueSuggestions"></datalist>
            <p id="batchingHelp" class="helper-text"></p>
            <div id="batchingEditorRows" class="editor-rows"></div>
          </section>

          <section id="builderCohorts" class="builder-section block">
            <h2>Cohorts</h2>
            <h3>Recipes</h3>
            <div class="inline-form">
              <select id="newCohortRecipe"></select>
              <input id="newCohortName" type="text" placeholder="cohort name">
              <button id="addCohort">Add Recipe</button>
            </div>
            <div id="cohortEditorRows" class="editor-rows"></div>
            <h3>Custom</h3>
            <div class="custom-builder">
              <div id="pkWarning" class="message warn hidden">
                <div class="message-code">PK warning</div>
                <div class="message-body">This draft already has a PK cohort. Only one PK cohort should be used.</div>
              </div>
              <div class="form-grid">
                <label>Name<input id="customName" type="text" placeholder="Mothers"></label>
                <label>Destination<input id="customDestTable" type="text" placeholder="Mothers"></label>
                <label>Type
                  <select id="customType">
                    <option value="fact">fact</option>
                    <option value="PK">PK</option>
                  </select>
                </label>
                <label class="checkbox-label"><input id="customPullThisCycle" type="checkbox" checked> Pull this cycle</label>
                <label>From Table<input id="customFromTable" type="text" placeholder="BirthParentFact"></label>
                <label>AS<input id="customFromAlias" type="text" placeholder="bpf"></label>
              </div>
              <div class="subsection-head">
                <h4>Columns</h4>
                <button id="addCustomColumn">Add Column</button>
              </div>
              <div id="customColumnRows" class="editor-rows"></div>
              <div class="subsection-head">
                <h4>Joins</h4>
                <button id="addCustomJoin">Add Join</button>
              </div>
              <div id="customJoinRows" class="editor-rows"></div>
              <div class="subsection-head">
                <h4>Where</h4>
                <button id="addCustomWhere">Add Where</button>
              </div>
              <div id="customWhereRows" class="editor-rows"></div>
              <div class="toolbar compact">
                <button id="addCustomCohort">Add Custom Cohort</button>
                <button id="resetCustomCohort">Reset Custom Form</button>
                <button id="copyCustomRecipe">Copy Custom As Recipe</button>
                <button id="downloadCustomRecipe">Download Custom Recipe</button>
              </div>
            </div>
          </section>

          <section id="builderDraft" class="builder-section block">
            <h2>Draft YAML</h2>
            <div class="form-grid single">
              <label>Download Name<input id="builderDraftFilename" type="text" placeholder="Test_Run_Full.yaml"></label>
            </div>
            <div class="toolbar compact">
              <button id="copyDraftYaml">Copy Draft</button>
              <button id="downloadDraftYaml">Download Draft</button>
            </div>
            <pre id="draftYaml"></pre>
          </section>
        </div>
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

    <section id="exports" class="panel">
      {exports_panel(template_path, recipes_path)}
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

  <script id="initialTemplateData" type="application/json">{json_payload(template)}</script>
  <script id="recipeDefsData" type="application/json">{json_payload(recipe_defs)}</script>
  <script id="recipeNamesData" type="application/json">{json_payload(recipe_names)}</script>
  <script id="batchingNamesData" type="application/json">{json_payload(batching_names)}</script>
  <script id="batchingRecipesData" type="application/json">{json_payload(batching_recipes)}</script>
  <script>{JS}</script>
</body>
</html>
"""


CSS = r"""
:root {
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
body.light {
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
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { display: flex; justify-content: space-between; align-items: center; gap: 18px; padding: 16px 24px; border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 4; }
h1 { margin: 0; font-size: 22px; }
h2 { margin: 0 0 14px; font-size: 18px; }
h3 { margin: 0 0 12px; font-size: 15px; }
h4 { margin: 12px 0 8px; font-size: 13px; }
p { color: var(--muted); margin: 4px 0 0; }
button, input, select { font: inherit; }
button { border: 1px solid var(--line); background: var(--panel); color: var(--ink); padding: 8px 11px; border-radius: 7px; cursor: pointer; }
button:hover { border-color: var(--accent); }
input, select { border: 1px solid var(--line); border-radius: 7px; padding: 9px 11px; background: var(--panel); color: var(--ink); min-width: 0; }
.header-actions { display: flex; gap: 8px; align-items: center; }
.header-main { display: grid; gap: 8px; min-width: 0; flex: 1; }
.header-path-form { display: grid; grid-template-columns: max-content minmax(260px, 1fr) auto; gap: 10px; align-items: center; max-width: 980px; }
.header-path-form label { color: var(--muted); font-size: 12px; font-weight: 700; }
.header-path-form input { width: 100%; padding: 7px 9px; background: var(--bg); }
main { padding: 22px; max-width: 1500px; margin: 0 auto; }
.path-form { display: grid; grid-template-columns: minmax(240px, 1fr) auto; gap: 10px; align-items: end; margin-top: 12px; }
.path-form label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
.path-form input { width: 100%; }
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
.badge.ok, .badge.pk, .badge.upload { color: var(--ok); }
.badge.warn { color: var(--warn); }
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
.toolbar.compact { margin-top: 14px; margin-bottom: 0; flex-wrap: wrap; }
.export-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.export-head .toolbar { margin-top: 0; }
.export-block pre { max-height: 420px; }
.builder-layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 14px; align-items: start; }
.builder-nav { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; position: sticky; top: 88px; display: grid; gap: 8px; }
.builder-link { text-align: left; }
.builder-link.active { background: var(--accent); border-color: var(--accent); color: white; }
.builder-section { display: none; }
.builder-section.active { display: block; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.form-grid.single { grid-template-columns: minmax(220px, 520px); margin-bottom: 12px; }
.form-grid label, .editor-row label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
.checkbox-label { align-content: end; grid-template-columns: max-content 1fr; align-items: center; min-height: 62px; }
.form-grid input, .form-grid select, .editor-row input, .editor-row select { width: 100%; color: var(--ink); font-weight: 400; }
.inline-form { display: grid; grid-template-columns: minmax(140px, 190px) minmax(140px, 1fr) minmax(180px, 1.4fr) auto; gap: 8px; align-items: center; margin-bottom: 12px; }
.editor-rows { display: grid; gap: 10px; }
.editor-row { border: 1px solid var(--line); border-radius: 8px; padding: 10px; display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)) auto; gap: 8px; align-items: end; }
.editor-row.cohort { grid-template-columns: minmax(140px, 1fr) minmax(140px, 1fr) auto; }
.editor-row.batch { grid-template-columns: minmax(140px, 220px) minmax(220px, 1fr) auto; }
.editor-row.custom-column { grid-template-columns: repeat(4, minmax(110px, 1fr)) auto; }
.editor-row.line-editor { grid-template-columns: minmax(220px, 1fr) auto; }
.custom-builder { display: grid; gap: 12px; margin-top: 10px; }
.subsection-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 6px; }
.subsection-head h4 { margin: 0; }
.helper-text { margin: 0 0 12px; }
.tag-list { display: flex; flex-wrap: wrap; gap: 7px; min-height: 38px; align-items: center; }
.tag { display: inline-flex; gap: 6px; align-items: center; border: 1px solid var(--line); border-radius: 999px; background: var(--chip); padding: 5px 8px; color: var(--ink); }
.tag button { border: 0; background: transparent; padding: 0 2px; color: var(--muted); }
.tag-note { color: var(--muted); font-size: 12px; }
.summary-connections { display: block; margin-top: 6px; font-size: 12px; color: var(--muted); }
.connection-chips { display: inline-flex; flex-wrap: wrap; gap: 6px; vertical-align: middle; }
.connection-chip { display: inline-flex; align-items: center; border: 1px solid currentColor; border-radius: 999px; padding: 3px 7px; background: color-mix(in srgb, currentColor 16%, transparent); color: var(--accent); }
.connection-detail { border-left: 4px solid currentColor; background: var(--chip); border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; }
.column-ref { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: currentColor; margin-right: 6px; vertical-align: -1px; }
.c0 { color: #6fb1cf; }
.c1 { color: #74c69d; }
.c2 { color: #e4b363; }
.c3 { color: #ff8a8a; }
.c4 { color: #b99cff; }
.c5 { color: #7bdff2; }
.c6 { color: #f7a072; }
.c7 { color: #b8d8ba; }
.danger { color: var(--err); }
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
  .summary, .grid.two, .grid.three, .graph-layout, .builder-layout, .form-grid, .inline-form, .editor-row, .editor-row.cohort, .editor-row.batch { grid-template-columns: 1fr; }
  header { position: static; align-items: stretch; flex-direction: column; }
  .header-actions { align-self: flex-end; }
  .header-path-form { grid-template-columns: 1fr; }
  .builder-nav { position: static; }
}
"""


JS = r"""
const initialTemplate = JSON.parse(document.getElementById('initialTemplateData').textContent);
const recipeDefs = JSON.parse(document.getElementById('recipeDefsData').textContent);
const recipeNames = JSON.parse(document.getElementById('recipeNamesData').textContent);
const batchingNames = JSON.parse(document.getElementById('batchingNamesData').textContent);
const batchingRecipes = JSON.parse(document.getElementById('batchingRecipesData').textContent);
let draftTemplate = clone(initialTemplate);
let customDraft = blankCustomCohort();

function clone(value) {
  return JSON.parse(JSON.stringify(value || {}));
}

document.querySelectorAll('.tab').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    button.classList.add('active');
    document.getElementById(button.dataset.tab).classList.add('active');
  });
});

document.getElementById('themeToggle').addEventListener('click', () => {
  document.body.classList.toggle('light');
});

document.getElementById('templatePathForm')?.addEventListener('submit', event => {
  if (window.location.protocol === 'file:') {
    event.preventDefault();
    window.location.reload();
  }
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

document.addEventListener('click', event => {
  const target = event.target;
  if (target.dataset.showMoreList !== undefined) {
    const list = target.closest('ul');
    if (!list) return;
    list.querySelectorAll('.extra-item').forEach(item => item.classList.remove('hidden'));
    target.closest('li')?.remove();
  }
});

document.querySelectorAll('.builder-link').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.builder-link').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.builder-section').forEach(s => s.classList.remove('active'));
    button.classList.add('active');
    document.getElementById(button.dataset.builderSection).classList.add('active');
  });
});

function ensureDraftShape() {
  draftTemplate.cosmos_vars = draftTemplate.cosmos_vars || {};
  draftTemplate.run_vars = draftTemplate.run_vars || {};
  draftTemplate.project_vars = draftTemplate.project_vars || {};
  draftTemplate.test_options = draftTemplate.test_options || {};
  draftTemplate.vars = draftTemplate.vars || {};
  draftTemplate.upload_cohorts = Array.isArray(draftTemplate.upload_cohorts) ? draftTemplate.upload_cohorts : [];
  draftTemplate.batching = Array.isArray(draftTemplate.batching) ? draftTemplate.batching : [];
  draftTemplate.cohorts = Array.isArray(draftTemplate.cohorts) ? draftTemplate.cohorts : [];
}

function blankTemplate() {
  return {
    cosmos_vars: {
      project_db: 'PROJECTD93A57',
      cosmos_db: 'COSMOS'
    },
    run_vars: {
      min_date_key: '',
      max_date_key: ''
    },
    test_options: {
      smallset: false,
      stop_at_for_pk_table: 10,
      stop_at_for_non_pk_tables: 0,
      random_pk_sample: false,
      printout_md: true
    },
    project_vars: {
      project_folder: 'New Project'
    },
    vars: {},
    upload_cohorts: [],
    multipliers: [],
    batching: [],
    cohorts: []
  };
}

function hydrateBuilder() {
  ensureDraftShape();
  setValue('builderProjectFolder', draftTemplate.project_vars.project_folder || draftTemplate.project_folder || '');
  setValue('builderProjectDb', draftTemplate.cosmos_vars.project_db || draftTemplate.project_db || '');
  setValue('builderCosmosDb', draftTemplate.cosmos_vars.cosmos_db || draftTemplate.cosmos_db || 'COSMOS');
  setValue('builderMinDate', draftTemplate.run_vars.min_date_key || draftTemplate.vars.min_date_key || '');
  setValue('builderMaxDate', draftTemplate.run_vars.max_date_key || draftTemplate.vars.max_date_key || '');
  setChecked('builderSmallset', Boolean(draftTemplate.test_options.smallset));
  setValue('builderStopAtPk', draftTemplate.test_options.stop_at_for_pk_table ?? '');
  setValue('builderStopAtNonPk', draftTemplate.test_options.stop_at_for_non_pk_tables ?? '');
  setChecked('builderRandomPkSample', Boolean(draftTemplate.test_options.random_pk_sample));
  setChecked('builderPrintoutMd', draftTemplate.test_options.printout_md !== false);
  renderRecipeOptions();
  renderBatchingOptions();
  renderCustomBuilder();
  renderUploadRows();
  renderBatchingRows();
  renderCohortRows();
  updateDraftYaml();
}

function setValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value;
}

function setChecked(id, value) {
  const el = document.getElementById(id);
  if (el) el.checked = Boolean(value);
}

function getValue(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : '';
}

function getChecked(id) {
  const el = document.getElementById(id);
  return Boolean(el?.checked);
}

function numericOrZero(value) {
  const text = String(value ?? '').trim();
  if (text === '') return 0;
  const number = Number(text);
  return Number.isFinite(number) ? number : 0;
}

function syncProjectFields() {
  ensureDraftShape();
  draftTemplate.project_vars.project_folder = getValue('builderProjectFolder');
  draftTemplate.cosmos_vars.project_db = getValue('builderProjectDb');
  draftTemplate.cosmos_vars.cosmos_db = getValue('builderCosmosDb') || 'COSMOS';
  draftTemplate.run_vars.min_date_key = getValue('builderMinDate');
  draftTemplate.run_vars.max_date_key = getValue('builderMaxDate');
  draftTemplate.test_options.smallset = getChecked('builderSmallset');
  draftTemplate.test_options.stop_at_for_pk_table = numericOrZero(getValue('builderStopAtPk'));
  draftTemplate.test_options.stop_at_for_non_pk_tables = numericOrZero(getValue('builderStopAtNonPk'));
  draftTemplate.test_options.random_pk_sample = getChecked('builderRandomPkSample');
  draftTemplate.test_options.printout_md = getChecked('builderPrintoutMd');
  updateDraftYaml();
}

['builderProjectFolder', 'builderProjectDb', 'builderCosmosDb', 'builderMinDate', 'builderMaxDate', 'builderSmallset', 'builderStopAtPk', 'builderStopAtNonPk', 'builderRandomPkSample', 'builderPrintoutMd'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('input', syncProjectFields);
  if (el) el.addEventListener('change', syncProjectFields);
});

['customName', 'customDestTable', 'customType', 'customPullThisCycle', 'customFromTable', 'customFromAlias'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('input', syncCustomFields);
  if (el) el.addEventListener('change', syncCustomFields);
});

document.getElementById('builderDraftFilename')?.addEventListener('input', updateDraftYaml);
document.getElementById('newBatchingRecipe')?.addEventListener('change', updateBatchingHelp);

function renderRecipeOptions() {
  const select = document.getElementById('newCohortRecipe');
  if (!select) return;
  select.innerHTML = [
    ...recipeNames.map(name => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`),
    '<option value="__custom__">Custom</option>'
  ].join('');
}

function renderBatchingOptions() {
  const select = document.getElementById('newBatchingRecipe');
  if (!select) return;
  select.innerHTML = batchingNames.map(name => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`).join('');
  updateBatchingHelp();
}

function renderUploadRows() {
  const root = document.getElementById('uploadEditorRows');
  if (!root) return;
  root.innerHTML = draftTemplate.upload_cohorts.map((upload, index) => `
    <div class="editor-row">
      <label>Name<input data-upload-field="name" data-index="${index}" value="${escapeAttr(upload.name || '')}"></label>
      <label>Destination<input data-upload-field="dest_table" data-index="${index}" value="${escapeAttr(upload.dest_table || upload.name || '')}"></label>
      <label>Type
        <select data-upload-field="file_type" data-index="${index}">
          ${['dbtable', 'csv', 'parquet'].map(type => `<option value="${type}" ${type === upload.file_type ? 'selected' : ''}>${type}</option>`).join('')}
        </select>
      </label>
      <label>File/Table<input data-upload-field="file_loc" data-index="${index}" value="${escapeAttr(upload.file_loc || '')}"></label>
      <button class="danger" data-remove-upload="${index}">Remove</button>
    </div>
  `).join('') || '<div class="empty">No uploads in draft.</div>';
}

function renderBatchingRows() {
  const root = document.getElementById('batchingEditorRows');
  if (!root) return;
  root.innerHTML = draftTemplate.batching.map((item, index) => {
    const parsed = describeBatching(item);
    const isChunk = parsed.name === 'chunk';
    const tags = parsed.values.map((value, valueIndex) => `
      <span class="tag">${escapeHtml(value)} <button title="Remove value" data-remove-batching-value="${index}" data-value-index="${valueIndex}">x</button></span>
    `).join('');
    return `
      <div class="editor-row batch">
        <label>Recipe
          <select data-batching-field="name" data-index="${index}">
            ${batchingNames.map(name => `<option value="${escapeAttr(name)}" ${name === parsed.name ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('')}
          </select>
        </label>
        <label>${isChunk ? 'Rows Per Batch' : 'Values'}
          ${isChunk
            ? `<input data-batching-field="chunk" data-index="${index}" value="${escapeAttr(parsed.chunk || '')}" placeholder="2000">`
            : `<div class="tag-list">${tags || '<span class="tag-note">All values. Explicit picks will also create an all-other batch.</span>'}</div>
               <input data-batching-add-value="${index}" value="" placeholder="type value and press Enter">`}
        </label>
        <button class="danger" data-remove-batching="${index}">Remove</button>
      </div>
    `;
  }).join('') || '<div class="empty">No batching rules in draft.</div>';
}

function renderCohortRows() {
  const root = document.getElementById('cohortEditorRows');
  if (!root) return;
  root.innerHTML = draftTemplate.cohorts.map((cohort, index) => `
    <div class="editor-row cohort">
      <label>${cohort.recipe ? 'Recipe' : 'Custom'}<input data-cohort-field="${cohort.recipe ? 'recipe' : 'type'}" data-index="${index}" value="${escapeAttr(cohort.recipe || cohort.type || '')}"></label>
      <label>Name<input data-cohort-field="name" data-index="${index}" value="${escapeAttr(cohort.name || '')}"></label>
      <button class="danger" data-remove-cohort="${index}">Remove</button>
    </div>
  `).join('') || '<div class="empty">No cohorts in draft.</div>';
  updatePkWarning();
}

function blankCustomCohort() {
  return {
    name: '',
    dest_table: '',
    type: 'fact',
    pull_this_cycle: true,
    columns: [],
    filter: {
      from_table: '',
      from_alias: '',
      join: [],
      where: []
    }
  };
}

function renderCustomBuilder() {
  setValue('customName', customDraft.name || '');
  setValue('customDestTable', customDraft.dest_table || '');
  setValue('customType', customDraft.type || 'fact');
  const pull = document.getElementById('customPullThisCycle');
  if (pull) pull.checked = customDraft.pull_this_cycle !== false;
  setValue('customFromTable', customDraft.filter.from_table || '');
  setValue('customFromAlias', customDraft.filter.from_alias || '');
  renderCustomColumnRows();
  renderCustomLineRows('customJoinRows', 'join', 'join condition');
  renderCustomLineRows('customWhereRows', 'where', 'where condition');
  updatePkWarning();
}

function renderCustomColumnRows() {
  const root = document.getElementById('customColumnRows');
  if (!root) return;
  root.innerHTML = customDraft.columns.map((column, index) => `
    <div class="editor-row custom-column">
      <label>Source<input data-custom-column-field="source" data-index="${index}" value="${escapeAttr(column.source || '')}"></label>
      <label>Name<input data-custom-column-field="name" data-index="${index}" value="${escapeAttr(column.name || '')}"></label>
      <label>Type<input data-custom-column-field="type" data-index="${index}" value="${escapeAttr(column.type || '')}" placeholder="optional"></label>
      <label>Nullable
        <select data-custom-column-field="nullable" data-index="${index}">
          <option value="" ${column.nullable === undefined || column.nullable === '' ? 'selected' : ''}>blank</option>
          <option value="true" ${column.nullable === true || column.nullable === 'true' ? 'selected' : ''}>true</option>
          <option value="false" ${column.nullable === false || column.nullable === 'false' ? 'selected' : ''}>false</option>
        </select>
      </label>
      <button class="danger" data-remove-custom-column="${index}">Remove</button>
    </div>
  `).join('') || '<div class="empty">No custom columns yet.</div>';
}

function renderCustomLineRows(rootId, field, placeholder) {
  const root = document.getElementById(rootId);
  if (!root) return;
  root.innerHTML = customDraft.filter[field].map((line, index) => `
    <div class="editor-row line-editor">
      <label>${field}<input data-custom-line-field="${field}" data-index="${index}" value="${escapeAttr(line || '')}" placeholder="${escapeAttr(placeholder)}"></label>
      <button class="danger" data-remove-custom-line="${field}" data-index="${index}">Remove</button>
    </div>
  `).join('') || `<div class="empty">No ${escapeHtml(field)} lines yet.</div>`;
}

function syncCustomFields() {
  customDraft.name = getValue('customName');
  customDraft.dest_table = getValue('customDestTable');
  customDraft.type = getValue('customType') || 'fact';
  customDraft.pull_this_cycle = !!document.getElementById('customPullThisCycle')?.checked;
  customDraft.filter.from_table = getValue('customFromTable');
  customDraft.filter.from_alias = getValue('customFromAlias');
  updatePkWarning();
}

function makeCustomCohort() {
  syncCustomFields();
  const cohort = {
    name: customDraft.name || 'CustomCohort',
    dest_table: customDraft.dest_table || customDraft.name || 'CustomCohort',
    type: customDraft.type || 'fact',
    pull_this_cycle: customDraft.pull_this_cycle !== false,
    columns: customDraft.columns
      .filter(column => column.source || column.name || column.type || column.nullable !== undefined)
      .map(cleanColumn),
    filter: {}
  };
  const fromTable = customDraft.filter.from_table;
  const fromAlias = customDraft.filter.from_alias;
  if (fromTable) cohort.filter.from = [fromAlias ? `${fromTable} as ${fromAlias}` : fromTable];
  const joins = customDraft.filter.join.map(line => line.trim()).filter(Boolean);
  const wheres = customDraft.filter.where.map(line => line.trim()).filter(Boolean);
  if (joins.length) cohort.filter.join = joins;
  if (wheres.length) cohort.filter.where = wheres;
  return cohort;
}

function cleanColumn(column) {
  const clean = {};
  if (column.source) clean.source = column.source;
  if (column.name) clean.name = column.name;
  if (column.type) clean.type = column.type;
  if (column.nullable !== undefined && column.nullable !== '') clean.nullable = column.nullable === true || column.nullable === 'true';
  return clean;
}

function updatePkWarning() {
  const warning = document.getElementById('pkWarning');
  if (!warning) return;
  const existingPk = draftTemplate.cohorts.some(cohort => {
    if (String(cohort.type || '').toLowerCase() === 'pk') return true;
    const recipe = recipeDefs.find(item => item.name === cohort.recipe);
    return String(recipe?.type || '').toLowerCase() === 'pk';
  });
  const customPk = String(getValue('customType') || customDraft.type || '').toLowerCase() === 'pk';
  warning.classList.toggle('hidden', !(existingPk && customPk));
}

function customRecipeText() {
  const cohort = makeCustomCohort();
  const recipeName = cohort.name || 'CustomRecipe';
  const recipe = clone(cohort);
  recipe.name = recipeName;
  delete recipe.recipe;
  return toYaml({ recipes: [recipe] });
}

function describeBatching(item) {
  if (typeof item === 'number') return { name: 'chunk', chunk: String(item), values: [] };
  if (typeof item === 'string') return { name: item, chunk: '', values: [] };
  if (item && typeof item === 'object') {
    if ('chunk' in item) return { name: 'chunk', chunk: String(item.chunk), values: [] };
    if (item.name) return { name: item.name, chunk: String(item.rows_per_batch || ''), values: Array.isArray(item.values) ? item.values : [] };
    const keys = Object.keys(item);
    if (keys.length === 1) {
      const name = keys[0];
      const value = item[name];
      if (value && typeof value === 'object') return { name, chunk: String(value.rows_per_batch || ''), values: Array.isArray(value.values) ? value.values : [] };
      return { name, chunk: '', values: value == null ? [] : [String(value)] };
    }
  }
  return { name: '', chunk: '', values: [] };
}

function batchingFromFields(name, value) {
  if (name === 'chunk') {
    const parsed = parseInt(value || '0', 10);
    return { chunk: Number.isFinite(parsed) && parsed > 0 ? parsed : 2000 };
  }
  const values = Array.isArray(value) ? value : String(value || '').split(',').map(v => v.trim()).filter(Boolean);
  if (values.length) {
    return { [name]: { values, include_other: true } };
  }
  return name;
}

function updateBatchingHelp() {
  const name = getValue('newBatchingRecipe');
  const help = document.getElementById('batchingHelp');
  const list = document.getElementById('batchingValueSuggestions');
  const recipe = batchingRecipes.find(item => item.name === name) || {};
  if (help) {
    help.textContent = name === 'chunk'
      ? 'Enter the number of PK rows per batch.'
      : `Leave blank to batch by all ${name || 'selected'} values. If selecting specific values, all other values will be batched together.`;
  }
  if (list) {
    const values = Array.isArray(recipe.values) ? recipe.values : [];
    list.innerHTML = values.map(value => `<option value="${escapeAttr(value)}"></option>`).join('');
  }
}

document.getElementById('addUpload')?.addEventListener('click', () => {
  ensureDraftShape();
  const name = getValue('newUploadName') || `Upload${draftTemplate.upload_cohorts.length + 1}`;
  const fileType = getValue('newUploadType') || 'csv';
  const upload = {
    name,
    dest_table: name,
    file_type: fileType,
    push_this_cycle: true
  };
  const fileLoc = getValue('newUploadPath');
  if (fileLoc) upload.file_loc = fileLoc;
  draftTemplate.upload_cohorts.push(upload);
  setValue('newUploadName', '');
  setValue('newUploadPath', '');
  renderUploadRows();
  updateDraftYaml();
});

document.getElementById('addBatching')?.addEventListener('click', () => {
  ensureDraftShape();
  const name = getValue('newBatchingRecipe');
  if (!name) return;
  draftTemplate.batching.push(batchingFromFields(name, getValue('newBatchingValue')));
  setValue('newBatchingValue', '');
  renderBatchingRows();
  updateDraftYaml();
});

document.getElementById('addCohort')?.addEventListener('click', () => {
  ensureDraftShape();
  const recipe = getValue('newCohortRecipe');
  if (!recipe) return;
  if (recipe === '__custom__') {
    document.getElementById('customName')?.focus();
    return;
  }
  draftTemplate.cohorts.push({ recipe, name: getValue('newCohortName') || recipe });
  setValue('newCohortName', '');
  renderCohortRows();
  updateDraftYaml();
});

document.getElementById('addCustomColumn')?.addEventListener('click', () => {
  customDraft.columns.push({ source: '', name: '', type: '', nullable: '' });
  renderCustomColumnRows();
});

document.getElementById('addCustomJoin')?.addEventListener('click', () => {
  customDraft.filter.join.push('');
  renderCustomLineRows('customJoinRows', 'join', 'join condition');
});

document.getElementById('addCustomWhere')?.addEventListener('click', () => {
  customDraft.filter.where.push('');
  renderCustomLineRows('customWhereRows', 'where', 'where condition');
});

document.getElementById('addCustomCohort')?.addEventListener('click', () => {
  ensureDraftShape();
  draftTemplate.cohorts.push(makeCustomCohort());
  customDraft = blankCustomCohort();
  renderCustomBuilder();
  renderCohortRows();
  updateDraftYaml();
});

document.getElementById('resetCustomCohort')?.addEventListener('click', () => {
  customDraft = blankCustomCohort();
  renderCustomBuilder();
});

document.getElementById('copyCustomRecipe')?.addEventListener('click', async () => {
  const text = customRecipeText();
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    window.prompt('Copy custom recipe YAML:', text);
  }
});

document.getElementById('downloadCustomRecipe')?.addEventListener('click', () => {
  const text = customRecipeText();
  const blob = new Blob([text], { type: 'text/yaml' });
  const a = document.createElement('a');
  const name = cleanDownloadName(`recipe_${makeCustomCohort().name || 'custom'}.yaml`);
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
});

document.addEventListener('input', event => {
  const target = event.target;
  const index = Number(target.dataset.index);
  if (target.dataset.uploadField) {
    draftTemplate.upload_cohorts[index][target.dataset.uploadField] = target.value;
    updateDraftYaml();
  }
  if (target.dataset.batchingField) {
    const parsed = describeBatching(draftTemplate.batching[index]);
    if (target.dataset.batchingField === 'name') {
      draftTemplate.batching[index] = batchingFromFields(target.value, parsed.name === 'chunk' ? parsed.chunk : parsed.values);
      renderBatchingRows();
    } else {
      draftTemplate.batching[index] = batchingFromFields(parsed.name, target.value);
    }
    updateDraftYaml();
  }
  if (target.dataset.cohortField) {
    draftTemplate.cohorts[index][target.dataset.cohortField] = target.value;
    updateDraftYaml();
  }
  if (target.dataset.customColumnField) {
    const column = customDraft.columns[index];
    const field = target.dataset.customColumnField;
    column[field] = target.value;
  }
  if (target.dataset.customLineField) {
    customDraft.filter[target.dataset.customLineField][index] = target.value;
  }
});

document.addEventListener('change', event => {
  const target = event.target;
  const index = Number(target.dataset.index);
  if (target.dataset.uploadField) {
    draftTemplate.upload_cohorts[index][target.dataset.uploadField] = target.value;
    updateDraftYaml();
  }
  if (target.dataset.batchingField) {
    const parsed = describeBatching(draftTemplate.batching[index]);
    if (target.dataset.batchingField === 'name') {
      draftTemplate.batching[index] = batchingFromFields(target.value, parsed.name === 'chunk' ? parsed.chunk : parsed.values);
      renderBatchingRows();
    } else {
      draftTemplate.batching[index] = batchingFromFields(parsed.name, target.value);
    }
    updateDraftYaml();
  }
  if (target.dataset.customColumnField) {
    const column = customDraft.columns[index];
    const field = target.dataset.customColumnField;
    column[field] = target.value;
  }
});

document.addEventListener('click', event => {
  const target = event.target;
  if (target.dataset.copyArtifact) {
    const text = document.getElementById(target.dataset.copyArtifact)?.innerText || '';
    navigator.clipboard.writeText(text).catch(() => window.prompt('Copy artifact:', text));
  }
  if (target.dataset.downloadArtifact) {
    const text = document.getElementById(target.dataset.downloadArtifact)?.innerText || '';
    const blob = new Blob([text], { type: 'text/yaml' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = cleanDownloadName(target.dataset.filename || 'artifact.yaml');
    a.click();
    URL.revokeObjectURL(a.href);
  }
  if (target.dataset.removeUpload) {
    draftTemplate.upload_cohorts.splice(Number(target.dataset.removeUpload), 1);
    renderUploadRows();
    updateDraftYaml();
  }
  if (target.dataset.removeBatching) {
    draftTemplate.batching.splice(Number(target.dataset.removeBatching), 1);
    renderBatchingRows();
    updateDraftYaml();
  }
  if (target.dataset.removeBatchingValue) {
    const index = Number(target.dataset.removeBatchingValue);
    const valueIndex = Number(target.dataset.valueIndex);
    const parsed = describeBatching(draftTemplate.batching[index]);
    parsed.values.splice(valueIndex, 1);
    draftTemplate.batching[index] = batchingFromFields(parsed.name, parsed.values);
    renderBatchingRows();
    updateDraftYaml();
  }
  if (target.dataset.removeCohort) {
    draftTemplate.cohorts.splice(Number(target.dataset.removeCohort), 1);
    renderCohortRows();
    updateDraftYaml();
  }
  if (target.dataset.removeCustomColumn) {
    customDraft.columns.splice(Number(target.dataset.removeCustomColumn), 1);
    renderCustomColumnRows();
  }
  if (target.dataset.removeCustomLine) {
    customDraft.filter[target.dataset.removeCustomLine].splice(Number(target.dataset.index), 1);
    renderCustomLineRows(target.dataset.removeCustomLine === 'join' ? 'customJoinRows' : 'customWhereRows', target.dataset.removeCustomLine, `${target.dataset.removeCustomLine} condition`);
  }
});

document.addEventListener('keydown', event => {
  const target = event.target;
  if (target.dataset.batchingAddValue && event.key === 'Enter') {
    event.preventDefault();
    const value = target.value.trim();
    if (!value) return;
    const index = Number(target.dataset.batchingAddValue);
    const parsed = describeBatching(draftTemplate.batching[index]);
    if (!parsed.values.includes(value)) parsed.values.push(value);
    draftTemplate.batching[index] = batchingFromFields(parsed.name, parsed.values);
    renderBatchingRows();
    updateDraftYaml();
  }
});

document.getElementById('builderNewTemplate')?.addEventListener('click', () => {
  draftTemplate = blankTemplate();
  hydrateBuilder();
});

document.getElementById('builderUseCurrent')?.addEventListener('click', () => {
  draftTemplate = clone(initialTemplate);
  hydrateBuilder();
});

document.getElementById('copyDraftYaml')?.addEventListener('click', async () => {
  const text = document.getElementById('draftYaml')?.innerText || '';
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    window.prompt('Copy draft YAML:', text);
  }
});

document.getElementById('downloadDraftYaml')?.addEventListener('click', () => {
  const text = document.getElementById('draftYaml')?.innerText || '';
  const blob = new Blob([text], { type: 'text/yaml' });
  const a = document.createElement('a');
  const projectName = draftTemplate.project_vars?.project_folder || draftTemplate.project_folder || 'draft';
  const project = projectName.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'draft';
  const requested = getValue('builderDraftFilename');
  const filename = cleanDownloadName(requested || `${project}_Full.yaml`);
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
});

function cleanDownloadName(value) {
  const cleaned = String(value || 'draft.yaml')
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return /\.ya?ml$/i.test(cleaned) ? cleaned : `${cleaned || 'draft'}.yaml`;
}

function updateDraftYaml() {
  ensureDraftShape();
  const clean = {
    cosmos_vars: draftTemplate.cosmos_vars || {},
    run_vars: draftTemplate.run_vars || {},
    test_options: draftTemplate.test_options || {},
    project_vars: draftTemplate.project_vars || {},
    vars: draftTemplate.vars || {},
    upload_cohorts: draftTemplate.upload_cohorts || [],
    multipliers: draftTemplate.multipliers || [],
    batching: draftTemplate.batching || [],
    cohorts: draftTemplate.cohorts || []
  };
  document.getElementById('draftYaml').textContent = toYaml(clean);
}

function toYaml(value, indent = 0) {
  const pad = ' '.repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return value.map(item => {
      if (item && typeof item === 'object' && !Array.isArray(item)) {
        const entries = Object.entries(item);
        if (entries.length === 0) return `${pad}- {}`;
        return entries.map(([key, val], idx) => {
          const prefix = idx === 0 ? `${pad}- ${key}:` : `${pad}  ${key}:`;
          if (val && typeof val === 'object') return `${prefix}\n${toYaml(val, indent + 4)}`;
          return `${prefix} ${formatScalar(val)}`;
        }).join('\n');
      }
      if (Array.isArray(item)) return `${pad}-\n${toYaml(item, indent + 2)}`;
      return `${pad}- ${formatScalar(item)}`;
    }).join('\n');
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value).filter(([, val]) => val !== undefined);
    if (entries.length === 0) return '{}';
    return entries.map(([key, val]) => {
      if (val && typeof val === 'object') return `${pad}${key}:\n${toYaml(val, indent + 2)}`;
      return `${pad}${key}: ${formatScalar(val)}`;
    }).join('\n');
  }
  return `${pad}${formatScalar(value)}`;
}

function formatScalar(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return String(value);
  const text = String(value);
  if (text === '') return '""';
  if (/[:#{}\[\]%,]|^\s|\s$/.test(text)) return JSON.stringify(text);
  return text;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function escapeAttr(value) {
  return escapeHtml(value);
}

hydrateBuilder();
"""


def render_dashboard(template_path: Path, recipes_path: Path, auto_refresh: int = 0) -> tuple[str, backend.CompileResult]:
    result = backend.compile_dashboard(template_path=template_path, recipes_path=recipes_path, write=False)
    return build_html(template_path, recipes_path, result, auto_refresh), result


def resolve_workspace_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def url_host(host: str) -> str:
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def network_hosts() -> list[str]:
    ipv4_hosts: list[str] = []
    ipv6_hosts: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified:
            continue
        hosts = ipv4_hosts if parsed.version == 4 else ipv6_hosts
        if address not in hosts:
            hosts.append(address)
    return (ipv4_hosts + ipv6_hosts)[:8]


def dashboard_url(host: str, port: int, template: str, recipes: str) -> str:
    query = urllib.parse.urlencode({"template": template, "recipes": recipes})
    return f"http://{url_host(host)}:{port}/?{query}"


def error_html(title: str, message: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)}</title>
  <style>{CSS}</style>
</head>
<body class="dark">
  <main>
    <section class="block">
      <h1>{e(title)}</h1>
      <p>{e(message)}</p>
      <form class="path-form" method="get" action="/">
        <label>Template YAML<input name="template" type="text" value="YAMLs/template.yaml"></label>
        <button type="submit">Refresh</button>
      </form>
    </section>
  </main>
</body>
</html>
"""


def serve_dashboard(
    host: str,
    port: int,
    template: str,
    recipes: str,
    auto_refresh: int,
    open_browser: bool,
    browser_host: str | None = None,
) -> int:
    default_template = template
    default_recipes = recipes

    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("/", "/dashboard", "/dashboard.html"):
                self.send_error(404)
                return
            params = urllib.parse.parse_qs(parsed.query)
            template_arg = params.get("template", [default_template])[0] or default_template
            recipes_arg = params.get("recipes", [default_recipes])[0] or default_recipes
            template_path = resolve_workspace_path(template_arg)
            recipes_path = resolve_workspace_path(recipes_arg)
            try:
                html_text, _ = render_dashboard(template_path, recipes_path, auto_refresh)
                status = 200
            except Exception as exc:  # noqa: BLE001 - surfaced as a dashboard page for local UI use.
                html_text = error_html("Dashboard Refresh Failed", str(exc))
                status = 500
            data = html_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[yamlmanager] {self.address_string()} - {format % args}")

    class DashboardServer(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    try:
        server = DashboardServer((host, port), DashboardHandler)
    except OSError as exc:
        print(f"[yamlmanager] Could not bind to {host}:{port}: {exc}", file=sys.stderr)
        print("[yamlmanager] Try --host 127.0.0.1 for SSH tunnel use, --public for VM/LAN access, or --port 0 for a free port.", file=sys.stderr)
        return 1

    actual_host, actual_port = server.server_address[:2]
    display_host = browser_host or actual_host
    url = dashboard_url(display_host, int(actual_port), default_template, default_recipes)
    print(f"Serving YAML Manager at {url}")
    if actual_host in ("", "0.0.0.0", "::"):
        extra_urls = [dashboard_url(host, int(actual_port), default_template, default_recipes) for host in network_hosts()]
        if extra_urls:
            print("Other reachable URLs may include:")
            for extra_url in extra_urls:
                print(f"  {extra_url}")
    print("Stop the manager with Ctrl+C, or the stop button in your editor.")
    if open_browser:
        if not webbrowser.open(url):
            print("[yamlmanager] No browser was opened automatically; copy the URL above into a browser.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped YAML Manager.")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a static YAML Manager UI.")
    parser.add_argument("--template", default="YAMLs/template.yaml")
    parser.add_argument("--recipes", default="YAMLs/recipes.yaml")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--open", action="store_true", help="Open the generated dashboard in a browser.")
    parser.add_argument("--serve", action="store_true", help="Run a local dashboard server so template paths can be changed in the UI.")
    parser.add_argument("--static", action="store_true", help="Write a static dashboard file instead of starting the local server when no args are provided.")
    parser.add_argument("--export-preyaml", choices=("symbolic", "expanded-recipes"), help="Write a pre-YAML artifact and exit.")
    parser.add_argument("--export-split", action="store_true", help="Write split YAML artifacts and pullmanifest.yaml, then exit.")
    parser.add_argument("--out-dir", help="Directory for split export artifacts.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Address to bind. Defaults to {DEFAULT_HOST!r}, or YAMLMANAGER_HOST/TELESCOPE_MANAGER_HOST/MANAGER_UI_HOST.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind. Use 0 for a free port. Defaults to {DEFAULT_PORT}, or YAMLMANAGER_PORT/TELESCOPE_MANAGER_PORT/MANAGER_UI_PORT.")
    parser.add_argument("--public", action="store_true", help="Bind to all interfaces unless --host is also supplied.")
    parser.add_argument("--browser-host", help="Host name to use in printed/opened URLs when it differs from the bind address.")
    parser.add_argument("--no-open", action="store_true", help="Do not try to open a browser when serving with no arguments.")
    parser.add_argument("--auto-refresh", type=int, default=0, help="Add browser auto-refresh, in seconds. Use 5 for every five seconds.")
    raw_argv = sys.argv[1:] if argv is None else argv
    open_by_default = not raw_argv
    args = parser.parse_args(raw_argv)
    explicit_host = any(arg == "--host" or arg.startswith("--host=") for arg in raw_argv)
    explicit_out = any(arg == "--out" or arg.startswith("--out=") for arg in raw_argv)
    if args.public and not explicit_host:
        args.host = "0.0.0.0"

    if args.export_preyaml:
        result = backend.build_preyaml(
            template_path=resolve_workspace_path(args.template),
            recipes_path=resolve_workspace_path(args.recipes),
            output_path=args.out if explicit_out else None,
            mode=args.export_preyaml,
            write=True,
        )
        print_messages(result)
        if result.ok:
            print(f"Wrote {result.output_path}")
        else:
            print("FAILED: errors block pre-YAML export")
        return 0 if result.ok else 1

    if args.export_split:
        result = backend.write_split_artifacts(
            template_path=resolve_workspace_path(args.template),
            recipes_path=resolve_workspace_path(args.recipes),
            output_dir=args.out_dir,
        )
        print_messages(result)
        if result.ok:
            print(f"Wrote split artifacts to {result.analysis.get('split_output_dir')}")
            print(f"Manifest: {result.output_path}")
        else:
            print("FAILED: errors block split export")
        return 0 if result.ok else 1

    if args.serve or (open_by_default and not args.static):
        return serve_dashboard(
            args.host,
            args.port,
            args.template,
            args.recipes,
            args.auto_refresh,
            open_browser=not args.static and not args.no_open,
            browser_host=args.browser_host,
        )

    template_path = resolve_workspace_path(args.template)
    recipes_path = resolve_workspace_path(args.recipes)

    out_path = Path(args.out)
    html_text, result = render_dashboard(template_path, recipes_path, args.auto_refresh)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")

    print(f"Wrote {out_path}")
    print(f"Status: {'Ready' if result.ok else 'Blocked'}")
    print(f"Errors: {len(result.errors)}")
    print(f"Warnings: {len(result.warnings)}")
    if args.open or open_by_default:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
