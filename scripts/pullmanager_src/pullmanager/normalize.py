"""Compatibility rules for hand-authored cohort YAML.

Each function returns its result alongside any notes worth surfacing, because
the failure these rules guard against is silence: the old generator accepted
only `dedup_keys` and, given `dedup_key`, emitted no deduplication at all and
said nothing, changing row counts invisibly.
"""

from __future__ import annotations

from typing import Any

from .naming import DEFAULT_TEMP_PREFIX, global_temp

TRUTHY = {"true", "yes", "y", "1", "on", "t"}
FALSY = {"false", "no", "n", "0", "off", "f", ""}

# `cosmos_db` accepts several spellings and two of them are directives rather
# than database names: `Dual`/`both` means render both variants, and the
# per-cohort `cosmos_db` says which database each one reads.
COSMOS_DATABASES = {
    "cosmos": "COSMOS",
    "dual": "COSMOS",
    "both": "COSMOS",
    "cosmos_sneakpeek": "COSMOS_SneakPeek",
    "sneakpeek": "COSMOS_SneakPeek",
    "sp": "COSMOS_SneakPeek",
}

DEAD_TEST_OPTIONS = {
    "stop_at_for_non_pk_tables": (
        "no longer used; row limits now apply only to the root PK cohort"
    ),
    "print_md": "reporting is the manifest's job; no markdown run report is written",
    "printout_md": "reporting is the manifest's job; no markdown run report is written",
}


class NormalizationError(ValueError):
    """Raised when a value cannot be interpreted."""


