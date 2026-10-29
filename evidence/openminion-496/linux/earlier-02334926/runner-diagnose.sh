#!/bin/bash
set -u
P=.venv/bin/python
echo "head: $(git log --oneline -1)"
echo "== ruff format --diff (whole tree)"; .venv/bin/ruff format --diff . 2>&1 | head -60
echo "== services_layout"; $P -m scripts.validate.services_layout 2>&1 | head -5
echo "== failing case"; $P -m pytest -q -p no:cacheprovider --tb=long -vv "tests/runtime/test_turn_optional_memory_writes.py::test_turn_without_memory_write_writes_no_decision_record_or_tool_outcome[require-approval-model-call-approved]" 2>&1 | grep -E "^E |assert|events|Error" | head -60
