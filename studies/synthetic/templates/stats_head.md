# Check yourself: the answers in this data

**SYNTHETIC DATA.** These answers describe the made-up data in this repository, not real patients. They are here so you can check your code: if your query gives the same number, you have read the tables the way they are meant to be read.

Each question has its answer and the code that finds it, in Python and in R. To get every answer at once:

```bash
python python/stats.py
Rscript R/stats.R
```

Both print the same list. The questions run in order, and later ones reuse what earlier ones built (`visits`, `lowest` and so on), so run them from the top. Try each one yourself before opening the code.

**The conventions they teach:**
- A child is a `PatientDurableKey`; in `Patients` it is called `DurableKey`.
- An admitted visit has a `HospitalAdmissionKey` above 0; one not admitted has -1.
- The ED tables join to the visit by `EncounterKey`.
- Instants are timestamps without a time zone: treat them as UTC in R.
- Temperatures mix °F and °C, as in Cosmos.
- Keys are 64-bit integers: in R, load `bit64` so they compare correctly.
