"""Published-source check for the commentary-only PRs #627 (#581) and #628 (#582).

For every path a PR touched, it compares three revisions of the git clone:
  pre    = the accepted pre-cleanup source (the merge commit's first parent),
  merged = the PR's merge commit (the reviewed patch as landed),
  tag    = the release tag.
and records:
  1. bytes: tag == merged (the reviewed patch is what the tag carries);
  2. executable AST (ast.dump with every docstring removed; comments are not in
     the AST): pre vs merged, and pre vs tag;
  3. docstrings and comments (tokenize): tag vs merged, so the patch's
     explanations are retained as reviewed;
  4. test names (test_* functions/methods, Test* classes), fixtures
     (@pytest.fixture functions) and assertions (assert statements and
     pytest.raises / pytest.warns calls): pre vs tag;
  5. for a path changed after the merge: the commits that changed it and which
     top-level names differ, so the later change is attributed, not judged.
Markdown files: bytes only.

Usage: python -I release_equivalence.py GIT_DIR TAG ISSUE:PRE:MERGED [ISSUE:PRE:MERGED ...]
Exit 0 only if every path's AST pre vs merged is identical, every docstring and
comment of the merged revision is retained at the tag, and test names, fixtures
and assertion counts are unchanged pre vs tag.
"""

from __future__ import annotations

import ast
import hashlib
import io
import subprocess
import sys
import tokenize


def git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True,
        encoding="utf-8",
    ).stdout


def show(repo: str, rev: str, path: str) -> str:
    return git(repo, "show", f"{rev}:{path}")


def has_doc(node: ast.AST) -> bool:
    body = getattr(node, "body", None)
    return bool(
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )


