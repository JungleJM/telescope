Intakes the app's view tests open (`scripts/yamlmanager_tk.py --tdd`), kept apart
from `YAMLs/temp/`, so the pulls there can change without breaking the tests.
`IBD_Ancestry_intake.yaml` lacks a variable on purpose: its tests need an error.
