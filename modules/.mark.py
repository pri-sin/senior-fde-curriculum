"""Usage: python3 modules/.mark.py <module_dir_name> <status>; on 'completed' also py_compiles lab.py."""
import json, py_compile, sys
name, status = sys.argv[1], sys.argv[2]
if status == "completed":
    py_compile.compile(f"modules/{name}/lab.py", doraise=True)
    for f in ("README.md", "interview_prep.md"):
        assert open(f"modules/{name}/{f}").read().strip(), f
p = json.load(open("progress.json")); p[name] = status
json.dump(p, open("progress.json", "w"), indent=2); print(name, status)