def docstrings(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if has_doc(child):
                    found[name] = child.body[0].value.value  # type: ignore[attr-defined]
                visit(child, name + ".")
            else:
                visit(child, prefix)

    if has_doc(tree):
        found["<module>"] = tree.body[0].value.value  # type: ignore[attr-defined]
    visit(tree, "")
    return found


class Strip(ast.NodeTransformer):
    def strip(self, node: ast.AST) -> ast.AST:
        self.generic_visit(node)
        if has_doc(node):
            node.body = node.body[1:] or [ast.Pass()]  # type: ignore[attr-defined]
        return node

    visit_Module = strip
    visit_ClassDef = strip
    visit_FunctionDef = strip
    visit_AsyncFunctionDef = strip


def exec_dump(src: str) -> str:
    return ast.dump(Strip().visit(ast.parse(src)), include_attributes=False)


def comments(src: str) -> list[str]:
    return [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.COMMENT]


def _is_fixture(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(target, ast.Attribute) and target.attr == "fixture" or (
        isinstance(target, ast.Name) and target.id == "fixture")


def inventory(src: str) -> dict[str, object]:
    tree = ast.parse(src)
    tests: list[str] = []
    fixtures: list[str] = []
    asserts = raises = 0

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if child.name.startswith("Test"):
                    tests.append(f"{prefix}{child.name}")
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test"):
                    tests.append(f"{prefix}{child.name}")
                if any(_is_fixture(d) for d in child.decorator_list):
                    fixtures.append(f"{prefix}{child.name}")
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            asserts += 1
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("raises", "warns")
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest"):
            raises += 1
    return {"tests": sorted(tests), "fixtures": sorted(fixtures),
            "assert": asserts, "pytest_raises_warns": raises}


def top_level_names(src: str) -> dict[str, str]:
    """Named top-level defs by name; unnamed statements (imports, assignments) by
    their own dump, so a line shift alone is not reported as a difference."""
    out = {}
    for node in Strip().visit(ast.parse(src)).body:  # type: ignore[attr-defined]
        dump = ast.dump(node, include_attributes=False)
        name = getattr(node, "name", None) or f"{type(node).__name__}:{h(dump)}"
        out[name] = dump
    return out


def unparse_top(src: str, name: str) -> str:
    for node in Strip().visit(ast.parse(src)).body:  # type: ignore[attr-defined]
        dump = ast.dump(node, include_attributes=False)
        if (getattr(node, "name", None) or f"{type(node).__name__}:{h(dump)}") == name:
            return ast.unparse(node)
    return "<absent>"


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main(repo: str, tag: str, specs: list[str]) -> int:
    tag_commit = git(repo, "rev-parse", f"{tag}^{{commit}}").strip()
    print(f"tag {tag} -> commit {tag_commit}")
    print(f"python {sys.version.split()[0]}")
    bad = 0
    for spec in specs:
        issue, pre, merged = spec.split(":")
        paths = git(repo, "diff", "--name-only", pre, merged).split()
        print(f"\n######## #{issue}: pre {pre[:12]} merged {merged[:12]} tag {tag_commit[:12]}"
              f" ({len(paths)} paths)")
        summary = {"same_bytes": 0, "changed_after_merge": [], "ast_pre_merged_same": 0,
                   "ast_pre_tag_same": 0, "docs_comments_retained": 0, "inventory_same": 0, "py": 0}
        for path in paths:
            p_src, m_src, t_src = (show(repo, r, path) for r in (pre, merged, tag_commit))
            same = m_src == t_src
            summary["same_bytes"] += same
            print(f"\n== {path}")
            print(f"   tag bytes == merged: {same} (sha256[:16] tag {h(t_src)})")
            if not same:
                log = git(repo, "log", "--format=%h %s", f"{merged}..{tag_commit}", "--", path).strip()
                summary["changed_after_merge"].append(path)
                print("   changed after the merge by:\n     " + log.replace("\n", "\n     "))
            if not path.endswith(".py"):
                continue
            summary["py"] += 1
            e_pre, e_m, e_t = exec_dump(p_src), exec_dump(m_src), exec_dump(t_src)
            pm, pt = e_pre == e_m, e_pre == e_t
            summary["ast_pre_merged_same"] += pm
            summary["ast_pre_tag_same"] += pt
            print(f"   executable AST pre == merged: {pm}; pre == tag: {pt}"
                  f" (sha256[:16] pre {h(e_pre)}, merged {h(e_m)}, tag {h(e_t)})")
            bad += not pm
            if not pt:
                a, b = top_level_names(m_src), top_level_names(t_src)
                diff = sorted(n for n in set(a) | set(b) if a.get(n) != b.get(n))
                print(f"   top-level statements differing merged vs tag: {diff}")
                for n in diff:
                    if ":" in n:  # unnamed statement: show it (absent on one side)
                        side = "tag only" if n in b else "merged only"
                        src = t_src if n in b else m_src
                        print(f"     {side}: {unparse_top(src, n)}")
            d_m, d_t = docstrings(ast.parse(m_src)), docstrings(ast.parse(t_src))
            c_m, c_t = comments(m_src), comments(t_src)
            missing_docs = sorted(n for n in d_m if d_t.get(n) != d_m[n])
            new_docs = sorted(n for n in d_t if n not in d_m)
            missing_comments = [c for c in c_m if c not in c_t]
            ok = not missing_docs and not missing_comments
            summary["docs_comments_retained"] += ok
            bad += not ok
            print(f"   merged docstrings retained at tag: {len(d_m) - len(missing_docs)}/{len(d_m)}"
                  f"{'' if not missing_docs else ' MISSING ' + str(missing_docs)}"
                  f"{'' if not new_docs else '; new at tag: ' + str(new_docs)}")
            print(f"   merged comments retained at tag: {len(c_m) - len(missing_comments)}/{len(c_m)}"
                  f" (tag has {len(c_t)})")
            i_p, i_t = inventory(p_src), inventory(t_src)
            inv_same = i_p == i_t
            summary["inventory_same"] += inv_same
            bad += not inv_same
            print(f"   tests {len(i_t['tests'])}, fixtures {len(i_t['fixtures'])}, assert {i_t['assert']},"
                  f" pytest.raises/warns {i_t['pytest_raises_warns']}; identical pre vs tag: {inv_same}")
            if not inv_same:
                for k in i_p:
                    if i_p[k] != i_t[k]:
                        print(f"     {k}: pre {i_p[k]} tag {i_t[k]}")
        print(f"\n#{issue} summary: {summary}")
    print(f"\nRESULT {'PASS' if not bad else 'FAIL'} (problems: {bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
