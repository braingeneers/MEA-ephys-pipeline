"""The owning scientific defaults, independent of launch/runtime/global params."""
import json
from pathlib import Path


def scientific_defaults():
    # Return a fresh value for each invocation; callers never edit shared state.
    return json.loads((Path(__file__).resolve().parents[1] / 'defaults.json').read_text())


def scientific_default(name):
    return scientific_defaults()[name]
