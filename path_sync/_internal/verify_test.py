from __future__ import annotations

import logging
import sys

import pytest

from path_sync._internal.verify import _command_prefix, run_command


@pytest.mark.parametrize(
    ("cmd", "prefix"),
    [
        ("uv run pytest", "uv"),
        ("just fmt", "just"),
        ("  just fmt", "just"),
        (r'"C:\Program Files\Git\usr\bin\dos2unix.exe" pyproject.toml', r"C:\Program Files\Git\usr\bin\dos2unix.exe"),
        (r'"C:\tools\fmt.exe"', r"C:\tools\fmt.exe"),
        ('"/opt/my tools/fmt" --check "a b"', "/opt/my tools/fmt"),
        ("'/opt/my tools/fmt' --check", "/opt/my tools/fmt"),
        ('"unclosed path/fmt --check', '"unclosed'),
        ("", ""),
        ("   ", ""),
    ],
)
def test_command_prefix(cmd, prefix):
    assert _command_prefix(cmd) == prefix


def test_run_command_prefix_unquotes_program(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    run_command(f'"{sys.executable}" -c "print(\'x\')"', tmp_path)
    assert f"[{sys.executable}] x" in caplog.text
