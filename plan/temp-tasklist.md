## utility: make parquets of certain codes 

Idea for a utility - I want to be able to run a simple query that searches the cosmos db for codes with a certain keyword or phrase, and then adds it into a relevant parquet. My goal is to have a growing set of ICD-10.parquet, SNOMED.parquet, CPT.paquet, LOINC.parquet, that find the codes for specific things. I should be able to run the query by a dropdown of those 4 code types, and then an input field where comma separated queries. For instance, "tryptase, potassium, magnesium" would be three separate words, and you'd do search for each of them.

WHen it finishes, it should save them as a parquet. It should grab all fields for the specific codes (i.e. labcompnentdim has all the columns below). I'm not sure how doable this is for ICD ro SNOMED or CPT, may need to do some investigation.

This should then have a window of parquetviewer where I can see the specific code set's parquet. It should also ask for 'category to file under' and have a sidebar of categories that the user has made. For instance, for this I would do 'HaT', as i probably looked for it for HaT. checking a category/categories shows just those results, unchecking all shows all.

since incorrect data is a thing, i should be able to click on a row and select it, and then have a button that says 'mark incorrect' and 'change category:' which will let me change any selected to my new category. That way, if someone wants to 'undo' one they mark incorrect they can return it.

Goal is to basically have a set of ICD, CPT, etc. codes that I can just have growing for my specific userbase.

FUTURE GOAL: eventually in the 'supporting tables' i can just add one of these and then be able to select specific keywords, keyphrases, and categories. I'm imagining a dropdown of teh code set, and then 'keywords' input field where someone could write "tryptase, potassium, magnesium" and a 'category' field where someone could put "GI, autoimmune". then during runtime, we could have a specific script that finds those in the parquet, copies them to a new, separate parquet.

During validate, it should run a check of all the keywords and categories and make sure they're in the system. If any are not, a warning should come up, with a shortcut to running the utility so that the user can add them. If the user does not, then we add a sql query to the beginning before we start the pulls. It does a search via a query like below, for those keywords, before continuing with the rest of the query. For anything it finds, it should add to the new parquet, but also to the original parquet collection, with the category 'uncategorized'.

END FUTURE GOAL

here's a sql query written and run in SSMS:

![](images/paste-3.png)

result:

![](images/paste-4.png)