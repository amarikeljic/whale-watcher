"""Part 4 #1: which code made each row, and headlines that count one model version."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import DAY, NOW
from dashfix import A, B, Fixture
from whalewatcher import bankroll as br
from whalewatcher import dashboard as dash
from whalewatcher import version
from whalewatcher.config import Config
from whalewatcher.db import STAMPED_TABLES, Database
from whalewatcher.stats import Scope, daily_summary, render

ROOT = Path(version.__file__).resolve().parent


def test_decision_code_has_not_changed_without_deciding_about_the_model_version():
    digest = version.source_digest()
    assert digest == version.MODEL_SOURCE_DIGEST, (
        "Code that decides, prices, sizes or settles a copy has changed (whalewatcher/version.py, "
        "MODEL_SOURCES). If the change can alter a decision, a price, a stake or a settlement, raise "
        "MODEL_VERSION and describe it in MODEL_CHANGES. If it cannot, record that by setting "
        f"MODEL_SOURCE_DIGEST = {digest!r}.")


def test_the_digest_ignores_comments_and_docstrings_but_not_code(tmp_path):
    pkg = tmp_path / "whalewatcher"
    pkg.mkdir()
    for name in version.MODEL_SOURCES:
        shutil.copy(ROOT / name, pkg / name)
    shutil.copy(ROOT.parent / "config.yaml", tmp_path / "config.yaml")
    before = version.source_digest(pkg)
    bot = pkg / "bot.py"
    bot.write_text(bot.read_text().replace('"""', '"""A new sentence. ', 1) + "\n# a comment\n")
    assert version.source_digest(pkg) == before
    bot.write_text(bot.read_text() + "\nSOMETHING = 1\n")
    assert version.source_digest(pkg) != before
    shipped = tmp_path / "config.yaml"
    shutil.copy(ROOT.parent / "config.yaml", shipped)
    bot.write_text((ROOT / "bot.py").read_text())
    assert version.source_digest(pkg) == before
    shipped.write_text(shipped.read_text() + "\nextra_setting: 1\n")
    assert version.source_digest(pkg) != before, "the shipped config.yaml counts too"


def test_the_commit_comes_from_the_build_and_anything_odd_is_unknown(monkeypatch):
    for given, seen in (("ABC1234", "abc1234"), ("abc1234-dirty", "abc1234-dirty"), ("", "unknown"),
                        ("abc'; DROP TABLE alerts; --", "unknown"), ("xyz", "unknown")):
        monkeypatch.setenv("WHALEWATCHER_COMMIT", given)
        assert version.code_commit() == seen
    monkeypatch.delenv("WHALEWATCHER_COMMIT")
    assert version.code_commit() == "unknown"


def test_the_settings_fingerprint_follows_decisions_not_plumbing():
    cfg = Config()
    base = version.settings_hash(cfg)
    cfg.api.timeout_seconds += 5
    cfg.database_path = "/elsewhere.sqlite3"
    cfg.stats.bootstrap_samples += 1
    assert version.settings_hash(cfg) == base, "where the data is and how reports look change no decision"
    cfg.bot.max_slippage += 0.01
    assert version.settings_hash(cfg) != base and len(base) == 12


async def test_every_row_the_service_writes_is_stamped_and_an_explicit_version_is_kept(app, clock):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 2, pnl=1.0)
    stamp = {"code_commit": "unknown", "model_version": version.MODEL_VERSION,
             "settings_hash": version.settings_hash(app.cfg)}
    for table in ("bot_trades", "alerts"):
        assert dict(app.db.one(f"SELECT code_commit, model_version, settings_hash FROM {table}")) == stamp
    br.sync_runs(app, ["single"])
    assert app.db.one("SELECT model_version FROM bankroll_runs")["model_version"] == version.MODEL_VERSION
    app.db.execute("INSERT INTO score_runs (as_of, kind, created_at, config_json, sources_json, n_candidates, "
                   "n_passed, model_version) VALUES (1, 'score', 1, '{}', '{}', 0, 0, 7)")
    assert app.db.one("SELECT model_version, code_commit FROM score_runs")["model_version"] == 7, "set explicitly"


def test_the_stamp_lives_on_the_connection_not_in_the_file(tmp_path):
    path = str(tmp_path / "x.sqlite3")
    db = Database(path)
    db.stamp_new_rows(commit="abc1234", model_version=1, settings="0123456789ab")
    other = sqlite3.connect(path)
    other.execute("INSERT INTO score_runs (as_of, kind, created_at, config_json, sources_json, n_candidates, "
                  "n_passed) VALUES (1, 'score', 1, '{}', '{}', 0, 0)")
    other.commit()
    assert db.one("SELECT model_version FROM score_runs")["model_version"] is None, "another program: no stamp"
    assert not db.query("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'stamp_%'")
    with pytest.raises(ValueError):
        db.stamp_new_rows(commit="x'; DROP TABLE alerts; --", model_version=1, settings="0")
    other.close()
    db.close()
    ro = Database.readonly(path)
    assert ro.read_only and set(STAMPED_TABLES) == {"alerts", "bot_trades", "paper_trades", "bankroll_runs",
                                                    "score_runs"}
    ro.close()


# -- the headline counts one model version ---------------------------------------------------------


def before_tagging(app) -> None:
    """Turn every row so far into one written before stamping existed."""
    for table in STAMPED_TABLES:
        app.db.execute(f"UPDATE {table} SET code_commit = NULL, model_version = NULL, settings_hash = NULL")


async def test_the_headline_counts_the_current_model_and_shows_older_rows_apart(app):
    fx = Fixture(app.db, NOW)
    for days, pnl in ((9, 5.0), (8, -10.0), (7, 4.0)):
        fx.trade(A, days, pnl=pnl)
    before_tagging(app)
    fx.trade(B, 2, pnl=2.0)
    text = render(app)
    head, _, rest = text.partition("BOT PAPER TRADES, BEFORE TAGGING")
    assert "counting model 1 (current) | code commits: unknown (1)" in head
    assert "kept apart, never added in: 3 signals from before tagging" in head
    assert "signals 1 | filled 1 | missed 0 | skipped 0 | waiting 0 | closed 1 | open 0" in head
    assert "NOT ENOUGH DATA TO DRAW CONCLUSIONS YET: 1 closed bot paper trades (flat_hold, model 1)" in head
    assert "kept apart, never added to the headline above" in rest and "signals 3 | filled 3" in rest
    before = render(app, scope=Scope("before"))
    assert "counting before tagging" in before and "signals 3 | filled 3" in before
    assert "BOT PAPER TRADES, BEFORE TAGGING" not in before, "shown once, as the subject"
    mixed = render(app, scope=Scope("all"))
    assert "every model version mixed (for looking only: not a result)" in mixed and "signals 4 | filled 4" in mixed
    brief = daily_summary(app)
    assert "kept apart, never added in: 3 signals from before tagging" in brief
    assert "single / flat_hold, before tagging (kept apart): net -$1.00, n=3 closed" in brief


async def test_rows_made_under_different_settings_are_flagged(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 3, pnl=1.0)
    app.db.execute("UPDATE bot_trades SET settings_hash = 'aaaaaaaaaaaa'")
    fx.trade(A, 2, pnl=1.0)
    assert "these rows were made under 2 different settings (config.yaml changed)" in render(app)


async def test_signals_inside_an_outage_are_left_out_unless_asked(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 3, pnl=5.0, hour=11)
    fx.trade(A, 2, pnl=1.0, hour=15)
    start = NOW - 3 * DAY
    start = start - start % DAY + 10 * 3600
    fx.db.execute("INSERT INTO outage_windows (start_ts, end_ts, cause, source) VALUES (?, ?, 'test', 'owner')",
                  (start, start + 2 * 3600))
    text = render(app)
    assert "left out: 1 signals inside 1 outage window(s) (`stats --include-outages` counts them)" in text
    assert "signals 1 | filled 1" in text
    assert "signals 2 | filled 2" in render(app, scope=Scope(include_outages=True))


def test_a_scope_is_checked_before_use():
    assert Scope.parse("2").wants_model(2) and not Scope.parse("2").wants_model(None)
    assert Scope.parse(None).model == "current" and Scope.parse("ALL").wants_model(None)
    with pytest.raises(ValueError):
        Scope.parse("latest")


async def test_a_new_model_version_ends_the_wallet_run_and_freezes_it_with_its_own_trades(live_app, clock,
                                                                                          monkeypatch):
    app = live_app
    br.sync_runs(app, ["single"])
    first = br.open_run(app.db, "single")
    fx = Fixture(app.db, NOW)
    fx.trade(A, -0.01, pnl=1.0, hold_days=0.001)
    clock.advance(DAY)
    monkeypatch.setattr(br, "MODEL_VERSION", version.MODEL_VERSION + 1)
    changes = br.sync_runs(app, ["single"])
    assert changes[0] == (f"single: ended run #{first['id']} (model version changed from model "
                          f"{version.MODEL_VERSION} to model {version.MODEL_VERSION + 1})")
    frozen = app.db.one("SELECT summary_json FROM bankroll_runs WHERE id = ?", (first["id"],))["summary_json"]
    assert '"taken": 1' in frozen, "the frozen run is valued with the trades of its own model version"
    assert br.open_run(app.db, "single")["id"] != first["id"]


def test_the_dashboard_counts_the_current_model_and_offers_the_older_rows(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    db = Database(path)
    fx = Fixture(db, NOW)
    fx.trade(A, 9, pnl=5.0)
    fx.trade(A, 8, pnl=-10.0)                     # written before stamping existed
    db.stamp_new_rows(commit="abc1234", model_version=version.MODEL_VERSION, settings="0123456789ab")
    fx.trade(B, 2, pnl=2.0)
    db.close()
    cfg = Config(database_path=path)

    def get(url: str, **query) -> str:
        return dash.render(cfg, path, url, query, now=NOW)[1]

    html = get("/")
    assert "n=1 closed" in html and "Bot numbers count model 1 (current)." in html
    assert "Kept apart: 2 signals from before tagging (pick them under Version)." in html
    assert '<option value="before">Before tagging</option>' in html
    before = get("/", model="before")
    assert "n=2 closed" in before and "model=before" in before, "the choice is carried to the other pages"
    mixed = get("/", model="all")
    assert "n=3 closed" in mixed and 'class="card-note warn"' in mixed and "not a result" in mixed
    assert "Bot numbers count model 1 (current)." in get("/bankroll"), "the wallet page says what it counts"
    assert "n=1 closed" in get("/", model="nonsense"), "anything else is the current model"
