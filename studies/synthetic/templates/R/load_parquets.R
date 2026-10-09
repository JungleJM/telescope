# Load every table in data/cosmos_parquets/ as a tibble.
#
#   source("R/load_parquets.R")
#   tables <- load_parquets()
#   head(tables$EDVisits)
#
# Keys are 64-bit integers (BIGINT). arrow reads them as bit64's integer64;
# with bit64 loaded, ==, %in% and joins on them work as they should.

suppressPackageStartupMessages({
  library(arrow)
  library(bit64)
})

load_parquets <- function(folder = file.path("data", "cosmos_parquets")) {
  files <- list.files(folder, pattern = "\\.parquet$", full.names = TRUE)
  if (length(files) == 0) stop("No parquets in ", folder)
  tables <- lapply(files, read_parquet)
  names(tables) <- tools::file_path_sans_ext(basename(files))
  tables
}
