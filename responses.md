# CSV

so CSV seems to have a lot of issues. I can see why - there's no variable typing, and that screws with a bunch of things.

- A CSV file is actually quite large when compared to a parquet. As such, when given the option, I will probably not convert a s parquet to a CSV, And we'll likely just work with parquets functionally from here on. That said, I will probably get a situation where somebody sends me a CSV and I need to be able to use it.
- For amount of trouble it would take to standardize a CSV upload, I don't think it's worth it. Instead, let's pivot the 'csv upload' option to pull a 'csv to parquet' script that will take column names and types as optional variables. I think I could then do something like 



```yaml
- name: HospitalICDCodes
  dest_table: UMCHospitalPatients
  file_type: csv
  file_loc: "csv/HospitalICDCodes.csv"   # relative to this template; travels with the transfer YAML
  scope: global #global or per_group
  push_this_cycle: true
  columns: 
  - name: PatientDurableKey 
    type: BIGINT
    	

```

which would allow for programmatic conversion to the variable type we need. Anything not mentioned in teh columns would stay as the string/varchar/whatever. 

I think we should focus instead on making sure parquets can reliably be uploaded to a projects table and then up to a cosmos. I suspect we need the project part because if there is a lot of rows and we need to do batching, I think it's gonna require it having a project section so that we can protect ourselves from crashes and have deterministic row order and that sort of thing. I think that's standard now, but you didn't mention it in your response here.

Thoughts and questions:

- 'the copy is the source' - that's a great idea. Note that in instructions or design somewhere so that we'll be able to find it, when I'm wondering if I need to repull because someone made some CSV changes. 
1. a) sounds great
2. that's fine
3. agreed, upload_ as a prior marker, then dest_table. Something like upload_HospitalICDCodes says it clearly and where it's from. If we are uploading it, we dont' need to have it downloaded again into the parquet during the Artifacts phase (not done yet) - instead we can just move or copy over the parquet file we essentially made already. 
# Transactions

I can see how that's an issue. I'm glad you fixed it. SHould it do commit at the end of each run? In this case, if there's 3 batches, is the 'end of run' commit between each batch, or at the end of batch 3?

I do feel like it should in general do teh full commit while it has the data, before jumping to do another one. But maybe that's less efficient, i dont' know. Does this system have multithreading? If it's not automatic, I dont' want to program such a thing in. I'm ok with just being sure it's committed before moving to the next one, because the whole point is committing to local, so I want that bird to be in hand before I go searching for another in the bush (i.e be sure I've got the files saved before I go pulling another down).

Thoughts:

- I agree with the addition of the bird-in-hand rule. 

# Mac YAML testing

making notes as I go along.

1. In 'uploads', i should be able to hit a checkmark to say it's a pk table. If there's already one, it should respond with a fail saying 'pktable already exists' and names  the pk table.


Builder Tab:

1. In teh builder tab, there is a 'batching' section, but not a 'multiplier' section? That seems odd. 

3. I want each section, where the title of the section is, to have a subtitle with a brief explanation of the section. Like, If possible, put the "# Comments" that are on the yaml for that section - that's where I did my explanations. Ideally make it to the right or use the space after the title, so it doesn't push everythign else downwards. But no worries if that makes things difficult (picture in response, YAML3) 
  
4. To that point, add a quick explanation of join types (very concise) under joins in 'cohorts', specifically about what the logic does with empties, etc. based on teh join type.
5. In Cohorts, I like the 'custom' setup for adding a new table, but the 'add custom cohort' and 'reset custom form' should be in the same place as the 'remove' buttons for the recipes. 'reset' should dit outside the specific custom table being built, and 'add' shoudl be in the same row. Also, 'add custom cohort' should be renamed to 'add custom table - this isn't a whole cohort you're adding. 
6. what does 'download custom recipe' do in this case? I imagine that 'copy custom as recipe' saves the custom as a new table in 'recipes.yaml' correct? If that's teh case that is all I need 'download' to do, so it shoudl just be one option, and it should be a button between 'load' and 'remove' in teh added custom table's form (see image for builder 6). Also, when I saved a custom table, it loads 'name' to the right of somethign called 'fact', which I suspect is asking the table type (pk or fact). remove that input, and ahve the 'is pk table' button just like I want to have in the 'uploads' section. In fact, have whatever the current PKTable is be illustrated in some way. '

Cohorts tab: 

5.  Now in the Cohorts tab, I'd like to have a small section that mentions current batching and multipliers. This shouldn't be editable there, so just a comma-separated or "/"-separated description, somethign like:  
"[multipliers] race: black/white, IBDType: crohns/UC  
[batching] sex: male/female, state: LA/MS/GA/NC/Other, chunk:2000
. 