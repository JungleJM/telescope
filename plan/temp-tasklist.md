## Urgent solve commands

## Show-stopper tests: what to paste

Paste these into **PowerShell in the working folder on the VM**, the folder with `scope.py`.

**Setup:** make a small test pull from a copy of Infant_RSV.

```         
Copy-Item YAMLs\temp\Infant_RSV_blueprint.yaml YAMLs\temp\ShowTest_blueprint.yaml
```

In Author, open **ShowTest**. Turn Collect all off, set the sample to 10, and Save. Then in Run, choose it from Start run and press Export split. Don't press Execute yet.

**1. The test suite:**

```         
python scope.py --tdd 2>&1 | Select-String "FAIL|ERROR|^Ran|^OK"
```

You should see `OK`, with no FAIL or ERROR lines.

MY RESPONSE: So, i don't have a "ShowTest".

**2. Check that a reader no longer blocks a save:**

```         
python -c "import os,sys,msvcrt; sys.path.insert(0,'pullmanager_runtime'); from pathlib import Path; from pullmanager.yaml_io import _open_shared_windows; Path('a.txt').write_text('x'); Path('b.txt').write_text('y'); f=open(msvcrt.open_osfhandle(_open_shared_windows(Path('a.txt')), os.O_RDONLY)); os.replace('b.txt','a.txt'); print('replaced while open:', Path('a.txt').read_text())"
Remove-Item a.txt, b.txt -ErrorAction SilentlyContinue
```

You should see `replaced while open: y`.

MY RESPONSE: No, i still see access denied. This is while I'm running three prompts though if that changes anythign.

![](images/paste-27.png)

**3. Force a busy manifest.** Press Execute on ShowTest. While it runs, paste this into a second PowerShell window in the same folder. It holds the manifest open for 90 seconds, the way the old reader did:

```         
python -c "import time; f=open(r'runs\ShowTest\pullmanifest.yaml','rb'); print('holding'); time.sleep(90); f.close(); print('released')"
```

Once the pull finishes, check its log:

```         
Select-String -Path runs\ShowTest\execute-*.log -Pattern "busy|FileBusy|trying again"
```

Pass: a "busy … trying again" line, and the pull still finishes. Fail: it stops with `FileBusy`.

**4. The real-life case.** Leave Run open on a real pull overnight, with Status showing. In the morning:

```         
Select-String -Path runs\*\execute-*.log -Pattern "FileBusy|Traceback|exit code 1"
```

Pass: nothing printed.

**5. Force a late error.** Open `YAMLs\temp\ShowTest_blueprint.yaml` in Notepad. Find any fact table's `columns:` list, for example under EDVitals. Add this as a new line, indented exactly like the `- {source: ...}` lines around it:

```         
      - {source: 1/0, name: Boom, type: INT}
```

Save, then press Export split and Execute again in Run. Afterwards:

```         
Select-String -Path runs\ShowTest\execute-*.log -Pattern "Divide by zero|FAILED"
```

Pass: that run is FAILED, with "Divide by zero" in its message. Before the fix, it read done with no rows. Remove the line afterwards.

**6. The scan, after every pull:**

```         
python scope.py --scan-runs
```

Pass: ShowTest isn't listed after step 3.

**Clean up afterwards.** The step-5 run failed, so its tables stay in Projects. Drop the ones starting with ShowTest's prefix in clear_projects_db. ShowTest is a copy of Infant_RSV, so its prefix is `infrsv2`, given that Infant_RSV already has `infrsv`. Then delete `runs\ShowTest` and `YAMLs\temp\ShowTest_blueprint.yaml`.

#