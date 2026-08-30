"""Package structure and hygiene tests."""

from __future__ import annotations

import importlib
import importlib.resources
import os
import subprocess
import sys
from pathlib import Path


def test_import_nexafreight() -> None:
    """Test that nexafreight can be imported and version is a non-empty string."""
    import nexafreight

    assert hasattr(nexafreight, "__version__")
    assert isinstance(nexafreight.__version__, str)
    assert len(nexafreight.__version__) > 0
    assert nexafreight.__version__ == "0.1.0"


def test_py_typed_exists() -> None:
    """Test that py.typed is packaged and accessible as a file."""
    py_typed = importlib.resources.files("nexafreight").joinpath("py.typed")
    assert py_typed.is_file()


def test_every_directory_has_init() -> None:
    """Test that every directory under src/nexafreight contains an __init__.py."""
    src_dir = Path(__file__).resolve().parent.parent.parent / "src" / "nexafreight"
    assert src_dir.exists() and src_dir.is_dir()

    for root, _dirs, files in os.walk(src_dir):
        if "__pycache__" in root or ".pytest_cache" in root:
            continue
        assert "__init__.py" in files, f"Directory missing __init__.py: {root}"


def test_clean_import_no_side_effects() -> None:
    """Test that importing nexafreight has no side effects (no engine, app, scheduler, logging)."""
    code = """
import sys
import nexafreight

# Check no heavy frameworks were loaded during bare package import
assert "fastapi" not in sys.modules, "fastapi loaded unexpectedly"
assert "sqlalchemy.engine" not in sys.modules, "sqlalchemy.engine loaded unexpectedly"
assert "apscheduler" not in sys.modules, "apscheduler loaded unexpectedly"

# Check root logger has no unexpected handlers configured
import logging
assert len(logging.getLogger().handlers) == 0, "Root logger handlers configured"
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    msg = f"Clean import check failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert result.returncode == 0, msg
