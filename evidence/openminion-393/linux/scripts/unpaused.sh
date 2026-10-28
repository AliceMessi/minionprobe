# #393: the PR's unpaused probe, three runs on the branch head and three on dev sources
# (git checkout $DEV -- src), SQLite and PostgreSQL. stdout is printed; stderr is summarised.
sha256sum /tmp/ev/probe_393_unpaused.py
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"; fi
  sec "[$tree] openminion imported from: $(PYTHONPATH=src $P -c 'import openminion; print(openminion.__file__)')"
  for run in 1 2 3; do
    sec "[$tree] run $run"
    PYTHONPATH=src $P /tmp/ev/probe_393_unpaused.py 2> /tmp/ev/stderr-$tree-$run.txt; echo "probe exit=$?"
    echo "(stderr: $(wc -l < /tmp/ev/stderr-$tree-$run.txt) lines; $(grep -c 'has pending migrations' /tmp/ev/stderr-$tree-$run.txt) of them are the 'Postgres module ... has pending migrations' log warning; the others follow)"
    grep -v 'has pending migrations' /tmp/ev/stderr-$tree-$run.txt | head -20
  done
done
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
