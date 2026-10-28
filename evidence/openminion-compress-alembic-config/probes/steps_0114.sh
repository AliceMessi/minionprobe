# Sourced by linux_setup.sh (PY = the 0.1.14 venv's python, B = this bundle, OUT = outputs).
"$PY" -I -X utf8 "$B/probe_compress_alembic_config.py" "$OUT" 0.1.14 > "$OUT/probe_compress_alembic_config_0.1.14.log" 2>&1
echo "probe exit=$?" >> "$OUT/probe_compress_alembic_config_0.1.14.log"
cat "$OUT/probe_compress_alembic_config_0.1.14.log"
