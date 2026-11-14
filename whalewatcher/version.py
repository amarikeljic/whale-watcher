"""Which code made a row (v2.1 Part 4 #1).

Every new alert, bot trade, paper trade, wallet run and score run is stamped with:

* ``model_version``: a number changed BY HAND whenever code that decides whether to
  copy, prices a fill, sizes a stake, or settles or marks a trade changes. Headlines
  combine only rows of one model version. A deploy that changes only documents, the
  dashboard or the reports keeps the number, so the sample keeps growing.
* ``code_commit``: the git commit the image was built from. The image has no ``.git``,
  so it is passed in at build time (``GIT_COMMIT``, see the Dockerfile). "unknown" when
  it was not passed. Never guessed.
* ``settings_hash``: a short fingerprint of the configuration that was loaded. A setting
  changed on the server is not a new model version, but rows made under different
  settings should not be silently pooled either: ``stats`` warns when one model
  version's rows were made under more than one.

Rows from before stamping have NULL in all three and are shown as their own group,
"before tagging". They are never deleted, never relabelled, never mixed into a headline.

``MODEL_SOURCE_DIGEST`` guards against forgetting the bump: it is a hash of the decision
code (comments and docstrings left out) and ``tests/test_version.py`` fails when the code
changes and the digest does not. Fixing that test is the moment to decide: bump the
model version, or record that the change cannot alter a decision. It is a reminder, not
a proof: a change outside these files can still alter a decision.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

MODEL_VERSION = 1

# What each model version changed. The newest is the one stamped on new rows.
MODEL_CHANGES: dict[int, tuple[str, str]] = {
    1: ("2026-11-14", "First tagged version: the bot model as deployed with the Part 4 fixes. Everything "
                      "recorded before it is 'before tagging'."),
}

BEFORE_TAGGING = "before tagging"

# The decision code: which wallets are watched, which fills become signals, the fill
# price, the fees, the stake, the settlement, the marks. config.py holds the defaults.
MODEL_SOURCES = (
    "backtest.py", "bankroll.py", "bot.py", "config.py", "consensus.py", "costs.py", "detector.py", "fees.py", "followup.py",
    "horizon.py", "ledger.py", "markets.py", "monitor.py", "paper.py", "polymarket.py", "polymarket_us.py",
    "scoring.py", "timeutil.py", "watchlist.py",
)
MODEL_SOURCE_DIGEST = "5305fffa58d2e6e2"

_COMMIT = re.compile(r"[0-9a-f]{4,40}(-dirty)?")
# Sections that change nothing about a decision: where the database is, how the API
# is reached (hosts, timeouts, politeness limits) and how reports are laid out.
_NOT_SETTINGS = ("database_path", "api", "stats")


def code_commit() -> str:
    """The commit passed in at build time, or "unknown". Anything that does not look
    like a commit is "unknown": the value ends up in SQL and on the dashboard."""
    value = os.environ.get("WHALEWATCHER_COMMIT", "").strip().lower()
    return value if _COMMIT.fullmatch(value) else "unknown"


def settings_hash(cfg: Any) -> str:
    """Twelve hex digits of the loaded configuration, without the sections in _NOT_SETTINGS."""
    data = {k: v for k, v in cfg.public_dict().items() if k not in _NOT_SETTINGS}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]


def label(version: int | None) -> str:
    return BEFORE_TAGGING if version is None else f"model {version}"


def describe() -> list[str]:
    lines = [f"model version {MODEL_VERSION} (the one new rows are stamped with), code commit {code_commit()}"]
    for v in sorted(MODEL_CHANGES, reverse=True):
        day, text = MODEL_CHANGES[v]
        lines.append(f"  model {v}, {day}: {text}")
    return lines


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def source_digest(root: Path | None = None) -> str:
    """A hash of the decision code's syntax trees (so comments and docstrings do not
    count) plus the shipped config.yaml."""
    root = root or Path(__file__).resolve().parent
    h = hashlib.sha256()
    for name in MODEL_SOURCES:
        tree = _strip_docstrings(ast.parse((root / name).read_text()))
        h.update(name.encode() + b"\0" + ast.dump(tree, include_attributes=False).encode() + b"\0")
    shipped = root.parent / "config.yaml"
    if shipped.exists():
        import yaml
        h.update(json.dumps(yaml.safe_load(shipped.read_text()), sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]
