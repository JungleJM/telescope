# Worked examples on the synthetic tables. Run from the repository's top folder:
#
#   source("R/examples.R")

library(dplyr)
source(file.path("R", "load_parquets.R"))

tables <- load_parquets()

# 1. Each visit with its patient, and the child's age at arrival in days.
visits <- tables$EDVisits %>%
  left_join(rename(tables$Patients, PatientDurableKey = DurableKey), by = "PatientDurableKey") %>%
  mutate(
    age_days = as.integer(as.Date(ArrivalInstant) - as.Date(BirthDate)),
    admitted = HospitalAdmissionKey > 0
  )

# 2. Admission rate by race.
visits %>%
  group_by(FirstRace) %>%
  summarise(visits = n(), admitted = sum(admitted), rate = round(mean(admitted), 3)) %>%
  arrange(desc(visits)) %>%
  print()

# 3. The lowest SpO2 in the ED, per visit.
lowest <- tables$EDVitals %>%
  inner_join(select(tables$EDVisits, EncounterKey, EdVisitKey, ArrivalInstant, DepartureInstant), by = "EncounterKey") %>%
  mutate(DepartureInstant = coalesce(DepartureInstant, ArrivalInstant + 6 * 3600)) %>%
  filter(TakenInstant >= ArrivalInstant, TakenInstant <= DepartureInstant) %>%
  group_by(EdVisitKey) %>%
  summarise(spo2_min_ed = suppressWarnings(min(SpO2, na.rm = TRUE)))

visits %>%
  left_join(lowest, by = "EdVisitKey") %>%
  group_by(admitted) %>%
  summarise(n = sum(is.finite(spo2_min_ed)), median_spo2 = median(spo2_min_ed[is.finite(spo2_min_ed)])) %>%
  print()

# 4. Where admissions were admitted to, and ICU stays among them.
print(count(tables$HospitalAdmissionFact, AdmitSpecialty, sort = TRUE))
icu <- tables$HospitalAdmissionFact %>%
  filter(AdmitSpecialty %in% c("Pediatric Intensive Care", "Critical Care Medicine", "Pediatric Critical Care Medicine")) %>%
  distinct(HospitalAdmissionKey)
cat("ICU visits:", sum(visits$HospitalAdmissionKey %in% icu$HospitalAdmissionKey), "\n")

# 5. A logistic regression: admission by race and age, as a start.
model <- glm(admitted ~ FirstRace + I(age_days / 30), data = visits, family = binomial())
print(round(exp(cbind(OR = coef(model), confint.default(model))), 2))
