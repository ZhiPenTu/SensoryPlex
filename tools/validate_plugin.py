"""Structural preflight only; never imports or executes plugin code."""

import argparse
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def validate(path: Path):
    schema = json.loads((ROOT / "docs/contracts/plugin.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(yaml.safe_load(path.read_text())))
    for error in errors:
        # ValidationError.message may contain supplied secrets; print field + rule only.
        print(f"invalid field: {'.'.join(map(str, error.absolute_path))}; rule: {error.validator}")
    return not errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    if not validate(arguments.manifest):
        raise SystemExit(1)
    print(
        "Manifest structure valid; signature, digest, runtime compatibility and policy not verified"
    )
