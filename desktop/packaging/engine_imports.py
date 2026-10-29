"""
What the frozen app must carry for the engine it will download.

The engine is Python source, fetched at run time, verified and run by the app's own interpreter, so every library it
imports has to be inside the app already. PyInstaller only packs what it can see being imported, and it cannot see the
engine (it is not part of the app), so this reads the repository's engine the way the app's ``missing_modules`` check
will read the downloaded one: every import in ``engine/*.py`` and ``dlne_server.py``, standard library included.

    python engine_imports.py <repository root>      prints one module name per line
"""

import ast
import os
import sys
from typing import List, Set, Tuple


def engine_imports(repo: str) -> Tuple[List[str], List[str]]:
    """``(standard library modules, third-party modules)`` imported anywhere in the engine, as top-level names."""
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    found: Set[str] = set()
    paths = [os.path.join(repo, "dlne_server.py")]
    engine = os.path.join(repo, "engine")
    paths += [os.path.join(engine, name) for name in sorted(os.listdir(engine)) if name.endswith(".py")]
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module)
    tops = {name.split(".")[0] for name in found} - {"engine"}
    # Full dotted names for the standard library (xml.etree.ElementTree, concurrent.futures, ...).
    std_full = sorted(name for name in found if name.split(".")[0] in stdlib)
    third = sorted(top for top in tops if top not in stdlib and top != "gi")     # gi: the engine's Linux probe, inside a try
    return std_full, third


if __name__ == "__main__":
    standard, third_party = engine_imports(sys.argv[1] if len(sys.argv) > 1 else ".")
    print("\n".join(standard + third_party))
