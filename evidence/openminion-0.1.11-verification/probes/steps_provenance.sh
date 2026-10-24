# Provenance only: the package, Python, OS and PostgreSQL versions on the runner.
cat "$OUT/pip-show.txt" "$OUT/os.txt" "$OUT/postgres-version.txt"
"$PY" -I -c "import openminion,sys;print(openminion.__file__.replace(sys.prefix,'<VENV>'), openminion.__version__)"
