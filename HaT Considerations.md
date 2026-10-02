# HaT PheWAS: Considerations For Later

Ideas and checks that are not in `YAMLs/temp/hat_phewas_intake.yaml` yet.

## Tryptase Labs (not now)

HaT is usually confirmed by an elevated baseline serum tryptase (above about 8 ng/mL) or TPSAB1 copy-number testing. A `hat_Labs` table would let us:

- validate the D89.44 cases (how many have a tryptase on record, and how high);
- later, exclude controls with an elevated tryptase.

Source: `LabComponentResultFact`, joined to `LabComponentDim` for the component name (both are in the data dictionary). The tryptase component keys have to be looked up on the VM first.

## Department Names

`hat_Encounters` carries `DepartmentKey`. Turning it into a department name or specialty (to see who diagnoses HaT, and to adjust for allergy/immunology follow-up) needs `DepartmentDim`, which is not in the data dictionary. Add it to the dictionary, then join it.

## Checks After The Pull

- **Index dates before the code existed.** D89.44 entered ICD-10-CM on 2021-10-01 (FY2022 code set; worth confirming). Count `hat_Patients` rows with `IndexDate < 20211001`. `IndexDate` is each patient's first D89.44, so any patient counted there has a diagnosis record that was mapped to D89.44 after the fact (DiagnosisTerminologyDim maps a diagnosis record to its current code, whatever the event date).
- **Coding before D89.44.** Patients diagnosed before 2021-10 were likely coded D89.49 or another D89.4x. Look for D89.4x in `hat_Diagnoses` before each index date.
- **Diagnosis coverage vs observation time.** Encounters may appear in years where ICD codes do not. Start observation no earlier than the first year with diagnosis rows, or controls and cases get time-at-risk in which no diagnosis could appear.
- **Ruled-out D89.44.** `hat_Patients` takes a D89.44 of any Type or Status. Rebuild the case set and index date from `hat_Diagnoses` after filtering on `DiagnosisStatus`.
- **Cosmos vs SneakPeek.** The pull is `Dual` on purpose: SneakPeek (a 1% set with the same columns) runs first and shows the pull works on the smaller set. The analysis uses the Cosmos tables; the `_sp` tables can be ignored after that.