# Build the PheWAS transfer library on the Mac. See sideproject-phewas.qmd.
#
# Expects in build_dir: check_phewas_lib.R, vm_packages.txt, the PheWAS clone
# and the PheWAS_<version>.tar.gz built from it. Leaves Rlib/ ready to pack.

build_dir   <- path.expand("~/phewas_transfer_build")
library_dir <- file.path(build_dir, "Rlib")
zips_dir    <- file.path(build_dir, "zips")
contrib     <- "https://cloud.r-project.org/bin/windows/contrib/4.6"
extra       <- character()  # packages the VM has but check_phewas_lib() says are too old

source(file.path(build_dir, "check_phewas_lib.R"))
unlink(c(library_dir, zips_dir), recursive = TRUE)
dir.create(library_dir, recursive = TRUE)
dir.create(zips_dir)

# PheWAS ships as source. Its own dependencies seed the search.
tarball <- list.files(build_dir, pattern = "^PheWAS_.*\\.tar\\.gz$", full.names = TRUE)
stopifnot(length(tarball) == 1)
file.copy(tarball, library_dir)
desc <- read.dcf(file.path(build_dir, "PheWAS", "DESCRIPTION"))[1, ]
direct <- trimws(sub("\\(.*", "", strsplit(paste(desc["Depends"], desc["Imports"], sep = ","), ",")[[1]]))
direct <- setdiff(direct[nzchar(direct)], "R")

# Everything those need, minus base R and what the VM already has.
db <- available.packages(contriburl = contrib)
needed <- tools::package_dependencies(direct, db = db, which = c("Depends", "Imports"),
                                      recursive = TRUE)
needed <- unique(c(direct, unlist(needed)))
needed <- setdiff(needed, rownames(installed.packages(priority = "base")))
vm <- read.table(file.path(build_dir, "vm_packages.txt"),
                 col.names = c("Package", "Version"), colClasses = "character")
to_bundle <- sort(c(setdiff(needed, vm$Package), extra))
unknown <- setdiff(to_bundle, rownames(db))
if (length(unknown)) stop("Not on CRAN for Windows R 4.6: ", paste(unknown, collapse = ", "))
print(to_bundle)

# Windows binaries are already-installed package folders, zipped. Unzipping
# them is what install.packages() does with them on Windows.
got <- download.packages(to_bundle, destdir = zips_dir, available = db,
                         contriburl = contrib, type = "win.binary")
for (z in got[, 2]) unzip(z, exdir = library_dir)
file.copy(file.path(build_dir, "check_phewas_lib.R"), library_dir)

check_phewas_lib(library_dir, vm_packages = file.path(build_dir, "vm_packages.txt"),
                 r_version = "4.6.1")
