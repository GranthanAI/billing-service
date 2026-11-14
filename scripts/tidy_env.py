"""Moves trailing `KEY=value   # note` comments onto their own line above (in place). Values are never changed.

    python scripts/tidy_env.py .env.dev
"""
import re
import sys

path = sys.argv[1]
out = []
for line in open(path, encoding="utf-8").read().split("\n"):
    m = re.match(r"""^([A-Za-z_][A-Za-z0-9_]*=[^#"']*[^\s#"'])\s+#\s*(.*)$""", line)
    out += ["# " + m.group(2), m.group(1)] if m else [line]
open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
