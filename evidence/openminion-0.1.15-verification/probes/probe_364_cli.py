"""Run the installed openminion CLI main with the participant clock and row ids fixed.

    python -I -X utf8 probe_364_cli.py <joined_at> <hex,hex,...> -- <openminion argv...>

This is the issue's tied-clock technique, in its own process. Only two names
are patched, in openminion.modules.storage.runtime.session_store.participants:
`utc_now_iso` (returns <joined_at>) and `uuid4` (returns the given hex ids in
turn, then real random ids). Everything else is the installed package:
`openminion.cli.main.main(argv)`, the room handlers and `_open_room_runtime`,
which starts APIRuntime.from_config_path from the config the argv names.
The exit status is main()'s return value.
"""

from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

PARTICIPANTS = "openminion.modules.storage.runtime.session_store.participants"


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[3] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    joined_at, ids, argv = sys.argv[1], [i for i in sys.argv[2].split(",") if i], sys.argv[4:]
    pending = list(ids)

    def next_id() -> SimpleNamespace:
        return SimpleNamespace(hex=pending.pop(0)) if pending else SimpleNamespace(hex=uuid.uuid4().hex)

    from openminion.cli.main import main as cli_main

    with patch(f"{PARTICIPANTS}.utc_now_iso", return_value=joined_at), patch(f"{PARTICIPANTS}.uuid4", side_effect=next_id):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
