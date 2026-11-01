"""Generate src/billing/gen from proto/ (the `make proto` target).

Runs grpc_tools.protoc, then rewrites the generated imports so the code lives under
`billing.gen` instead of a top-level `granthan` package. The serialized descriptors inside the
_pb2.py files are left untouched.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "proto"
DEFAULT_OUT = ROOT / "src" / "billing" / "gen"
PREFIX = "billing.gen."


def main(out: Path = DEFAULT_OUT) -> None:
    OUT = out
    files = sorted(p.relative_to(PROTO).as_posix() for p in PROTO.rglob("*.proto"))
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.rglob("*"):
        if stale.is_file() and stale.name != "__init__.py":
            stale.unlink()
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO}",
        f"--python_out={OUT}",
        f"--grpc_python_out={OUT}",
        f"--mypy_out={OUT}",
        f"--mypy_grpc_out={OUT}",
        *files,
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)

    for path in OUT.rglob("*.py*"):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            new = re.sub(r"^(from|import) granthan\.", rf"\1 {PREFIX}granthan.", text, flags=re.M)
        else:  # .pyi: no serialized descriptors, so every reference can be rewritten
            new = re.sub(
                r"(?<![\w.])granthan\.(billing|events)\.v1", PREFIX + r"granthan.\1.v1", text
            )
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
    dirs = [OUT, *(p for p in OUT.rglob("*") if p.is_dir() and p.name != "__pycache__")]
    for d in dirs:
        (d / "__init__.py").touch()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
