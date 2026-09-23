# Build the PheWAS transfer library on the Mac. See sideproject-phewas.qmd.
#
# From the repo root:
#
#   Rscript QMDs/sideproject-phewas/build_phewas_lib.R
#
# Works in ~/phewas_transfer_build and leaves Rlib/ there ready to pack. Uses
# ~/phewas_transfer_build/vm_packages.txt as the VM's package list when it
# exists, and falls back to the (possibly stale) DSVM Plugins.yaml otherwise.

build_dir   <- path.expand("~/phewas_transfer_build")
library_dir <- file.path(build_dir, "Rlib")
zips_dir    <- file.path(build_dir, "zips")
contrib     <- "https://cloud.r-project.org/bin/windows/contrib/4.6"
phewas_repo <- "https://github.com/PheWAS/PheWAS.git"
extra       <- character()  # packages the VM has but check_phewas_lib() says are too old

file_arg   <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else "QMDs/sideproject-phewas"
repo_root  <- normalizePath(file.path(script_dir, "..", ".."))
check_file <- file.path(script_dir, "check_phewas_lib.R")
source(check_file)

dir.create(build_dir, showWarnings = FALSE)
unlink(c(library_dir, zips_dir, file.path(build_dir, "PheWAS"),
         Sys.glob(file.path(build_dir, "PheWAS_*.tar.gz"))), recursive = TRUE)
dir.create(library_dir)
dir.create(zips_dir)

# The VM's package list, as "name version" lines.
vm_file <- file.path(build_dir, "vm_packages.txt")
if (!file.exists(vm_file)) {
  yaml <- readLines(file.path(repo_root, "YAMLs", "DSVM Plugins.yaml"))
  start <- grep('^  - name: "R"$', yaml)
  tops <- grep("^  - name:", yaml)
  stop_at <- min(c(tops[tops > start], length(yaml) + 1)) - 1
  pkgs <- regmatches(yaml[start:stop_at],
                     regexec('^ +- name: "(.+) v(.+)"$', yaml[start:stop_at]))
  pkgs <- Filter(length, pkgs)
  vm_file <- file.path(build_dir, "vm_packages_from_yaml.txt")
  writeLines(vapply(pkgs, function(m) paste(m[2], m[3]), ""), vm_file)
  message("No vm_packages.txt; using DSVM Plugins.yaml (", length(pkgs),
          " packages). Its versions may predate the VM's R upgrade.")
}

# PheWAS ships as source: clone it and build a tarball. COPYFILE_DISABLE keeps
# macOS from adding ._ metadata files to the tarball.
owd <- setwd(build_dir)
if (system2("git", c("clone", "--quiet", "--depth", "1", phewas_repo, "PheWAS")) != 0)
  stop("git clone of ", phewas_repo, " failed")
Sys.setenv(COPYFILE_DISABLE = "1")
if (system2(file.path(R.home("bin"), "R"),
            c("CMD", "build", "--no-build-vignettes", "--no-manual", "PheWAS")) != 0)
  stop("R CMD build PheWAS failed")
setwd(owd)
tarball <- Sys.glob(file.path(build_dir, "PheWAS_*.tar.gz"))
stopifnot(length(tarball) == 1)
invisible(file.copy(tarball, library_dir))

# PheWAS's own dependencies seed the search.
desc <- read.dcf(file.path(build_dir, "PheWAS", "DESCRIPTION"))[1, ]
direct <- trimws(sub("\\(.*", "", strsplit(paste(desc["Depends"], desc["Imports"], sep = ","), ",")[[1]]))
direct <- setdiff(direct[nzchar(direct)], "R")

# Everything those need, minus base R and what the VM already has.
db <- available.packages(contriburl = contrib)
needed <- tools::package_dependencies(direct, db = db, which = c("Depends", "Imports"),
                                      recursive = TRUE)
needed <- unique(c(direct, unlist(needed)))
needed <- setdiff(needed, rownames(installed.packages(priority = "base")))
vm <- read.table(vm_file, col.names = c("Package", "Version"), colClasses = "character")
to_bundle <- sort(c(setdiff(needed, vm$Package), extra))
unknown <- setdiff(to_bundle, rownames(db))
if (length(unknown)) stop("Not on CRAN for Windows R 4.6: ", paste(unknown, collapse = ", "))
message("Shipping as Windows binaries: ", paste(to_bundle, collapse = ", "))

# Windows binaries are already-installed package folders, zipped. Unzipping
# them is what install.packages() does with them on Windows.
got <- download.packages(to_bundle, destdir = zips_dir, available = db,
                         contriburl = contrib, type = "win.binary", quiet = TRUE)
for (z in got[, 2]) unzip(z, exdir = library_dir)
invisible(file.copy(check_file, library_dir))

check_phewas_lib(library_dir, vm_packages = vm_file, r_version = "4.6.1")
message("Next, from the repo root:\n  python3 QMDs/sideproject-phewas/phewas_transfer.py pack ",
        library_dir, " ", file.path(build_dir, "parts"))
