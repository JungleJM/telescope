# Brief For The VM's AI: A History Of Gen1

Paste everything below the line into Kilo Code on the VM. It asks for a written
history of the Gen1 generator repo, laid out to survive printing, screenshots
and OCR on the way back. Bring the result back, and it will be folded into
`thoroughhistory.qmd` (Section 4).

Written for: Kilo Code (GPT-5) on the VM. It cannot read images, so everything
it needs is in the text.

-------------------------------------------------------------------------------

# Task: a thorough, evidence-based history of my Gen1 generator

I built a data-pull generator on this VM, which I call Gen1: generator.py,
makeCosmos.py, makeProjects.py, makeR, and their YAML templates, recipes, SQL
scripts and notes. I have since rebuilt it off the VM as a new system, and that
system's history is already written. I now need the same record for Gen1,
which exists only here.

This is for my portfolio and job interviews, not for production. Do not change
any code or data. The only file you write is the history document described
below.

## What the history should show

I was the only person on this project. I found the problem, designed the
solution, learned SQL, Python and R in order to build it, tested it and ran it.
I want the record to show that work under the roles of a software team:
product management, UX, architecture, data and database engineering, clinical
and research methods, QA and testing, release and delivery, security and
compliance, documentation, and working with AI assistants.

## Ground rules

1. Evidence first. Use your tools to read files and run read-only commands.
   Every claim cites its source: a file path, a commit id, or a command and
   what it printed.
2. Say what you did not do. If you did not run a command, write NOT RUN beside
   it. Do not describe what a command "would show". A previous answer
   explained queries without running them; this time I need what you found,
   not what you would expect to find.
3. No guessed dates or reasons. If the evidence does not give a date or a
   reason, write "unknown" and add a question to the Gaps section. Mark
   anything you infer with "(inferred)".
4. Nothing sensitive leaves the VM. Follow the internal best-practice and
   disallowed-practice documents you have access to. If a section would break
   them, leave it out and say so. In particular, do not include:
   - any patient-level data or query results, including the sample rows and
     row counts inside run reports such as generator_output.md;
   - connection strings, credentials, server names or instance names;
   - project database names;
   - anything those documents forbid exporting.
   File names, dates, line counts and descriptions of code are fine. If you
   are unsure whether something may leave, leave it out and list it under
   Gaps.
5. Do not query Cosmos or any database. This task needs files only.
6. Read-only. Do not edit, move, rename or delete anything. Do not run git
   commands that change state (no checkout, reset, commit, stash or pull).

## Where to look

First find the Gen1 folder or folders and list what exists. Then, in order:

1. Version history, if the folder is a git repository:
     git log --reverse --date=short --format="%h %ad %s" --stat
     git log --all --diff-filter=D --name-only --date=short --format="%h %ad %s"
   If git is not available, say so and use file dates instead (PowerShell):
     Get-ChildItem -Recurse -File |
       Sort-Object LastWriteTime |
       Select-Object LastWriteTime, Length, FullName
2. Older copies of scripts: names like *_old, *_v2, *.bak, backup folders, and
   copies of scripts inside run output folders (QueryScripts, scripts).
   Comparing them shows how the code grew.
3. Notes, READMEs, markdown and QMD files, YAML templates and recipes, and the
   comments inside the scripts.
4. The document you wrote earlier, "Generator stack - behavior & stability
   notes", if it is still here. It describes where Gen1 ended up; this task is
   about how it got there.

Before writing, list the sources you found and the commands you ran, so I can
see what the history rests on.

## What to write

Write one file, Notes/gen1_history.md, with these sections.

1. SOURCES. Folders, files and commands used, with NOT RUN where it applies.

2. TIMELINE. Dated entries, oldest first: when each script first appeared,
   each major version, each feature added, each thing removed. One line each:
   date, what changed, source. Give the reason when the evidence has one.

3. ARCHITECTURE AT THE END. What each script did and how they fit together.
   Keep it short; the earlier notes cover the detail.

4. DECISIONS. Number them G1, G2, and so on. For each: what was chosen, what it
   replaced or what was rejected, why (if known), and the source. Include
   decisions that were later reversed, and what replaced them. Look in
   particular for:
   - the ##JVM_ global temp naming
   - capturing @@SERVERNAME and passing it to makeProjects
   - running Projects SQL per destination table, gated on Cosmos success
   - deduplication through staging tables, and its fallback
   - the CSV upload rewrite (from a one-column code list to general CSV)
   - @Pull and @Push switch variables
   - the 80,000,000-row warning
   - markdown run reports (printout_md)
   - copying scripts into run output folders
   - makeR and parquet export
   - makeCosmos.py being absorbed into generator.py

5. PROBLEMS AND FIXES. Errors hit, VM limits worked around, and what was done
   about each. Name the limit when one was involved: no git, no internet, no
   file transfer, blocked programs, the fixed package list, database
   permissions.

6. LEARNING. Evidence of skills growing: early scripts compared with later
   ones, and the first use of a technique (functions, error handling, pyodbc,
   T-SQL features, R). Describe it; quote at most a few short lines.

7. BY ROLE. Under each heading, 3 to 10 bullets, each citing a source:
   - Product management
   - UX and usability
   - Architecture
   - Data and database engineering
   - Clinical and research methods
   - QA and testing
   - Release and delivery
   - Security and compliance
   - Documentation
   - Working with AI assistants

8. NUMBERS. Number of scripts; lines of code per script, and how that changed
   over time; number of templates and recipes; number of versions; the date
   span. Counts about the code only, never about data from pulls.

9. GAPS. Everything you could not find or were unsure of, written as questions
   for me.

## Format: this file leaves the VM as screenshots

I will print or screenshot the file and transcribe it on another machine with
OCR, so:

- Plain ASCII only. No arrows (write ->), no emoji, no curly quotes, no
  special bullet characters.
- Lines of 90 characters or fewer. No wide tables; use lists.
- Put a marker line "=== PAGE n ===" at the start and after every 50 lines,
  numbered from 1, and end the file with "=== END: N pages ===". I use these
  to check that no screenshot is missing.
- Aim for 10 to 15 printed pages. Be specific rather than long.

When you are done, reply with the file path, the page count and the GAPS list.
