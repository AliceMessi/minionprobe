# Provenance only: the published 0.1.13 and 0.1.12 on the runner (run with
# NEED_0112=1). Sourced by linux_setup.sh from an empty directory, after its
# own provenance of 0.1.13.
"$PY0112" -I "$B/provenance.py" "$RUNNER_TEMP/dl0112" "$OUT/provenance-linux-0.1.12.json" 0.1.12 > "$OUT/provenance-linux-0.1.12.log" 2>&1
echo "provenance 0.1.12: $(grep -c '"wheel_sha256_matches_pypi": true' "$OUT/provenance-linux-0.1.12.json") match"
