# Infant RSV: what to do on the VM, in order

Run every command in the VM folder that holds `scope.py`. The pages it writes are in `runs\Infant_RSV\analysis\pages\`; open each in the transcription viewer and send it back.

## 1. Now, while the redo pull runs

**Copy over:** `dist/rsv_bundle.py` (version `cebfee48`), to beside `scope.py`.

```
python rsv_bundle.py        (answer y)
python rsv test             (should end OK)
python rsv icu              -> pages\icu-check.txt
python rsv verify           -> pages\assumption-verify.txt and assumption-verify-detail.txt
```

**Send back:** `icu-check.txt` and `assumption-verify.txt`, plus the detail page if you have time. `icu` and `verify` only read Cosmos; they are safe to run while the pull runs.

## 2. When the redo is finished and packaged

Status shows Infant_RSV packaged, and `runs\Infant_RSV\cosmos_parquets\` has `Patients.parquet`, `EDVitals.parquet` and the rest.

```
python rsv build            -> pages\build.txt
python rsv all              -> every page, and pages\first_run.txt holding them all
python rsv keys             -> rsv_visit_keys.parquet, beside scope.py
```

**Send back:** `build.txt`, then `first_run.txt`.

## 3. The ICU pull, after step 2

**Copy over:** `dist/rsv_icu/bundle.py` (content id `fde1c0ef`). It carries the updated data dictionary and the `Infant_RSV_ICU` blueprint. Install it only when no pull is executing; it refuses otherwise.

```
python bundle.py            (answer y)
python scope.py             -> Run: Blueprint Infant_RSV_ICU, Export split, Execute
```

It needs `rsv_visit_keys.parquet` from step 2 beside `scope.py`.

## 4. When the ICU pull is packaged

```
python rsv build            -> pages\build.txt (now with ICU from the registry)
python rsv all
```

**Send back:** `build.txt` and `first_run.txt`.

## Where things are on the Mac

| File | What it is |
|---|---|
| `dist/rsv_bundle.py` | the analysis (`rsv`) |
| `dist/rsv_icu/bundle.py` | Scope, with the dictionary and the ICU blueprint |
| `YAMLs/temp/Infant_RSV_ICU_intake.yaml` | the ICU pull's intake |
| `plan/tasklist.md` | open questions, under *Infant RSV* and *ICU* |

**If something fails:** send the screen. `python rsv` alone lists every command.
