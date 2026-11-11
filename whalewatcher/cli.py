"""Command line: ``python -m whalewatcher <command>``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence

from dotenv import load_dotenv

from . import config as config_mod
from .app import open_app
from .scoring import WalletMetrics, run_scoring
from .timeutil import iso, now_ts, parse_as_of, start_of_day
from .validate import ValidationReport, run_validation

log = logging.getLogger("whalewatcher")


def short(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-3:]}" if len(wallet) > 12 else wallet


def _f(value: float | None, spec: str, dash: str = "-") -> str:
    return dash if value is None else format(value, spec)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.rjust(w) for h, w in zip(headers, widths, strict=True))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.rjust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join(out)


METRIC_HEADERS = ("rank", "wallet", "settled", "total_pnl", "roi", "prof_mo", "max_share", "avg_stake",
                  "days_since", "two_sided", "clv", "clv_n", "markout24h", "win_rate")


def metric_row(m: WalletMetrics) -> list[str]:
    return [
        str(m.rank) if m.rank else "-", short(m.wallet), str(m.settled_markets), _f(m.total_pnl, ",.0f"),
        _f(m.roi, ".1%"), f"{m.profitable_months}/{m.months_in_window}", _f(m.max_single_market_share, ".2f"),
        _f(m.avg_stake_usd, ",.0f"), _f(m.days_since_last_trade, ".1f"), _f(m.two_sided_ratio, ".2f"),
        _f(m.clv, "+.3f"), str(m.clv_n), _f(m.clv_markout, "+.3f"), _f(m.win_rate, ".0%"),
    ]


def print_score(run, *, show: int, as_of: int) -> None:
    print(f"\nWallet scores as of {iso(as_of)} (run #{run.run_id}), using only data before that instant.")
    for note in run.notes:
        print(f"  NOTE: {note}")
    passed = [m for m in run.metrics if m.rank is not None]
    print(f"  candidates: {len(run.metrics)}   passed all filters: {len(passed)}   "
          f"selected for monitoring: {len(run.selected)}")
    print(f"  pool sources: {', '.join(f'{k}={v}' for k, v in sorted(run.sources.items()))}")
    if passed:
        print("\nTop wallets (passed every filter, ranked):")
        print(table(METRIC_HEADERS, [metric_row(m) for m in passed[:show]]))
    nearest = [m for m in run.metrics if m.rank is None and not m.truncated][: max(0, show - len(passed[:show]))]
    if nearest:
        print("\nClosest misses (failed at least one filter), by total P&L:")
        print(table(METRIC_HEADERS + ("fails",),
                    [metric_row(m) + ["; ".join(m.fail_reasons)[:90]] for m in nearest]))
    reasons = Counter(r.split(" ")[0].split("(")[0] for m in run.metrics for r in m.fail_reasons)
    if reasons:
        print("\nWhy wallets failed (a wallet can fail several):")
        for reason, count in reasons.most_common():
            print(f"  {reason:28s} {count}")
    if run.watch is not None and run.watch.stages:
        print("\nWhere the time went:")
        print(run.watch.table())
    print("\nclv = closing price minus entry VWAP (spec definition: pre-game for sports, market close otherwise);"
          "\nmarkout24h = price 24h after the first buy minus that buy's price. Outside sports the spec's"
          "\nclosing price is the outcome itself, so clv there mostly repeats realized ROI.")


def print_validation(rep: ValidationReport) -> None:
    print(f"\nValidation: select at {iso(rep.cutoff)} using only earlier data, then measure "
          f"{iso(rep.cutoff)} .. {iso(rep.as_of)}")
    print(f"  ranked by {rep.rank_by}; selection snapshot run #{rep.selection_run_id}")
    for note in rep.notes:
        print(f"  NOTE: {note}")
    headers = ("group", "wallets", "w/ data", "positions", "top_wallet", "sel_roi", "hold_roi", "mean_pos_roi",
               "95% CI", "med_wallet", "%wallets+", "hold_clv", "hold_mk24")
    rows = []
    for g in rep.groups:
        ci = "-" if g.ci_low is None else f"[{g.ci_low:+.1%}, {g.ci_high:+.1%}]"
        rows.append([g.name, str(g.wallets), str(g.wallets_with_data), str(g.positions),
                     _f(g.top_wallet_share, ".0%"), _f(g.selection_roi, "+.1%"),
                     _f(g.holdout_roi, "+.1%"), _f(g.mean_position_roi, "+.1%"), ci, _f(g.median_wallet_roi, "+.1%"),
                     _f(g.share_wallets_positive, ".0%"), _f(g.holdout_clv, "+.3f"), _f(g.holdout_markout, "+.3f")])
    print(table(headers, rows))
    print("\n  Does the selection metric predict the holdout? Over wallets with >= 5 holdout positions:")
    print(f"    Spearman(selection {rep.rank_by}, holdout ROI) = {_f(rep.spearman_rank_metric, '+.2f')}"
          f"  (n={rep.rank_metric_n} wallets)")
    print(f"    Spearman(selection ROI, holdout ROI) = {_f(rep.spearman_selection_roi, '+.2f')}"
          f"  (n={rep.selection_roi_n} wallets)")
    print("    95% CIs resample whole wallets (a wallet's positions are not independent).")
    selected = [r for r in rep.rows if r.group == "selected"]
    if selected:
        print("\nSelected wallets, selection vs holdout:")
        print(table(("rank", "wallet", "sel_settled", "sel_roi", f"sel_{rep.rank_by}", "hold_n", "hold_pnl",
                     "hold_roi", "hold_win", "hold_clv"),
                    [[str(r.selection.rank), short(r.wallet), str(r.selection.settled_markets),
                      _f(r.selection.roi, "+.1%"), _f(r.selection.rank_value(rep.rank_by), "+.3f"), str(r.n),
                      _f(r.pnl, ",.0f"), _f(r.roi, "+.1%"), _f(r.win_rate, ".0%"), _f(r.clv, "+.3f")]
                     for r in sorted(selected, key=lambda r: r.selection.rank or 0)]))
    print(f"\nVerdict: {rep.verdict}")
    if rep.watch is not None and rep.watch.stages:
        print("\nWhere the time went:")
        print(rep.watch.table())


def _req_text(req: dict) -> str:
    from .optimize import LOOSEST
    parts = [f"{k}={'off' if v is None else v}" for k, v in req.items() if v != LOOSEST[k]]
    return ", ".join(parts) if parts else "no requirements beyond the eligibility rules"


def _score_cells(sc) -> list[str]:
    return [str(sc.wallets), str(sc.alerts), _f(sc.mean, "+.1%")]


def print_optimize(rep, *, holdout_months: int) -> None:
    from .optimize import GRID
    print(f"\nWalk-forward search for wallet requirements: {len(rep.folds)} folds, {holdout_months}-month holdouts, "
          f"{rep.configs_tried:,} requirement sets per search, objective '{rep.objective}'.")
    print("Outcome: ROI per copied alert (buy group >= min_whale_trade_usd inside the price band, at the whale's "
          "VWAP plus taker fee, held to resolution).")
    print("\nFolds (each with a pool built only from trades before its cutoff):")
    rows = []
    for f, base in zip(rep.folds, rep.baseline_per_fold, strict=True):
        with_alerts = sum(1 for w in f.wallets if w.rois)
        rows.append([iso(f.cutoff)[:10], iso(f.end)[:10], str(f.candidates), str(f.too_active), str(len(f.wallets)),
                     str(with_alerts), str(base.alerts), _f(base.mean, "+.1%")])
    print(table(("cutoff", "holdout to", "candidates", "too active", "measured", "w/ alerts", "alerts",
                 "ROI/alert (all)"), rows))
    for i, st in enumerate(rep.steps, start=1):
        train = ", ".join(iso(c)[:10] for c in st.train_cutoffs)
        print(f"\nStep {i}: choose on {train}, test on {iso(st.test_cutoff)[:10]} "
              f"({st.feasible:,} requirement sets feasible in training)")
        print(f"  chosen: {_req_text(st.chosen.req)}")
        tr = st.chosen.train
        print(f"  in training: {tr.wallets} wallets, {tr.alerts} alerts, mean {_f(tr.mean, '+.1%')} "
              f"(lower 95% bound {_f(tr.lcb, '+.1%')})")
        out = []
        for r in st.results:
            ci = "-" if r.ci[0] is None else f"[{r.ci[0]:+.1%}, {r.ci[1]:+.1%}]"
            out.append([r.name, *_score_cells(r.score), ci, _f(r.p_random, ".3f")])
        print(table(("on the unseen fold", "wallets", "alerts", "ROI/alert", "95% CI", "p vs random"), out))
        print(f"  transfer: Spearman(training lower bound, test ROI) across requirement sets = "
              f"{_f(st.transfer, '+.2f')}")
        if st.oracle is not None and st.oracle.test is not None:
            print(f"  hindsight: the best set chosen ON the test fold shows {_f(st.oracle.test.mean, '+.1%')} "
                  f"({st.oracle.test.wallets} wallets) - what an in-sample search would have claimed")
    for note in rep.skipped:
        print(f"\n  NOTE: {note}")
    if rep.final is not None:
        print(f"\nRecommendation (chosen on all folds): {_req_text(rep.final.req)}")
        print(f"  all folds pooled: {rep.final.train.wallets} wallet-folds, {rep.final.train.alerts} alerts, "
              f"mean {_f(rep.final.train.mean, '+.1%')} (lower 95% bound {_f(rep.final.train.lcb, '+.1%')})")
        rows = [[iso(f.cutoff)[:10], *_score_cells(a), *_score_cells(b), *_score_cells(c)]
                for f, a, b, c in zip(rep.folds, rep.final_per_fold, rep.spec_per_fold, rep.baseline_per_fold,
                                      strict=True)]
        print(table(("fold", "rec wallets", "rec alerts", "rec ROI", "spec wallets", "spec alerts", "spec ROI",
                     "all wallets", "all alerts", "all ROI"), rows))
        print("  (these per-fold numbers are in-sample for the recommendation; the steps above are the honest test)")
    print(f"\nVerdict: {rep.verdict}")
    print(f"\nSearched: {', '.join(f'{k} {v}' for k, v in GRID.items())}")
    print("\nWhere the time went:")
    print(rep.watch.table())


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    logging.Formatter.converter = __import__("time").gmtime
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def _score(args, cfg) -> int:
    as_of = parse_as_of(args.as_of) if args.as_of else start_of_day(now_ts())
    if args.max_candidates:
        cfg.candidates.max_candidates = args.max_candidates
    if args.rank_by:
        cfg.scoring.rank_by = args.rank_by
    async with open_app(cfg) as app:
        pool = None
        if args.wallet:
            pool = {w.lower(): {"cli"} for w in args.wallet}
        historical = True if args.historical_pool else (False if args.live_pool else None)
        run = await run_scoring(app, as_of, pool=pool, historical_pool=historical,
                                progress=lambda m: print(f"  .. {m}", file=sys.stderr))
        print_score(run, show=args.show, as_of=as_of)
    return 0


async def _validate(args, cfg) -> int:
    as_of = parse_as_of(args.as_of) if args.as_of else start_of_day(now_ts())
    if args.max_candidates:
        cfg.candidates.max_candidates = args.max_candidates
    if args.rank_by:
        cfg.scoring.rank_by = args.rank_by
    async with open_app(cfg) as app:
        rep = await run_validation(app, as_of, holdout_months=args.holdout_months,
                                   with_leaderboard=args.with_leaderboard,
                                   progress=lambda m: print(f"  .. {m}", file=sys.stderr))
        print_validation(rep)
    return 0


async def _optimize(args, cfg) -> int:
    from .optimize import run_optimize
    if args.max_candidates:
        cfg.candidates.max_candidates = args.max_candidates
    cutoffs = _cutoffs(args)
    if len(cutoffs) < 2:
        print("error: need at least two cutoffs (one to choose on, one to test on)", file=sys.stderr)
        return 2
    if args.from_dataset:
        from .optimize import SPEC_REQUIREMENTS, evaluate, load_folds
        from .timing import Stopwatch
        async with open_app(cfg) as app:
            rep = evaluate(load_folds(args.from_dataset), min_wallets=args.min_wallets, min_alerts=args.min_alerts,
                           spec=dict(SPEC_REQUIREMENTS), watch=Stopwatch(None), objective=args.objective)
        print_optimize(rep, holdout_months=args.holdout_months)
        return 0
    async with open_app(cfg) as app:
        rep = await run_optimize(app, cutoffs, holdout_months=args.holdout_months, clv_sample=args.clv_sample,
                                 min_wallets=args.min_wallets, min_alerts=args.min_alerts,
                                 dataset_path=args.dataset, objective=args.objective,
                                 progress=lambda m: print(f"  .. {m}", file=sys.stderr))
        print_optimize(rep, holdout_months=args.holdout_months)
        if args.dataset:
            print(f"\nPer-wallet dataset written to {args.dataset} (re-run the search on it with --from-dataset)")
    return 0


def _cutoffs(args) -> list[int]:
    from .timeutil import add_months
    if args.cutoffs:
        return sorted(parse_as_of(c.strip()) for c in args.cutoffs.split(",") if c.strip())
    end = start_of_day(now_ts())
    return [add_months(end, -args.holdout_months * k) for k in range(args.folds, 0, -1)]


async def _backtest(args, cfg) -> int:
    from .backtest import describe, run_backtest
    from .optimize import build_folds, load_folds, save_folds
    from .stats import render
    from .timing import Stopwatch
    if args.max_candidates:
        cfg.candidates.max_candidates = args.max_candidates

    def say(m: str) -> None:
        print(f"  .. {m}", file=sys.stderr)

    async with open_app(cfg) as app:
        watch = Stopwatch(app.http)
        if args.from_dataset:
            folds = load_folds(args.from_dataset)
        else:
            cutoffs = _cutoffs(args)
            if len(cutoffs) < 2:
                print("error: need at least two periods (the walk-forward rule is chosen on earlier ones)",
                      file=sys.stderr)
                return 2
            folds = await build_folds(app, cutoffs, holdout_months=args.holdout_months, clv_sample=args.clv_sample,
                                      clv_min_settled=20, say=say, watch=watch)
            if args.dataset:
                save_folds(folds, args.dataset)
                say(f"per-wallet dataset written to {args.dataset} (reuse it with --from-dataset)")
        report = await run_backtest(app, folds, delay=args.delay, ask_premium=args.ask_premium,
                                    min_wallets=args.min_wallets, min_alerts=args.min_alerts,
                                    objective=args.objective, kalshi=args.kalshi, progress=say, watch=watch)
        print(describe(report))
        print("\nWhere the time went:\n" + watch.table())
        print("\n" + render(app, with_breakdowns=args.breakdowns))
    return 0


async def _audit(args, cfg) -> int:
    from .audit import monitored_wallets, render, run_audit
    async with open_app(cfg) as app:
        rep = run_audit(app, n=args.trades, pick="random" if args.random else "gap", ids=args.id)
        print(render(rep, monitored_wallets(app)))
    return 0


async def _fresh(args, cfg) -> int:
    from .fresh import describe, run_fresh_wallet_test
    from .stats import render
    start = parse_as_of(args.start)
    end = parse_as_of(args.end) if args.end else start_of_day(now_ts())
    async with open_app(cfg) as app:
        rep = await run_fresh_wallet_test(app, start, end, max_age_days=args.max_age_days, min_usd=args.min_usd,
                                          detect_usd=args.detect_usd, delay=args.delay, write=not args.no_write,
                                          progress=lambda m: print(f"  .. {m}", file=sys.stderr))
        print(describe(rep, samples=cfg.stats.bootstrap_samples))
        if not args.no_write:
            print("\n" + render(app, with_breakdowns=False))
    return 0


async def _watchlist(args, cfg) -> int:
    from . import watchlist as wl
    async with open_app(cfg) as app:
        if wl.seed_from_monitored(app):
            wl.sync_monitored(app, None)
        if args.review:
            review = await wl.nightly(app, as_of=parse_as_of(args.as_of) if args.as_of else None,
                                      progress=lambda m: print(f"  .. {m}", file=sys.stderr))
            print(wl.render_review(review.run_id, review.as_of, review.before, review.after, review.max_size,
                                   review.decisions))
            print()
        if args.history:
            print(wl.render_history(app))
        elif args.last:
            print(wl.render_last_review(app))
        else:
            print(wl.render_current(app))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m whalewatcher",
                                description="Find skilled Polymarket wallets and paper trade their entries. "
                                            "Research only: never places orders.")
    p.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("score", help="score wallets using data up to --as-of and store a snapshot")
    s.add_argument("--as-of", help="YYYY-MM-DD (UTC midnight) or ISO time; default: today 00:00 UTC")
    s.add_argument("--wallet", action="append", help="score only this wallet (repeatable)")
    s.add_argument("--max-candidates", type=int)
    s.add_argument("--rank-by", choices=config_mod.RANK_KEYS)
    s.add_argument("--show", type=int, default=25, help="rows to print")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--historical-pool", action="store_true",
                   help="build candidates from large trades before as_of (leak-free; default for past dates)")
    g.add_argument("--live-pool", action="store_true", help="use today's leaderboards and recent large trades")

    v = sub.add_parser("validate", help="select on data before a cutoff, measure the holdout after it")
    v.add_argument("--as-of", help="end of the holdout; default: today 00:00 UTC")
    v.add_argument("--holdout-months", type=int, default=2)
    v.add_argument("--max-candidates", type=int)
    v.add_argument("--rank-by", choices=config_mod.RANK_KEYS)
    v.add_argument("--with-leaderboard", action="store_true",
                   help="also use today's leaderboards (leaks the holdout; for comparison only)")

    o = sub.add_parser("optimize", help="walk-forward search for the wallet requirements worth tracking")
    o.add_argument("--cutoffs", help="comma-separated YYYY-MM-DD cutoffs (default: --folds back-to-back "
                                     "holdouts ending today)")
    o.add_argument("--folds", type=int, default=3)
    o.add_argument("--holdout-months", type=int, default=2)
    o.add_argument("--max-candidates", type=int)
    o.add_argument("--clv-sample", type=int, default=50, help="positions per wallet for CLV (0 = all)")
    o.add_argument("--min-wallets", type=int, default=10, help="wallets with alerts a set must keep in each fold")
    o.add_argument("--min-alerts", type=int, default=50, help="alerts a set must keep in each fold")
    o.add_argument("--dataset", default="exports/optimize-folds.json", help="where to write the per-wallet data")
    o.add_argument("--from-dataset", help="re-run the search on a saved dataset, without fetching anything")
    o.add_argument("--objective", choices=("pooled", "worst"), default="pooled",
                   help="pooled: best lower bound over all folds together; worst: best lower bound in the worst fold")

    bt = sub.add_parser("backtest", help="replay past periods as simulated paper trades (shown by `stats`)")
    bt.add_argument("--cutoffs", help="comma-separated YYYY-MM-DD period starts (default: --folds back-to-back "
                                      "periods ending today)")
    bt.add_argument("--folds", type=int, default=3)
    bt.add_argument("--holdout-months", type=int, default=2, help="length of each period")
    bt.add_argument("--max-candidates", type=int)
    bt.add_argument("--clv-sample", type=int, default=50, help="positions per wallet for CLV (0 = all)")
    bt.add_argument("--dataset", default="exports/optimize-folds.json", help="where to write the per-wallet data")
    bt.add_argument("--from-dataset", help="use a dataset written by optimize or backtest instead of rebuilding it")
    bt.add_argument("--delay", type=int, default=90, help="seconds from the triggering fill to the copy's fill")
    bt.add_argument("--ask-premium", type=float, default=0.01, help="added to the minute price for the spread")
    bt.add_argument("--min-wallets", type=int, default=10)
    bt.add_argument("--min-alerts", type=int, default=50)
    bt.add_argument("--objective", choices=("pooled", "worst"), default="pooled")
    bt.add_argument("--breakdowns", action="store_true", help="also print the per-month/category/wallet tables")
    bt.add_argument("--kalshi", action="store_true",
                    help="also copy each alert on a game Kalshi lists, at Kalshi's own ask (public data, no key)")

    au = sub.add_parser("audit", help="hand-check settled paper trades and their shadow fills (read-only)")
    au.add_argument("--trades", type=int, default=5, help="how many trades to show in full")
    au.add_argument("--random", action="store_true", help="show a seeded random sample instead of the trades "
                                                            "that make the zero-latency vs alert-time gap")
    au.add_argument("--id", type=int, action="append", help="show this paper trade id (repeatable)")

    fr = sub.add_parser("fresh-wallets", help="test copying large buys by brand-new wallets in non-sports markets")
    fr.add_argument("--start", default="2026-05-07", help="markets resolved from this date (UTC)")
    fr.add_argument("--end", help="...until this date; default: today 00:00 UTC")
    fr.add_argument("--max-age-days", type=int, default=14, help="a wallet is fresh this long after its first trade")
    fr.add_argument("--min-usd", type=float, default=1000.0, help="buy group size that counts as a signal")
    fr.add_argument("--detect-usd", type=float, default=500.0, help="smallest fill read when looking for wallets")
    fr.add_argument("--delay", type=int, default=90)
    fr.add_argument("--no-write", action="store_true", help="print only; keep the overview as it is")

    w = sub.add_parser("watchlist", help="show the wallets being watched; --history shows every add and removal")
    w.add_argument("--history", action="store_true", help="every stay on the list, with the reasons")
    w.add_argument("--last", action="store_true", help="what the most recent nightly review decided, and why")
    w.add_argument("--review", action="store_true",
                   help="run the nightly review now (score, add, keep, remove), then show the result")
    w.add_argument("--as-of", help="with --review: use data up to this date (default: today 00:00 UTC)")

    d = sub.add_parser("detector", help="watch trades and publish tracked wallets' fills (its own container)")
    d.add_argument("--cycles", type=int, help="stop after this many feed polls")

    for name, helptext in (("monitor", "run the `single` runner: alerts + the paper bot (+ the detector, unless "
                                       "--no-detector)"),
                           ("settle", "settle open paper trades once"),
                           ("stats", "print paper-trading statistics"),
                           ("export", "write all paper trades to CSV"),
                           ("health", "exit 0 if the monitor loop has run recently (Docker healthcheck)")):
        sp = sub.add_parser(name, help=helptext)
        if name == "monitor":
            sp.add_argument("--once", action="store_true", help="one poll cycle, then exit")
            sp.add_argument("--cycles", type=int, help="stop after this many poll cycles")
            sp.add_argument("--no-post", action="store_true", help="never post to Discord (dry run)")
            sp.add_argument("--no-detector", action="store_true",
                            help="do not detect in this process: a separate `detector` process publishes the fills")
        if name == "health":
            sp.add_argument("--detector", action="store_true", help="check the detector's heartbeat instead")
        if name == "stats":
            sp.add_argument("--post", action="store_true", help="also post the summary to the stats webhook")
        if name == "export":
            sp.add_argument("--csv", default="exports/paper_trades.csv")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    _setup_logging()
    args = build_parser().parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "as_of", None):
        try:
            parse_as_of(args.as_of)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "score":
        return asyncio.run(_score(args, cfg))
    if args.command == "validate":
        return asyncio.run(_validate(args, cfg))
    if args.command == "optimize":
        return asyncio.run(_optimize(args, cfg))
    if args.command == "backtest":
        return asyncio.run(_backtest(args, cfg))
    if args.command == "fresh-wallets":
        return asyncio.run(_fresh(args, cfg))
    if args.command == "audit":
        return asyncio.run(_audit(args, cfg))
    if args.command == "watchlist":
        return asyncio.run(_watchlist(args, cfg))
    from . import service  # the live loop and stats, imported lazily
    return asyncio.run(service.run_command(args, cfg))
