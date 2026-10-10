# Infant RSV: what to do on the VM, in order

Run every command in the VM folder that holds `scope.py`. The pages it writes are in `runs\Infant_RSV\analysis\pages\`; open each in the transcription viewer and send it back.

## 1. The analysis, and the checks (any time)

**Copy over:** `dist/rsv_bundle.py` (version `3924c7db`), to beside `scope.py`.

```
python rsv_bundle.py        (answer y)
python rsv test             (should end OK)
python rsv verify           -> pages\assumption-verify.txt and assumption-verify-detail.txt
```

**Send back:** both pages. `verify` now has 40 checks; the six new ones show what the synthetic copy still leaves empty. It only reads Cosmos.

## 2. The redo pull

1. Stop the running redo, and back up `runs\Infant_RSV\`.
2. **Copy over:** `dist/rsv_redo/bundle.py` (content id `2e5968dd`). It carries Scope (with Project DB Auto, D218, and the transcription viewer in `utils\`), the updated data dictionary, and the `Infant_RSV` blueprint: `IcuStays`, and the patients' birth-date accuracy.

```
python bundle.py            (answer y)
python scope.py             -> Run: Blueprint Infant_RSV, Export split, Execute
```

## 3. When the redo is finished and packaged

```
python rsv build            -> pages\build.txt
python rsv all              -> every page, and pages\first_run.txt holding them all
```

**Send back:** `build.txt`, then `first_run.txt`.

## Where things are on the Mac

| File | What it is |
|---|---|
| `dist/rsv_bundle.py` | the analysis (`rsv`) |
| `dist/rsv_redo/bundle.py` | Scope, with the dictionary and the redo blueprint; also the one for GERD |
| `YAMLs/temp/Infant_RSV_intake.yaml` | the redo's intake, written by `studies/infant_rsv/make_intakes.py` |
| `plan/tasklist.md` | open questions, under *Infant RSV* and *ICU* |

**If something fails:** send the screen. `python rsv` alone lists every command.
