# Commemorating

For one instance of Claude to do, separate from the other headings.

- I made a folder in QMDs related to getting the history of this project. Look at the last day that that was edited/committed in teh git. Many changes were made since then, and I want them to be documented in 'thoroughhistory.qmd'. If you can isolate the changes by date great, if not then just compare decisions, design, roadmap MDs against that document and add what's missing.

- Add to CLAUDE.MD a rule that when I say 'update docs' that you update those 3 MDs and thoroughhistory.qmd, so that I don't have to keep explaining it.

# UI Redesign ideas

## tkinter-based UI for yamlmanager

- I really like the interface of the tkinter plugin. It's able to use the file explorer to find files, it can take text, it can do buttons. I'd like to explore if I could do the entirety of the yamlmanager in this, as well. If not, that's fine. We should have the UI/UX decoupled from the engine anyways, so I want to see if we can just make a fully usable tkinter system. We can't add any imports unless it's literall something we can copy into a text file and not run an installation on, so hopefully all this can be reproduced with just tkinter's native options. I want you to start with just taking the current logic of the web UI and translating it to tkinter.

## Separate idea -

- I'm going to imagine the variables in a UX-friendly readable interface. It can map to the variables we already have, so we dont' have to worry about redoing core logic. Don't make it yet - I want you to ask me any questions you ahve about function, and tell me how doable all this is, in both the web UI (which will only exist on mac) and also tkinter.

  - Project name at top -

    - Dropdown if possible, if not then just text and 'browse'. a 'new' button next to 'browse', which makes 'new_project_intake.yaml'. (new_project is the project name, intake is the phase of the yaml. I'm either intaking from a client by phone or via a chatbot that i design to ask and elicit the quetsions. It's better than 'temp' currently.)

    - "pull from:" text with toggles for "Cosmos" and "Cosmos_SneakPeek", both turned on by default. If both are on, system reads it as 'dual'.

    - Project DB: Prefill PROJECTD33A929, but can be edited.

    - Study Variables section:

      - Min Start Date, Max Start Date. SHould default to 19900101, 20260601. Note that it's in YYYYMMDD format.

      - "Collect all patients matching criteria:" Toggle as well, true on default. If checked, 'smallset = false', and greyed out options for sample size (field) and 'random sample' (toggle). If unchecked, fillable fields.

      - Builder section: Basically what we have for builder, but split a little differently:

        - Primary Key (PK) Table

          - should give options for recipes, a new table from the dictionary, or a parquet, csv or current Projects.dbo.table (called 'dbtable'). If parquet, or CSV, require a 'browse' or input directory. If dbtable, can just be a text input. parquet, csv and dbtable will likely be empty - I'll be making these on the Mac side and can't access the VM's files. If that's the case, it has to provide a warning that doesn't prevent compiling of the \_transfer yaml.

        - Supporting Tables (formerly upload)

          - CSVs, parquets, where you name them. It should be able to see the categories, and you should be able to edit the end-column names.

          - remove the 'pk table' option here, but otherwise have it be the same

        - Multipliers and Splitters

          - "Multipliers" section: With explanation saying that each level will be its own cohort, which is during_build as of now.

          - "Splitters" section (formerly batching, and also formerly split_after_build): with explanation that it will split the PK table by a specific column in the PK, or chunk by number of rows.

            - Will have chunk: option with input field

            - will pull the pktable's columns, in dropdown menu. Button to add, without input field. Once added, will then show input field which does the x-able system we have now.

            - If the PKTable hasn't been input yet,

        - Fact Tables (formerly Cohorts)

          Same idea as before, but integrating other tabs into this

          - Prefabricated (formerly recipes): Same as recipes.

            - Required values: If imported from PKTable, should say "from PKTable column (column). if from uploaded table, should say "from supported table (table)

            - I don't know how the logic currently works to check this. Tell me how it's currently working so I can see if we can improve it somehow

        - Validate Pipeline (formerly 'Pipeline' tab)

          - Same functions, but with a bit more info. If uploads or pktable are unavaialble but has the info of where to check on VM (maybe there's just a checkmark toggle like 'pending yaml transfer to VM' next to the individual submissions on those sections), then the warning should be blue and say 'pending transfter. Otherwise, fails are red, passes are green, warnings are yellow.

          - Is there an API to check for CPT codes, API codes? I'm thining