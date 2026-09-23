# Check a PheWAS transfer library.
#
# Mac, before packing. Nothing is loaded: the answer comes from the DESCRIPTION
# files, the source tarballs and the VM's exported package list.
#   check_phewas_lib("QMDs/sideproject-phewas/build/Rlib",
#                    vm_packages = "QMDs/sideproject-phewas/build/vm_packages.txt",
#                    r_version = "4.6.1")
#
# VM, after unpacking and installing the source tarballs. Puts the library
# first on .libPaths(), loads PheWAS and, on success, marks the folder so
# phewas_transfer.py will promote it.
#   check_phewas_lib("C:/phewas_offline/Rlib.new")

check_phewas_lib <- function(lib, vm_packages = NULL, roots = "PheWAS",
                             r_version = getRversion()) {
  lib <- normalizePath(lib, winslash = "/", mustWork = TRUE)
  on_vm <- is.null(vm_packages)
  r_version <- as.character(r_version)
  minor <- function(v) sub("^(\\d+\\.\\d+).*", "\\1", v)

  installed <- installed.packages(lib.loc = lib, noCache = TRUE)
  descs <- lapply(rownames(installed), function(p) installed[p, ])
  tarballs <- list.files(lib, pattern = "\\.tar\\.gz$", full.names = TRUE)
  problems <- character()

  for (tb in tarballs) {
    pkg <- sub("_.*", "", basename(tb))
    if (on_vm) {
      if (!pkg %in% rownames(installed))
        problems <- c(problems, sprintf("%s is shipped as source and not installed yet", pkg))
      next
    }
    tmp <- tempfile()
    untar(tb, files = paste0(pkg, "/DESCRIPTION"), exdir = tmp)
    descs <- c(descs, list(read.dcf(file.path(tmp, pkg, "DESCRIPTION"))[1, ]))
  }
  if (!length(descs)) stop("No packages found in ", lib)
  shipped <- vapply(descs, function(d) d[["Package"]], "")

  if (on_vm) {
    .libPaths(c(lib, .libPaths()))
    ip <- installed.packages(noCache = TRUE)
    have <- setNames(ip[, "Version"], ip[, "Package"])
  } else {
    vm <- read.table(vm_packages, col.names = c("Package", "Version"),
                     colClasses = "character")
    base <- rownames(installed.packages(priority = "base"))
    have <- c(setNames(vapply(descs, function(d) d[["Version"]], ""), shipped),
              setNames(vm$Version, vm$Package),
              setNames(rep(r_version, length(base)), base))
  }
  # Earlier entries win, matching the order R searches .libPaths().
  have <- have[!duplicated(names(have))]
  have[["R"]] <- r_version

  field <- function(d, f) if (f %in% names(d) && !is.na(d[[f]])) d[[f]] else ""
  parse_deps <- function(text) {
    items <- trimws(strsplit(text, ",")[[1]])
    items <- items[nzchar(items)]
    m <- regmatches(items, regexec(
      "^([A-Za-z0-9.]+)\\s*(?:\\(\\s*([<>=]+)\\s*([^)]+?)\\s*\\))?$", items, perl = TRUE))
    lapply(m, function(x) list(pkg = x[2], op = x[3], ver = x[4]))
  }

  for (d in descs) {
    pkg <- d[["Package"]]
    # Built reads like "R 4.6.1; x86_64-w64-mingw32; <date>; windows".
    built <- field(d, "Built")
    if (nzchar(built)) {
      built_r <- sub("^\\D*(\\d+\\.\\d+\\.\\d+).*", "\\1", built)
      if (minor(built_r) != minor(r_version))
        problems <- c(problems, sprintf("%s was built under R %s, not R %s",
                                        pkg, built_r, minor(r_version)))
      if (!on_vm && !grepl(";\\s*windows\\s*$", built))
        problems <- c(problems, sprintf("%s is not a Windows build (%s)", pkg, built))
    }
    for (dep in parse_deps(paste(field(d, "Depends"), field(d, "Imports"), sep = ","))) {
      if (is.na(dep$pkg)) {
        problems <- c(problems, sprintf("%s has a dependency this check cannot read", pkg))
      } else if (!dep$pkg %in% names(have)) {
        problems <- c(problems, sprintf("%s needs %s, which is missing", pkg, dep$pkg))
      } else if (nzchar(dep$op) &&
                 !match.fun(dep$op)(package_version(have[[dep$pkg]]), package_version(dep$ver))) {
        problems <- c(problems, sprintf("%s needs %s %s %s, but %s is available",
                                        pkg, dep$pkg, dep$op, dep$ver, have[[dep$pkg]]))
      }
    }
  }

  if (on_vm && !length(problems)) {
    for (r in roots) {
      err <- tryCatch({
        suppressPackageStartupMessages(library(r, character.only = TRUE))
        NULL
      }, error = conditionMessage)
      if (!is.null(err)) problems <- c(problems, sprintf("library(%s) failed: %s", r, err))
    }
  }

  if (length(problems)) {
    stop(length(problems), " problem(s) in ", lib, ":\n",
         paste0("  - ", unique(problems), collapse = "\n"), call. = FALSE)
  }
  if (on_vm) {
    writeLines(c(format(Sys.time()), R.version.string), file.path(lib, ".check-passed"))
  }
  message("OK: ", length(descs), " package(s) in ", lib,
          if (on_vm) "; loaded and marked as checked" else "; dependencies satisfied")
  invisible(TRUE)
}
