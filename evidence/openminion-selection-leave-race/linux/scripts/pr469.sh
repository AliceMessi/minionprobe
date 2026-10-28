# PR #469 head c00bb452 plus the probe commit: the probe, three runs, SQLite and PostgreSQL.
echo "src identical to c00bb452: $(git diff --quiet c00bb452 HEAD -- src && echo yes || echo no)"
probe_runs pr469
