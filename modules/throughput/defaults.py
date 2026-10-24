"""Throughput module settings defaults (modules/throughput/defaults.json, 16-settings)."""

from pathlib import Path

from core.settings import load_defaults

DEFAULTS = load_defaults(Path(__file__).with_name("defaults.json"))
