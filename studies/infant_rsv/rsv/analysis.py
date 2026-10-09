"""The analyses: load the visits, pick a section, and make each page."""

from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np
import pandas as pd

from .build import VISITS_FILE
from .config import RsvError, Settings, load_settings
from .page import Page, data_version, fit, number, p_text

# Each metric: its column, its label, and whether it is yes/no.
METRICS: list[tuple[str, str, bool]] = [
    ("rr_initial", "RR initial", False),
    ("rr_max_ed", "RR max, ED", False),
    ("rr_max_stay", "RR max, stay", False),
    ("spo2_min_ed", "SpO2 min, ED", False),
    ("spo2_min_stay", "SpO2 min, stay", False),
    ("temp_initial", "Temp C initial", False),
    ("temp_max_ed", "Temp C max, ED", False),
    ("temp_max_stay", "Temp C max, stay", False),
    ("ga_weeks", "GA weeks", False),
    ("age_months", "Age months", False),
    ("los_days", "LOS days (admitted)", False),
    ("vbg_ed", "VBG in ED", True),
    ("iv_fluids_ed", "IV fluids in ED", True),
    ("admitted", "Admitted", True),
    ("icu", "ICU", True),
]
DESCRIBE = ["age_band", "ga_band", "sex", "race", "ethnicity", "svi_quartile", "financial_class", "dx_group", "era"]


def visits_path(settings: Settings):
    return settings.analysis_folder / VISITS_FILE


def load(settings: Settings | None = None, sp: bool = False) -> pd.DataFrame:
    """visits.parquet, as `build` wrote it."""
    settings = settings or load_settings(sp=sp)
    path = visits_path(settings)
    if not path.is_file():
        raise RsvError(f"No {path}. Run `python rsv build{' --sp' if settings.sp else ''}` first.")
    return pd.read_parquet(path)


def section(visits: pd.DataFrame, settings: Settings, name: str | None) -> tuple[pd.DataFrame, str, str]:
    """The visits in a named section, its name and its filter."""
    if not name or name == "all":
        return visits, "all visits", ""
    sections = settings.get("sections") or {}
    if name not in sections:
        raise RsvError(f"No section `{name}`. Sections: {', '.join(sections) or 'none'} (settings.yaml `sections`).")
    expression = str(sections[name])
    try:
        mask = visits.eval(expression)
    except Exception as exc:
        raise RsvError(f"Section `{name}` (`{expression}`) cannot be read: {exc}") from exc
    mask = pd.Series(mask, index=visits.index).astype("boolean").fillna(False).astype(bool)
    return visits[mask], name, expression


def available(series: pd.Series) -> bool:
    return series.notna().any()


def yes(series: pd.Series) -> pd.Series:
    return series.astype("boolean")


