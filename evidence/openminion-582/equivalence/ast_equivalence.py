"""Executable equivalence of a commentary-only diff.

For every .py file changed between BASE and HEAD in REPO:
1. both versions parse;
2. ast.dump of both, with every docstring removed, is identical (comments are
   not in the AST, so this proves the change is comments/docstrings only);
3. every docstring that differs is listed by its owner (module, class or def
   qualified name) with its old and new text.

Usage: python -I ast_equivalence.py REPO BASE HEAD
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True,
        encoding="utf-8",
    ).stdout


def _has_docstring(node: ast.AST) -> bool:
    body = getattr(node, "body", None)
    return bool(
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )


def _docstrings(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if _has_docstring(child):
                    found[name] = child.body[0].value.value  # type: ignore[attr-defined]
                visit(child, name + ".")
            else:
                visit(child, prefix)

    if _has_docstring(tree):
        found["<module>"] = tree.body[0].value.value  # type: ignore[attr-defined]
    visit(tree, "")
    return found


class _Strip(ast.NodeTransformer):
    def _strip(self, node: ast.AST) -> ast.AST:
        self.generic_visit(node)
        if _has_docstring(node):
            node.body = node.body[1:] or [ast.Pass()]  # type: ignore[attr-defined]
        return node

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def _stripped_dump(source: str) -> str:
    return ast.dump(_Strip().visit(ast.parse(source)), include_attributes=False)


def main(repo: str, base: str, head: str) -> int:
    files = [
        f for f in _git(repo, "diff", "--name-only", base, head).split()
        if f.endswith(".py")
    ]
    failures = 0
    print(f"base {_git(repo, 'rev-parse', base).strip()}")
    print(f"head {_git(repo, 'rev-parse', head).strip()}")
    print(f"python {sys.version.split()[0]}")
    for path in files:
        old = _git(repo, "show", f"{base}:{path}")
        new = _git(repo, "show", f"{head}:{path}")
        old_tree, new_tree = ast.parse(old), ast.parse(new)
        a, b = _stripped_dump(old), _stripped_dump(new)
        same = a == b
        failures += not same
        digest = hashlib.sha256(a.encode()).hexdigest()[:16]
        print(f"\n== {path}")
        print(f"   ast without docstrings identical: {same} (sha256[:16] base {digest}"
              f", head {hashlib.sha256(b.encode()).hexdigest()[:16]})")
        print(f"   ast with docstrings identical: {ast.dump(old_tree) == ast.dump(new_tree)}")
        old_docs, new_docs = _docstrings(old_tree), _docstrings(new_tree)
        changed = sorted(
            name for name in set(old_docs) | set(new_docs)
            if old_docs.get(name) != new_docs.get(name)
        )
        print(f"   docstrings changed: {len(changed)}")
        for name in changed:
            print(f"   - {name}")
            print("     old: " + ascii(old_docs.get(name)))
            print("     new: " + ascii(new_docs.get(name)))
    print(f"\nfiles: {len(files)}, ast mismatches: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
