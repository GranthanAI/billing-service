"""Normalises an env file in place (values are never changed):

  1. a trailing `KEY=value   # note` comment moves onto its own line above the assignment;
  2. each KEY is assigned exactly once: when a key appears more than once the LAST assignment wins (that is what
     `source` and python-dotenv do anyway) and the earlier ones are dropped, so nobody copies the wrong line.

    python scripts/tidy_env.py .env.dev
"""
import re
import sys

ASSIGN = re.compile(r"""^([A-Za-z_][A-Za-z0-9_]*)=""")
TRAILING = re.compile(r"""^([A-Za-z_][A-Za-z0-9_]*=[^#"']*[^\s#"'])\s+#\s*(.*)$""")


def tidy(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        m = TRAILING.match(line)
        out += ["# " + m.group(2), m.group(1)] if m else [line]
    last: dict[str, int] = {}
    for i, line in enumerate(out):
        m = ASSIGN.match(line)
        if m:
            last[m.group(1)] = i
    kept = [line for i, line in enumerate(out) if not (ASSIGN.match(line) and last[ASSIGN.match(line).group(1)] != i)]
    return "\n".join(kept)


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(tidy(text))
