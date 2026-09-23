# The Old Generator

Everything here is about the generator that preceded Pullmanager. None of it
is needed to understand or run the current system. What was worth keeping has
been absorbed into `../design.md` (the contracts) and `../decisions.md` (why,
and what was rejected), so this folder can be deleted.

| File | What it is |
| --- | --- |
| `old_Generator_analysis.qmd` | OCR transcription of the old code, with analysis |
| `generator_understanding.md` | First-pass digest of that analysis |
| `yamlprocessing.md` | Analysis of `inputSimple.yaml` against its SQL; partly retracted, see D1 |
| `inputSimple.yaml`, `examplecos.sql`, `exampleproj.sql` | A verified matching triple: the old generator's input and output |

## Lessons Carried Forward

Each of these is now a tested contract in the new code:

- `##JVM_<dest>` global temps, `#Local_<dest>` staging, fully qualified
  `<project_db>.dbo.<dest>` destinations.
- The Cosmos instance captured at run time with `@@SERVERNAME`.
- Date placeholder substitution; `IS NOT NULL` filters for non-nullable columns;
  grouped `WHERE` fragments without stray `AND`.
- Legacy `dedup_key` normalized (D28); canonical `dedup_keys`.
- CSV upload header normalization; uploaded PK validation.
- Exactly one canonical PK per session.
- Cosmos and Projects row counts captured separately and compared; large-count
  warnings.
- SQL errors reported with the server's messages, including the inner error of
  a failed `OPENQUERY`.
- Connection strings, `GO` splitting, the `nextset()` drain and transaction
  behaviour, harvested as mechanical facts (D2).

## Deliberately Not Reproduced

- One giant `Cosmos.sql` plus one giant `Projects.sql`.
- Selecting SQL by substring-matching a table name (D31).
- Row counts scraped by inspecting result-set column names (D32).
- Dropping the destination inside every transfer block, which with batching
  keeps only the last batch (D21).
- `stop_at_for_pk_table` applied to every PK-typed cohort (D29).
- A single script supervising every stage; markdown run reports as the status
  record; verbosity as a cross-cutting feature; fallbacks that silently change
  SQL semantics.

## Never Answered

If the old code resurfaces, these are the only questions still worth asking it:

- Was any upload path other than literal `INSERT ... VALUES` ever tried?
- How were `parquet` upload cohorts meant to work, given Cosmos cannot read them?
- Anything about `makeR` or artifact export (roadmap: artifact handoff).
