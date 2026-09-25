# Core idea
For the artifact system, now that we have made our successful table pulls, I want to generate a series of objects. 

- The parquets themselves. which should just be a straight pull of the tables that we actually downloaded. I think that we can look at the manifest or some pre-split yaml, because I don't need the batching (assuming they're all stitched back into the table at PROJECT download). I believe that's most of the time because we do the INSERT function. But there is also the chance that user wanted to separate a few batches (say, a few states) so as long as that funcitonality is in here, we should account for that for when we search for pulls. Another way is to simply just look at whatever list of final tables was made and just do a for() loop through them. The files should all download directly into the project folder under a 'parquets' subfolder. 
- a 'load_parquets' function, one in R and one in Python, that loads all parquets into the IDE. At the moment this is for VS Codium and for R studio, but in the future it might be for others like Positron or something else. To minimize the concern of memory use, it can be done via tbe 'opendata' system/function. I think that's what it's called
- a 'examine_parquets' function, one in R and one in python, that does the same but loads them into memory, for direct examination. We will explain in a 'how to' doc why you'd use one vs the other. 
- A 'contents' markdown that I'll detail below. 


## Contents

This needs to be a client facing document that is friendly to non-coders and presents all of the tables with their respective columns and relevant information for both. This information is already placed in the YAML when we make it, so it'll be a matter of extracting that, maybe into an outline form, or just bullet points. 

In the YAML, the table has a granularity and descriptor field.Each column has a output name, a type, and a descriptor. I'm imagining something like this: 

## whiteCrohnsPatients
Granularity: (granularity descriptsion), specific to (multipliers - 'white patients with ICD code K50.%')
Description: (table description)
Columns are as follows: 
- PatientDurableKey (type): (description)
- IndexDate (type): (description)

Of course this should take into account any separated batches as their own tables.


# Running this

I think this could just be another sub script within Pull Manager. I could run a simple command similar to the way I do the Execute command. Something like --extract (folder name). (I I'm wary of extract and execute being so similar, so maybe we could use another word?)

Qestions and recommendations
1. I agree with your tables recommendations - i want it to accurately reflect what is there. Good with chunking as well. I agree with the writing one file per separate_parquets value. Agree with the uploads copying. 
2. Excellent, do it. These should be at the root of the project folder, the client can decide which language to use and which to delete if desired. 
3. below:
- I agree, we should add granularity: per table and description: per column in recipes, with fields in teh builder. fill whatever we can. 
- For things I make, I will write the descriptions, especially in the recipes section. Agree with borrowing dictionary text. - Don't need to write 'from source column', it's implied or user won't know what that means. "No description" is definitely better than guessing. 
- agreed with how you'd do the "specific to", using the logic makes things clear how I selected them too. 
- I disagree on type: This should specifically be the programmatic definition, as they'll need it for writing code. Likely, I'll have them give it to the VM's AI as a reference for stringing functions together, so it needs to be able to ensure the rows can join correctly etc. 
- I like the pull summary. 
4. package is still kind of confusing. I like --artifacts as you mentioned below. 

For me to decide: 
- output folder is runs\(project folder)\parquets as you recommend
- subfolders for SneakPeek and Cosmos, but also _sp suffix, so when they load them all together you can see easily which is which, and there's no programmatic confusion. 
- Column descriptions: Both
- regarding _batch, nowhere because it's internal. 
- partial pulls: agreed with only finished tables with rest listed
- re-packaging - replace existing files each time - no use for older ones. 
- the parquets can't leave, the VM has R and Python, R studio and VSCodium. 
- --artifacts is good, consistent with AI artifact language. 

 