# Sourced by linux_setup.sh (PY = the 0.1.14 venv's python, B = this bundle, OUT = outputs).
"$PY" -I -X utf8 "$B/probe_postgres_autoincrement_baseline.py" "$OUT" 0.1.14 > "$OUT/probe_postgres_autoincrement_baseline_0.1.14.log" 2>&1
echo "probe exit=$?" >> "$OUT/probe_postgres_autoincrement_baseline_0.1.14.log"
cat "$OUT/probe_postgres_autoincrement_baseline_0.1.14.log"
