"""Phase 1 validation: do wallets chosen on past data keep outperforming?

Selection is an ordinary score run at the cutoff (``as_of - holdout_months``)
using only data before it, on a candidate pool built only from trades before
it. The holdout measures each candidate on positions it OPENED during the
holdout and that resolved before ``as_of``, so nothing the selection could see
is counted again.

Comparison groups:
  selected        the top_k by ``rank_by`` among wallets that passed the filters
  passed_rest     passed the filters but ranked below top_k
  all_scored      every candidate with at least one holdout position: "average"
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .app import App
from .scoring import (
    DAY,
    WalletMetrics,
    analyse,
    candidate_pool,
    compute_clv,
    load_wallet_rows,
    run_scoring,
)
from .stats_util import bootstrap_mean_ci, median, spearman
from .timeutil import add_months

log = logging.getLogger(__name__)


@dataclass
class HoldoutRow:
    wallet: str
    group: str
    selection: WalletMetrics
    n: int = 0
    pnl: float = 0.0
    cost: float = 0.0
    roi: float | None = None
    win_rate: float | None = None
    clv: float | None = None
    clv_n: int = 0
    markout: float | None = None
    markout_n: int = 0
    market_rois: list[float] = field(default_factory=list)


@dataclass
class GroupSummary:
    name: str
    wallets: int
    wallets_with_data: int
    positions: int
    selection_roi: float | None
    holdout_roi: float | None          # sum pnl / sum cost
    mean_position_roi: float | None
    ci_low: float | None
    ci_high: float | None
    median_wallet_roi: float | None
    share_wallets_positive: float | None
    holdout_clv: float | None
    holdout_markout: float | None


@dataclass
class ValidationReport:
    as_of: int
    cutoff: int
    selection_run_id: int | None
    rank_by: str
    groups: list[GroupSummary]
    rows: list[HoldoutRow]
    spearman_rank_metric: float | None
    spearman_selection_roi: float | None
    correlation_n: int
    notes: list[str]
    verdict: str


async def run_validation(app: App, as_of: int, *, holdout_months: int = 2, with_leaderboard: bool = False,
                         min_holdout_positions: int = 5,
                         progress: Callable[[str], None] | None = None) -> ValidationReport:
    cfg = app.cfg.scoring
    say = progress or (lambda _m: None)
    cutoff = add_months(as_of, -holdout_months)
    notes = []
    pool, pool_notes = await candidate_pool(app, cutoff, historical=True)
    notes.extend(pool_notes)
    if with_leaderboard:
        live, _ = await candidate_pool(app, cutoff, historical=False)
        for w, tags in live.items():
            pool.setdefault(w, set()).update(tags)
        notes.append("LEAKAGE: --with-leaderboard adds wallets from today's leaderboards, which already "
                     "reflect the holdout period. Treat the result as optimistic.")
    say(f"selection at cutoff, {len(pool)} candidates")
    selection = await run_scoring(app, cutoff, kind="validate-selection", pool=pool, extra_notes=notes,
                                  progress=say)

    scored = [m for m in selection.metrics if not m.truncated]
    rows: list[HoldoutRow] = []
    sem = asyncio.Semaphore(4)
    fetch_start = add_months(cutoff, -cfg.lookback_months) - cfg.entry_lookback_days * DAY

    async def holdout(m: WalletMetrics) -> HoldoutRow:
        group = "selected" if m.selected else ("passed_rest" if m.passed else "rest")
        row = HoldoutRow(m.wallet, group, m)
        async with sem:
            wallet_rows, truncated = await load_wallet_rows(app, m.wallet, fetch_start, as_of,
                                                            max_rows=cfg.max_activity_rows)
        if truncated:
            return row
        markets = await app.markets.ensure({r.condition_id for r in wallet_rows})
        a = analyse(m.wallet, wallet_rows, markets, as_of, cfg, window_start=cutoff, entry_after=cutoff)
        row.n, row.pnl, row.cost = a.metrics.settled_markets, a.metrics.total_pnl, a.metrics.total_cost
        row.roi, row.win_rate = a.metrics.roi, a.metrics.win_rate
        row.market_rois = [s.pnl / s.cost for s in a.settled if s.cost > 0]
        if group in ("selected", "passed_rest") and a.clv_positions:
            async with sem:
                row.clv, row.clv_n, row.markout, row.markout_n = await compute_clv(
                    a.clv_positions, app.prices, as_of, cfg.markout_hours)
        return row

    say(f"measuring {len(scored)} wallets on the holdout")
    rows = list(await asyncio.gather(*(holdout(m) for m in scored)))

    groups = [
        _summarise("selected", [r for r in rows if r.group == "selected"], app),
        _summarise("passed_rest", [r for r in rows if r.group == "passed_rest"], app),
        _summarise("all_scored", rows, app),
    ]
    paired = [r for r in rows if r.n >= min_holdout_positions and r.roi is not None]
    rank_pairs = [(r.selection.rank_value(cfg.rank_by), r.roi) for r in paired
                  if r.selection.rank_value(cfg.rank_by) is not None]
    roi_pairs = [(r.selection.roi, r.roi) for r in paired if r.selection.roi is not None]
    rho_rank = spearman([a for a, _ in rank_pairs], [b for _, b in rank_pairs]) if rank_pairs else None
    rho_roi = spearman([a for a, _ in roi_pairs], [b for _, b in roi_pairs]) if roi_pairs else None
    report = ValidationReport(as_of, cutoff, selection.run_id, cfg.rank_by, groups, rows, rho_rank, rho_roi,
                              len(paired), selection.notes, "")
    report.verdict = verdict(report)
    return report


def _summarise(name: str, rows: list[HoldoutRow], app: App) -> GroupSummary:
    with_data = [r for r in rows if r.n > 0]
    position_rois = [x for r in with_data for x in r.market_rois]
    mean, lo, hi = bootstrap_mean_ci(position_rois, samples=min(app.cfg.stats.bootstrap_samples, 5000))
    pnl, cost = sum(r.pnl for r in with_data), sum(r.cost for r in with_data)
    sel_pnl = sum(r.selection.total_pnl for r in rows)
    sel_cost = sum(r.selection.total_cost for r in rows)
    clvs = [r.clv for r in with_data if r.clv is not None]
    marks = [r.markout for r in with_data if r.markout is not None]
    wallet_rois = [r.roi for r in with_data if r.roi is not None]
    return GroupSummary(
        name=name, wallets=len(rows), wallets_with_data=len(with_data), positions=len(position_rois),
        selection_roi=sel_pnl / sel_cost if sel_cost > 0 else None,
        holdout_roi=pnl / cost if cost > 0 else None, mean_position_roi=mean, ci_low=lo, ci_high=hi,
        median_wallet_roi=median(wallet_rois),
        share_wallets_positive=(sum(1 for x in wallet_rois if x > 0) / len(wallet_rois)) if wallet_rois else None,
        holdout_clv=sum(clvs) / len(clvs) if clvs else None,
        holdout_markout=sum(marks) / len(marks) if marks else None,
    )


def verdict(report: ValidationReport) -> str:
    sel = next(g for g in report.groups if g.name == "selected")
    base = next(g for g in report.groups if g.name == "all_scored")
    if sel.wallets == 0:
        return ("No wallet passed the filters at the cutoff, so there is nothing to validate. "
                "Loosen the filters or widen the candidate pool.")
    if sel.positions < 30:
        return (f"Not enough data to draw conclusions: the selected wallets opened and settled only "
                f"{sel.positions} positions in the holdout.")
    if sel.ci_low is not None and sel.ci_low > 0 and (base.mean_position_roi is None
                                                     or sel.mean_position_roi > base.mean_position_roi):
        return ("The selected wallets kept outperforming: their mean holdout ROI per position is above zero "
                "with 95% confidence and above the all-candidates average.")
    if sel.ci_low is not None and sel.ci_low <= 0 <= (sel.ci_high or 0):
        return ("The 95% confidence interval for the selected wallets' holdout ROI includes zero: no evidence "
                "the selection picked wallets that keep winning. Treat it as regression to average.")
    return "The selected wallets did not outperform in the holdout."
