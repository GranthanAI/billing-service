"""G2: tidy_env moves trailing comments and keeps each key once (the last assignment wins), never changing a value."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("tidy_env", Path(__file__).resolve().parents[2] / "scripts" / "tidy_env.py")
tidy_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tidy_env)


def test_duplicates_keep_the_last_value_and_trailing_comments_move():
    text = "A=1\nGATEWAY_SHARED_SECRET=dev-gateway-secret\n# note\nB=2   # two\nGATEWAY_SHARED_SECRET=dev-gateway-abc\nA=3\n"
    out = tidy_env.tidy(text)
    lines = out.split("\n")
    assert [l for l in lines if l.startswith("GATEWAY_SHARED_SECRET=")] == ["GATEWAY_SHARED_SECRET=dev-gateway-abc"]
    assert [l for l in lines if l.startswith("A=")] == ["A=3"]
    assert "# two" in lines and lines.index("# two") + 1 == lines.index("B=2")
    assert "# note" in lines


def test_quoted_values_and_comments_are_left_alone():
    text = 'EMAIL_FROM="GranthanAI <billing@x>"\n# KEY=ignored\nURL=http://a/b#frag\n'
    assert tidy_env.tidy(text) == text


def test_idempotent():
    once = tidy_env.tidy("A=1\nA=2  # x\nB=3\n")
    assert tidy_env.tidy(once) == once
