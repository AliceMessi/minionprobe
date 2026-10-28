"""Run pytest in the current tree, then report which openminion it imported."""
import platform
import sqlite3
import sys

import pytest

rc = pytest.main(["-q", "-p", "no:cacheprovider", "-rfE", "--tb=line", *sys.argv[1:]])
import openminion  # noqa: E402

print(f"# pytest imported openminion from {openminion.__file__}")
print(f"# {platform.platform()} | CPython {sys.version.split()[0]} | SQLite {sqlite3.sqlite_version} | pytest {pytest.__version__}")
print(f"# exit={int(rc)}")
