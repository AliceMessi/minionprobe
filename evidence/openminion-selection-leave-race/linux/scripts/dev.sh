# dev 39da8ffb plus the probe commit: the probe, three runs, SQLite and PostgreSQL.
echo "src identical to 39da8ffb: $(git diff --quiet 39da8ffb HEAD -- src && echo yes || echo no)"
probe_runs dev
