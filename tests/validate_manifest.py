#!/usr/bin/env python3
"""Validates aw-app.json against schemas/aw-app.schema.json. Run with the
AW venv (jsonschema is installed there): .venv/aw/bin/python tests/validate_manifest.py
"""
import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent

manifest = json.loads((ROOT / "aw-app.json").read_text())
schema = json.loads((ROOT / "schemas" / "aw-app.schema.json").read_text())

jsonschema.validate(instance=manifest, schema=schema)

# The declarative window spec referenced from contributes.windows must exist.
for win in manifest["contributes"].get("windows", []):
    body = win.get("body", {})
    if body.get("type") == "declarative":
        spec_path = ROOT / body["spec"]
        if not spec_path.is_file():
            print(f"FAIL: window spec missing: {spec_path}", file=sys.stderr)
            sys.exit(1)

print("OK: aw-app.json is valid and all window specs exist")
