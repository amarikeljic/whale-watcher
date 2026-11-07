"""The command line parses what the README documents and fails cleanly on bad input."""

from whalewatcher.cli import build_parser, main


def test_documented_commands_parse():
    p = build_parser()
    a = p.parse_args(["score", "--as-of", "2026-11-01"])
    assert (a.command, a.as_of, a.show) == ("score", "2026-11-01", 25)
    a = p.parse_args(["validate", "--as-of", "2026-11-01", "--holdout-months", "2", "--rank-by", "clv_markout"])
    assert (a.command, a.holdout_months, a.rank_by) == ("validate", 2, "clv_markout")
    assert p.parse_args(["monitor", "--no-post", "--cycles", "3"]).cycles == 3
    assert p.parse_args(["stats", "--post"]).post
    assert p.parse_args(["export", "--csv", "x.csv"]).csv == "x.csv"
    assert p.parse_args(["health"]).command == "health"


def test_bad_date_is_a_clean_error(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["score", "--as-of", "next tuesday"]) == 2
    assert "not a date" in capsys.readouterr().err


def test_config_typo_is_a_clean_error(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("alerts:\n  min_prise: 0.1\n")
    assert main(["stats"]) == 2
    assert "min_prise" in capsys.readouterr().err
