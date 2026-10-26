# Provenance only: linux_setup.sh records the package, Python, OS and PostgreSQL
# versions and checks the wheel against PyPI; this run also installs 0.1.11.
echo "provenance-only run"
"$PY0111" -I "$B/provenance.py" "$RUNNER_TEMP/dl0111" "$OUT/provenance-linux-0.1.11.json" 0.1.11 > "$OUT/provenance-linux-0.1.11.log" 2>&1
echo "0.1.11 provenance: $(grep -c '"wheel_sha256_matches_pypi": true' "$OUT/provenance-linux-0.1.11.json") match"
