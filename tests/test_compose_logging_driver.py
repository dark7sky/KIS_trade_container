"""Regression check for hosts whose default Docker log driver is journald."""

from pathlib import Path


compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(encoding="utf-8")

assert compose.count("driver: local") == 3, (
    "Each service must explicitly use Docker's local log driver so max-file is supported."
)
