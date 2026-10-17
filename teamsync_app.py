"""Backward-compatible launcher for the primary TeamSync AI application."""

from pathlib import Path
from runpy import run_path


run_path(str(Path(__file__).with_name("app.py")))