def normalize_bool(value: Any, *, default: bool = False) -> bool:
    """Accept the YAML and human spellings of a boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    raise NormalizationError(f"Cannot interpret {value!r} as a boolean.")


def normalize_dedup_keys(cohort: dict[str, Any]) -> tuple[list[list[str]], list[str]]:
    """Return dedup keys as a list of key sets, plus any notes.

    Canonical form is `dedup_keys`, a list of lists. The legacy singular
    `dedup_key` is accepted and normalized rather than rejected: refusing the
    file only relocates the friction, while accepting it loudly removes the
    silent-no-dedup outcome entirely.
    """
    notes: list[str] = []
    raw = cohort.get("dedup_keys")
    legacy = cohort.get("dedup_key")

    if raw is not None and legacy is not None:
        raise NormalizationError(
            f"Cohort {cohort.get('dest_table') or cohort.get('name')!r} sets both "
            "`dedup_keys` and legacy `dedup_key`. Keep only `dedup_keys`."
        )

    if raw is None and legacy is None:
        return [], notes

    if raw is None:
        raw = legacy
        notes.append(
            f"`dedup_key` is legacy; normalized to `dedup_keys` for "
            f"{cohort.get('dest_table') or cohort.get('name')!r}. Update the template."
        )

    if isinstance(raw, str):
        key_sets = [[raw]]
    elif isinstance(raw, list):
        if not raw:
            raise NormalizationError("`dedup_keys` was supplied but is empty.")
        # A flat list is one key set; a list of lists is several.
        if all(isinstance(item, list) for item in raw):
            key_sets = [[str(col) for col in item] for item in raw]
        elif any(isinstance(item, list) for item in raw):
            raise NormalizationError(
                "`dedup_keys` mixes bare columns and key sets. Use either "
                "[[a, b]] for one key set or [[a], [b]] for two."
            )
        else:
            key_sets = [[str(col) for col in raw]]
    else:
        raise NormalizationError(f"Cannot interpret dedup keys from {raw!r}.")

    for key_set in key_sets:
        if not key_set:
            raise NormalizationError("`dedup_keys` contains an empty key set.")
    return key_sets, notes


def validate_dedup_columns(
    key_sets: list[list[str]], cohort: dict[str, Any]
) -> list[str]:
    """Check dedup keys name columns the cohort actually produces."""
    produced = {
        str(col.get("name"))
        for col in cohort.get("columns") or []
        if isinstance(col, dict) and col.get("name")
    }
    problems = []
    for key_set in key_sets:
        unknown = [col for col in key_set if col not in produced]
        if unknown:
            problems.append(
                f"dedup key(s) {', '.join(unknown)} are not columns of "
                f"{cohort.get('dest_table') or cohort.get('name')!r}"
            )
    return problems


def dead_options(test_options: dict[str, Any] | None) -> list[str]:
    """Notes for retired `test_options` keys that are still present."""
    if not test_options:
        return []
    return [
        f"`test_options.{key}` is ignored: {why}"
        for key, why in DEAD_TEST_OPTIONS.items()
        if key in test_options
    ]


def cosmos_database(value: Any, default: str = "COSMOS") -> str:
    """The database to connect to, from a `cosmos_db` setting.

    `Dual` and `both` are expansion directives, not database names; connecting
    with `Database=Dual` would simply fail. Under those the connection goes to
    COSMOS and the SneakPeek cohorts qualify their own tables instead.
    """
    if value is None or not str(value).strip():
        return default
    key = str(value).strip().lower()
    if key in COSMOS_DATABASES:
        return COSMOS_DATABASES[key]
    raise NormalizationError(
        f"Unsupported cosmos_db {value!r}. Expected one of: "
        + ", ".join(sorted(COSMOS_DATABASES))
    )


def is_dual(value: Any) -> bool:
    return str(value or "").strip().lower() in ("dual", "both")


def is_pk(cohort: dict[str, Any]) -> bool:
    return str(cohort.get("type", "")).strip().lower() == "pk"


def pk_cohorts(cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cohorts if isinstance(c, dict) and is_pk(c)]


def joined_generated_tables(cohort: dict[str, Any], prefix: str = DEFAULT_TEMP_PREFIX) -> set[str]:
    """Global temp names this cohort joins, found in its filter text."""
    filter_block = cohort.get("filter") or {}
    text_parts: list[str] = []
    for key in ("from", "join", "where"):
        value = filter_block.get(key)
        if isinstance(value, str):
            text_parts.append(value)
        elif isinstance(value, list):
            text_parts.extend(str(item) for item in value)
    haystack = " ".join(text_parts).upper()
    return {token for token in _global_temp_tokens(haystack, prefix)}


def _global_temp_tokens(haystack: str, prefix: str = DEFAULT_TEMP_PREFIX) -> set[str]:
    tokens: set[str] = set()
    marker = f"##{prefix}_".upper()
    start = haystack.find(marker)
    while start != -1:
        end = start + len(marker)
        while end < len(haystack) and (haystack[end].isalnum() or haystack[end] == "_"):
            end += 1
        tokens.add(haystack[start:end])
        start = haystack.find(marker, end)
    return tokens


def root_pk_cohorts(
    cohorts: list[dict[str, Any]], prefix: str = DEFAULT_TEMP_PREFIX
) -> list[dict[str, Any]]:
    """PK cohorts that do not depend on another PK cohort's global temp.

    A chained PK (patients -> diagnosis events for those patients) has exactly
    one root. Row limits apply there, because limiting a downstream PK as well
    compounds the restriction into an unrepresentative sample.
    """
    pks = pk_cohorts(cohorts)
    sibling_temps = {
        global_temp(c.get("dest_table"), prefix).upper() for c in pks if c.get("dest_table")
    }
    roots = []
    for cohort in pks:
        own = global_temp(cohort.get("dest_table"), prefix).upper() if cohort.get("dest_table") else None
        depends_on = joined_generated_tables(cohort, prefix) & sibling_temps
        depends_on.discard(own)
        if not depends_on:
            roots.append(cohort)
    return roots


def root_pk_cohort(cohorts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The single root PK cohort, or None when there is no PK at all."""
    roots = root_pk_cohorts(cohorts)
    if not roots:
        return None
    if len(roots) > 1:
        names = ", ".join(str(c.get("dest_table") or c.get("name")) for c in roots)
        raise NormalizationError(
            f"Several PK cohorts depend on nothing ({names}), so the root PK is "
            "ambiguous. Chain them, or mark one as canonical."
        )
    return roots[0]
