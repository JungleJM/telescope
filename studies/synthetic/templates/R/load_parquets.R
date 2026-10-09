# Load every table in data/cosmos_parquets/ as a tibble.
#
#   source("R/load_parquets.R")
#   tables <- load_parquets()
#   head(tables$EDVisits)

library(arrow)

load_parquets <- function(folder = file.path("data", "cosmos_parquets")) {
  files <- list.files(folder, pattern = "\\.parquet$", full.names = TRUE)
  if (length(files) == 0) stop("No parquets in ", folder)
  tables <- lapply(files, read_parquet)
  names(tables) <- tools::file_path_sans_ext(basename(files))
  tables
}
