"""Observation outside #437: what one in-process APIRuntime start writes into the cwd.

    python -I -X utf8 probe_437_cwd.py <work_dir> <out.json>

Run from a new, empty directory. Starts and closes one APIRuntime whose config,
home_root and data_root are all under <work_dir> (SQLite, offline echo agent),
then lists every entry that appeared in the current working directory. It is
recorded, not checked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import banner, quiet, write_result  # noqa: E402
from probe_437_lib import strip_env, write_config  # noqa: E402


def main() -> int:
    work, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    strip_env()
    quiet()
    info = banner("probe_437_cwd")
    from openminion import APIRuntime

    before = sorted(os.listdir("."))
    cfg = write_config(work)
    rt = APIRuntime.from_config_path(str(cfg), home_root=work, data_root=work / ".openminion")
    rt.close()
    after = sorted(p.as_posix() for p in Path(".").rglob("*"))
    files = [p for p in after if Path(p).is_file()]
    result = {"banner": info, "cwd_entries_before": before, "cwd_entries_after": after, "files_after": files}
    write_result(out, result)
    print(f"cwd before: {before}")
    print(f"cwd after: {after}")
    print(f"files: {files}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
