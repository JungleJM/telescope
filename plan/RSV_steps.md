# Infant RSV: what to do on the VM, in order

Run every command in the VM folder that holds `scope.py`. The pages it writes are in `runs\Infant_RSV\analysis\pages\`; open each in the transcription viewer and send it back.

## 1. The analysis, and two checks (any time)

**Copy over:** `dist/rsv_bundle.py` (version `13d091be`), to beside `scope.py`.

```
python rsv_bundle.py        (answer y)
python rsv test             (should end OK)
python rsv icu              -> pages\icu-check.txt
python rsv verify           -> pages\assumption-verify.txt and assumption-verify-detail.txt
```

**Send back:** `icu-check.txt` and `assumption-verify.txt`, plus the detail page if you have time. `icu` and `verify` only read Cosmos; they are safe to run while a pull runs.

## 2. The redo pull, with the ICU stays in it

1. Back up `runs\Infant_RSV\`.
2. **Copy over:** `dist/rsv_redo/bundle.py` (content id `ef092a18`). It carries Scope, the updated data dictionary and the new `Infant_RSV` blueprint, which now includes `IcuStays`. Install it only when no pull is executing; it refuses otherwise.

```
python bundle.py            (answer y)
python scope.py             -> Run: Blueprint Infant_RSV, Export split, Execute
```

## 3. When the redo is finished and packaged

Status shows Infant_RSV packaged, and `runs\Infant_RSV\cosmos_parquets\` has `Patients.parquet`, `EDVitals.parquet`, `IcuStays.parquet` and the rest.

```
python rsv build            -> pages\build.txt
python rsv all              -> every page, and pages\first_run.txt holding them all
```

**Send back:** `build.txt`, then `first_run.txt`.

## 4. Then, for the next pulls (GERD)

**Copy over:** `dist/bundle.py` (content id `ba65b4ef`), once nothing is executing. Author's Project DB is then **Auto**, or a database chosen from a list showing each one's free space; a chosen one is used as chosen.

## Where things are on the Mac

| File | What it is |
|---|---|
| `dist/rsv_bundle.py` | the analysis (`rsv`) |
| `dist/rsv_redo/bundle.py` | Scope, with the dictionary and the redo blueprint |
| `dist/bundle.py` | Scope with Project DB Auto (D218), for after the redo |
| `YAMLs/temp/Infant_RSV_intake.yaml` | the redo's intake, written by `studies/infant_rsv/make_intakes.py` |
| `plan/tasklist.md` | open questions, under *Infant RSV* and *ICU* |

**If something fails:** send the screen. `python rsv` alone lists every command.