def summarize(visits: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Each metric's n, and its median and quartiles, or its yes count."""
    out: dict[str, dict[str, Any]] = {}
    for column, label, binary in METRICS:
        series = visits[column] if column in visits else pd.Series(dtype=float)
        if column == "los_days":
            series = series[visits["admitted"].astype(bool)] if "admitted" in visits else series
        known = series.dropna()
        entry: dict[str, Any] = {"label": label, "binary": binary, "n": int(len(known)), "of": int(len(series))}
        if binary:
            entry["yes"] = int(yes(known).sum()) if len(known) else 0
        elif len(known):
            values = pd.to_numeric(known, errors="coerce").astype(float)
            entry.update(median=float(values.median()), q1=float(values.quantile(0.25)),
                         q3=float(values.quantile(0.75)))
        out[column] = entry
    return out


def stat_text(page: Page, entry: dict[str, Any]) -> str:
    if not entry["n"]:
        return "not available"
    if entry["binary"]:
        return f"{page.count(entry['yes'])}/{page.count(entry['n'])} ({page.pct(entry['yes'], entry['n'])}%)"
    if not page.enough(entry["n"]):
        return "too few"
    return f"{number(entry['median'])} ({number(entry['q1'])}-{number(entry['q3'])})"


def new_page(settings: Settings, title: str, command: str, name: str, expression: str) -> Page:
    return Page(settings, title, command + (" --sp" if settings.sp else ""), section=name,
                section_filter=expression, version=data_version(visits_path(settings)))


# ---------------------------------------------------------------- report

def report(settings: Settings, section_name: str | None = None, visits: pd.DataFrame | None = None) -> Page:
    visits = load(settings) if visits is None else visits
    chosen, name, expression = section(visits, settings, section_name)
    page = new_page(settings, "report", f"report {section_name or ''}".strip(), name, expression)
    page.line(f"{'ED visits':<20}{page.count(len(chosen)):>12}")
    page.line(f"{'patients':<20}{page.count(chosen['PatientDurableKey'].nunique()):>12}")
    page.heading("Metric: median (IQR) or n/N (%)")
    for column, entry in summarize(chosen).items():
        page.line(fit(f"{entry['label']}", page.width))
        page.line(fit(f"   {stat_text(page, entry)}   n={page.count(entry['n'])}", page.width))
    for column in DESCRIBE:
        page.heading(column, f"{'n':>10} {'%':>6}", 41)
        series = chosen[column]
        if not available(series):
            page.line("  not available")
            continue
        for level, n in series.fillna("Unknown").value_counts().sort_index().items():
            page.line(f"  {fit(level, 22):<22}{page.count(n):>10} {page.pct(n, len(chosen)):>6}")
    return page


# ---------------------------------------------------------------- compare

def levels_of(chosen: pd.DataFrame, grouping: str) -> pd.Series:
    series = chosen[grouping]
    if series.dtype == bool or str(series.dtype) == "boolean":
        return series.astype("boolean").map({True: "yes", False: "no"}).astype("string").fillna("Unknown")
    return series.astype("string").fillna("Unknown")


def numeric_test(groups: list[pd.Series]) -> float | None:
    from scipy import stats
    groups = [g for g in groups if len(g) > 0]
    if len(groups) < 2:
        return None
    try:
        if len(groups) == 2:
            return float(stats.mannwhitneyu(groups[0], groups[1]).pvalue)
        return float(stats.kruskal(*groups).pvalue)
    except ValueError:
        return None


def table_test(table: pd.DataFrame) -> float | None:
    from scipy import stats
    table = table.loc[table.sum(axis=1) > 0, table.sum(axis=0) > 0]
    if table.shape[0] < 2 or table.shape[1] < 2:
        return None
    return float(stats.chi2_contingency(table.values)[1])


def compare(settings: Settings, grouping: str, section_name: str | None = None,
            visits: pd.DataFrame | None = None) -> Page:
    groupings = settings.get("groupings") or []
    if grouping not in groupings:
        raise RsvError(f"No grouping `{grouping}`. Groupings: {', '.join(groupings)} (settings.yaml `groupings`).")
    visits = load(settings) if visits is None else visits
    chosen, name, expression = section(visits, settings, section_name)
    page = new_page(settings, f"compare by {grouping}", f"compare {grouping} {section_name or ''}".strip(),
                    name, expression)
    if not available(chosen[grouping]):
        page.line(f"{grouping}: not available")
        return page
    levels = levels_of(chosen, grouping)
    order = sorted(levels.unique())
    page.heading(grouping, f"{'n':>10} {'%':>6}", 41)
    for level in order:
        n = int((levels == level).sum())
        page.line(f"  {fit(level, 22):<22}{page.count(n):>10} {page.pct(n, len(chosen)):>6}")
    lw = 18
    for column, label, binary in METRICS:
        if column == grouping or column not in chosen or not available(chosen[column]):
            continue
        frame = chosen
        if column == "los_days":
            frame = chosen[chosen["admitted"].astype(bool)]
        frame_levels = levels.loc[frame.index]
        known = frame[column].notna()
        if binary:
            values = yes(frame[column])
            table = pd.crosstab(frame_levels[known], values[known].astype(bool))
            p = table_test(table)
        else:
            values = pd.to_numeric(frame[column], errors="coerce")
            p = numeric_test([values[(frame_levels == lv) & known].astype(float) for lv in order])
        page.heading(f"{label}")
        page.line(fit(f"  {p_text(p)}   " + ("n/N (%)" if binary else "median (IQR)"), page.width))
        for level in order:
            here = (frame_levels == level) & known
            n = int(here.sum())
            if binary:
                k = int(values[here].astype(bool).sum())
                stat = f"{page.count(k)}/{page.count(n)} ({page.pct(k, n)})"
            elif n and page.enough(n):
                s = values[here].astype(float)
                stat = f"{number(s.median())} ({number(s.quantile(.25))}-{number(s.quantile(.75))})"
            else:
                stat = f"n {page.count(n)}"
            page.line(fit(f"  {fit(level, lw):<{lw}} {stat}", page.width))
    return page


# ---------------------------------------------------------------- admission (D212)

def pooled(series: pd.Series, minimum: int) -> pd.Series:
    """Levels with fewer than `minimum` visits become Other."""
    series = series.astype("string").fillna("Unknown")
    counts = series.value_counts()
    small = counts[counts < minimum].index
    return series.where(~series.isin(small), "Other")


def fit_model(frame: pd.DataFrame, outcome: str, factors: list[str], adjust: list[str],
              minimum: int) -> tuple[dict[str, list[tuple[str, float, float, float]]], str, int]:
    """Odds ratios (95% CI) per factor level against its largest level, clustered by patient."""
    import statsmodels.api as sm

    data = frame[frame[outcome].notna()].copy()
    columns = [c for c in factors + adjust if available(data[c])]
    design_parts, references = [], {}
    for column in columns:
        values = pooled(data[column], minimum)
        reference = values.value_counts().idxmax()
        references[column] = reference
        dummies = pd.get_dummies(values, prefix=column, prefix_sep="=", dtype=float)
        design_parts.append(dummies.drop(columns=f"{column}={reference}"))
    if not design_parts:
        return {}, "nothing to model", 0
    design = sm.add_constant(pd.concat(design_parts, axis=1), has_constant="add")
    design = design.loc[:, design.std() > 0].assign(const=1.0)
    y = data[outcome].astype(bool).astype(float)
    groups = pd.factorize(data["PatientDurableKey"])[0]
    from statsmodels.tools import sm_exceptions
    with warnings.catch_warnings():
        for name in ("ConvergenceWarning", "PerfectSeparationWarning", "HessianInversionWarning"):
            if hasattr(sm_exceptions, name):
                warnings.simplefilter("error", getattr(sm_exceptions, name))
        warnings.simplefilter("error", RuntimeWarning)
        try:
            result = sm.Logit(y, design).fit(disp=0, maxiter=200, cov_type="cluster", cov_kwds={"groups": groups})
        except Exception as exc:
            return {}, f"did not converge: {type(exc).__name__}", int(len(y))
    if not result.mle_retvals.get("converged", True):
        return {}, "did not converge", int(len(y))
    ci = result.conf_int()
    out: dict[str, list[tuple[str, float, float, float]]] = {}
    for column in factors:
        if column not in references:
            continue
        rows = [(f"{references[column]} (ref)", 1.0, math.nan, math.nan)]
        for term in result.params.index:
            if term.startswith(f"{column}="):
                rows.append((term.split("=", 1)[1], float(np.exp(result.params[term])),
                             float(np.exp(ci.loc[term, 0])), float(np.exp(ci.loc[term, 1]))))
        out[column] = rows
    return out, "", int(len(y))


def admission(settings: Settings, section_name: str | None = None, visits: pd.DataFrame | None = None) -> Page:
    visits = load(settings) if visits is None else visits
    chosen, name, expression = section(visits, settings, section_name)
    page = new_page(settings, "admission and ICU", f"admission {section_name or ''}".strip(), name, expression)
    factors = list(settings["admission_factors"])
    adjust = list(settings["adjust_for"])
    minimum = int(settings["model_min_level"])
    outcomes = [("admitted", "Admitted"), ("icu", "ICU")]
    total = len(chosen)
    page.line(f"{'ED visits':<20}{page.count(total):>12}")
    for column, label in outcomes:
        if available(chosen[column]):
            k = int(yes(chosen[column]).fillna(False).sum())
            page.line(f"{label:<20}{page.count(k):>12} {page.pct(k, total):>6}%")
        else:
            page.line(f"{label:<20}{'not available':>19}")

    for factor in factors:
        page.heading(f"{factor}")
        if not available(chosen[factor]):
            page.line("  not available")
            continue
        levels = chosen[factor].astype("string").fillna("Unknown")
        page.line(f"  {'level':<16}{'visits':>8}{'adm %':>10}{'ICU %':>10}")
        for level in sorted(levels.unique()):
            here = chosen[levels == level]
            n = len(here)
            parts = [f"  {fit(level, 16):<16}{page.count(n):>8}"]
            for column, _ in outcomes:
                if available(chosen[column]):
                    k = int(yes(here[column]).fillna(False).sum())
                    parts.append(f"{page.pct(k, n):>10}")
                else:
                    parts.append(f"{'-':>10}")
            page.line("".join(parts))
        for column, label in outcomes:
            if available(chosen[column]):
                p = table_test(pd.crosstab(levels, yes(chosen[column]).fillna(False).astype(bool)))
                page.line(fit(f"  {label}: chi-square {p_text(p)}", page.width))

    for column, label in outcomes:
        page.heading(f"Model: {label}")
        if not available(chosen[column]):
            page.line("  not available")
            continue
        page.line(fit(f"  adjusted for {', '.join(adjust) or 'nothing'}", page.width))
        page.line(fit(f"  levels under {minimum} pooled as Other", page.width))
        page.line(fit("  SEs clustered by patient", page.width))
        odds, problem, used = fit_model(chosen, column, factors, adjust, minimum)
        page.line(f"  {'visits in model':<20}{page.count(used):>10}")
        if problem:
            page.line(fit(f"  {problem}", page.width))
            continue
        for factor in factors:
            rows = odds.get(factor)
            if rows is None:
                continue
            if len(rows) < 2:
                page.line(fit(f" {factor}: one level once pooled; not modelled", page.width))
                continue
            page.line(f" {factor}")
            page.line(f"  {'level':<18}{'OR':>6}  95% CI")
            for level, ratio, low, high in rows:
                ci = "" if math.isnan(low) else f"{low:.2f}-{high:.2f}"
                page.line(fit(f"  {fit(level, 18):<18}{ratio:>6.2f}  {ci}", page.width))
    return page
