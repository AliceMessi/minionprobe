"""Pytest plugin for the macOS repeat workflow (diagnostics only).

It changes no test and no assertion. It records, per PtySession, the harness
actions (send, resize) with the length of the raw PTY output at that moment,
and in PtySession.__exit__, after the test body has finished or raised, it
saves the raw PTY output, the action log and the emulator's final screen and
cursor for the resize test (every run) and for any failing session.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

RESIZE_TEST = "test_focus_pty_keeps_top_down_transcript_with_inline_input_across_resize"


def pytest_configure(config):  # noqa: ARG001
    from tests.e2e.cli.focus.harness import pty as pty_mod

    cls = pty_mod.PtySession
    out = Path(os.environ.get("FOCUS_DUMP_DIR", "focus-dump"))
    logs: dict[int, list] = {}
    original_send = cls.send
    original_resize = cls.resize
    original_exit = cls.__exit__

    def _log(session, entry):
        logs.setdefault(id(session), []).append(entry)

    def send(self, text):
        _log(self, {"t": time.monotonic(), "op": "send", "text": text[:60], "raw_len": len(self._transcript)})
        return original_send(self, text)

    def resize(self, *, rows, cols):
        before = {"t": time.monotonic(), "op": "resize", "rows": rows, "cols": cols, "raw_len": len(self._transcript),
                  "cursor_before": [self._screen.cursor.y + 1, self._screen.cursor.x + 1]}
        result = original_resize(self, rows=rows, cols=cols)
        before["raw_len_after_0.1s_read"] = len(self._transcript)
        before["cursor_after_pyte_resize_and_read"] = [self._screen.cursor.y + 1, self._screen.cursor.x + 1]
        _log(self, before)
        return result

    def __exit__(self, *exc):
        try:
            test = os.environ.get("PYTEST_CURRENT_TEST", "unknown").split(" ")[0]
            failed = bool(exc) and exc[0] is not None
            if failed or RESIZE_TEST in test:
                out.mkdir(parents=True, exist_ok=True)
                tag = os.environ.get("FOCUS_ITER", "0")
                safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", test.split("::")[-1])[-90:]
                base = out / f"{tag}-{'FAILED' if failed else 'passed'}-{safe}"
                self._read_available(timeout=0.05)
                Path(f"{base}.raw.txt").write_text(self._transcript, encoding="utf-8")
                rows = list(self._screen_display_lines())
                screen = {
                    "rows": self.rows,
                    "cols": self.cols,
                    "cursor_1based": [self._screen.cursor.y + 1, self._screen.cursor.x + 1],
                    "last_occupied_row_1based": max((i + 1 for i, r in enumerate(rows) if r), default=0),
                    "exception": repr(exc[1])[:2000] if failed else None,
                    "actions": logs.get(id(self), []),
                    "screen": [f"{i + 1:02d}|{r}" for i, r in enumerate(rows)],
                }
                Path(f"{base}.json").write_text(json.dumps(screen, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as error:  # diagnostics must never change the result
            print("focus_dump: could not save:", error)
        return original_exit(self, *exc)

    cls.send = send
    cls.resize = resize
    cls.__exit__ = __exit__
