# #393 PR: the full make test-ci on the branch head.
sec "make test-ci"; make test-ci 2>&1 | tail -60; echo "test-ci exit=${PIPESTATUS[0]}"
