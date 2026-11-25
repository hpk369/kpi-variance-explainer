"""Daily synthetic feed for Northlake Bank (fictional): one business day of activity per file.

Each day from dates.feed_start up to yesterday (America/Toronto) gets:
  feed/revenue_daily/<date>.parquet      recurring accruals + fees from deals that closed that day
  feed/pipeline_new/<date>.parquet       deals opened that day
  feed/pipeline_events/<date>.parquet    stage moves, wins and losses that day
  feed/coverage_daily/<date>.parquet     meetings and calls
  feed/gl_control_daily/<date>.parquet   GL control totals (match revenue: no break)
  feed/fact_capital/<YYYY-MM>.parquet    capital, expected loss and cost allocation for the month

Re-running a day gives the same rows (seeded by the date), so missed days can be backfilled at any time.
The two slipped Industrials mandates from story 1 close in November 2026.

Run locally:      python generator/daily_feed.py                   (writes generator/out/feed up to yesterday)
                  python generator/daily_feed.py --until 2026-11-22
In Databricks:    pipeline/00_daily_feed.py calls run(out_dir="/Volumes/cib_demo/bronze/raw")
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import ROOT, constants  # noqa: E402
from generator.generate import DEAL_FEE, DEAL_MIX, DEAL_RATE, STAGE_PROB, TEAM_WIN_RATE  # noqa: E402

STAGE_ORDER = {"Origination": 0, "Pitch": 1, "Mandate": 2, "Execution": 3}
TABLES = ["revenue_daily", "pipeline_new", "pipeline_events", "coverage_daily", "gl_control_daily"]

# Fixed parquet schemas. Without them, a weekend or quiet day writes untyped (null) columns that Spark cannot read.
_S, _D, _F, _I = pa.string(), pa.date32(), pa.float64(), pa.int64()
SCHEMAS = {
    "revenue_daily": pa.schema([("client_id", _S), ("product_id", _S), ("revenue_date", _D), ("revenue", _F), ("fees", _F), ("nii", _F)]),
    "pipeline_new": pa.schema([("deal_id", _S), ("deal_name", _S), ("client_id", _S), ("product_id", _S), ("banker_id", _S),
                               ("stage", _S), ("status", _S), ("probability", _F), ("expected_fee", _F), ("actual_fee", _F),
                               ("open_date", _D), ("pitch_date", _D), ("mandate_date", _D), ("execution_date", _D),
                               ("expected_close_date", _D), ("actual_close_date", _D)]),
    "pipeline_events": pa.schema([("deal_id", _S), ("event_date", _D), ("event_type", _S), ("new_stage", _S), ("actual_fee", _F)]),
    "coverage_daily": pa.schema([("banker_id", _S), ("client_id", _S), ("activity_date", _D), ("meetings", _I), ("calls", _I)]),
    "gl_control_daily": pa.schema([("product_id", _S), ("control_date", _D), ("control_total", _F), ("source_system", _S)]),
    "fact_capital": pa.schema([("client_id", _S), ("month_start", _D), ("allocated_capital", _F), ("expected_loss", _F), ("cost_allocation", _F)]),
}


def write_typed(df: pd.DataFrame, name: str, path: Path) -> None:
    """Write one feed file with its fixed schema (works for empty days too)."""
    schema = SCHEMAS[name]
    cols = {}
    for f in schema:
        s = df[f.name] if f.name in df else pd.Series([None] * len(df), dtype=object)
        if f.type == _D:
            s = pd.to_datetime(s).dt.date
        elif f.type == _F:
            s = pd.to_numeric(s, errors="coerce").astype("float64")
        elif f.type == _I:
            s = pd.to_numeric(s, errors="coerce").astype("int64")
        else:
            s = s.astype(object).where(s.notna(), None)
        cols[f.name] = pa.array(s.tolist(), type=f.type, from_pandas=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pydict(cols, schema=schema), path)


def yesterday_toronto() -> date:
    return (datetime.now(ZoneInfo("America/Toronto")) - timedelta(days=1)).date()


def _rng(seed: int, day: date, stream: int) -> np.random.Generator:
    return np.random.default_rng([seed, day.toordinal(), stream])


class Feed:
    def __init__(self, out_dir: Path | str, C: dict | None = None):
        self.C = C or constants()
        self.out = Path(out_dir)
        inner = self.out / "_internal"
        self.meta = pd.read_parquet(inner / "client_meta.parquet")
        self.recurring = pd.read_parquet(inner / "recurring_plan.parquet")
        self.capital = pd.read_parquet(inner / "capital_future.parquet")
        self.base_schedule = pd.read_parquet(inner / "pipeline_schedule.parquet")
        for col in ["pitch_date", "mandate_date", "execution_date", "expected_close_date", "fate_date"]:
            self.base_schedule[col] = pd.to_datetime(self.base_schedule[col]).dt.date
        self.e2_team = self.C["stories"]["capacity"]["team"]
        self.start = date.fromisoformat(self.C["dates"]["feed_start"])
        self._feed_deals: list[pd.DataFrame] = []
        self._generated_until: date | None = None

    # ---------- deals ----------
    def _new_deals(self, day: date) -> pd.DataFrame:
        cols = ["deal_id", "client_id", "product_id", "banker_id", "team", "expected_fee", "open_date", "pitch_date", "mandate_date",
                "execution_date", "expected_close_date", "fate_status", "fate_date", "fate_fee", "lost_at_stage"]
        if day.weekday() >= 5:
            return pd.DataFrame(columns=cols)
        rng = _rng(self.C["seed"], day, 1)
        pool = self.meta[~self.meta.story_lending_only].reset_index(drop=True)
        rates = pool.segment.map(DEAL_RATE).to_numpy() / 21.0
        n = rng.poisson(rates.sum())
        rows = []
        for i in range(n):
            c = pool.iloc[rng.choice(len(pool), p=rates / rates.sum())]
            mix = DEAL_MIX[c.segment]
            p = rng.choice(list(mix), p=list(mix.values()))
            fee_rng = DEAL_FEE[p][c.segment] if p == "P02" else DEAL_FEE[p]
            fee = round(rng.uniform(*fee_rng), -3)
            dur = int(rng.integers(90, 271))
            exp_close = day + timedelta(days=dur)
            win = rng.random() < (0.27 if c.team == self.e2_team else TEAM_WIN_RATE[c.team])
            if win:
                fate_d = exp_close + timedelta(days=int(rng.integers(25, 51))) if rng.random() < 0.08 else exp_close
                fate_fee, lost_at = round(fee * rng.normal(1.0, 0.05), -3), None
            else:
                fate_d = max(exp_close - timedelta(days=int(rng.integers(0, 21))), day + timedelta(days=10))
                fate_fee, lost_at = 0.0, rng.choice(["Pitch", "Mandate", "Execution"], p=[0.45, 0.35, 0.20])
            rows.append([f"D-{day:%y%m%d}-{i + 1:02d}", c.client_id, p, c.coverage_banker_id, c.team, fee, day,
                         day + timedelta(days=round(dur * 0.25)), day + timedelta(days=round(dur * 0.50)),
                         day + timedelta(days=round(dur * 0.75)), exp_close, "Won" if win else "Lost", fate_d, fate_fee, lost_at])
        return pd.DataFrame(rows, columns=cols)

    def _ensure_deals(self, until: date) -> None:
        day = self.start if self._generated_until is None else self._generated_until + timedelta(days=1)
        while day <= until:
            self._feed_deals.append(self._new_deals(day))
            day += timedelta(days=1)
        self._generated_until = max(until, self._generated_until or until)

    def schedule(self, until: date) -> pd.DataFrame:
        self._ensure_deals(until)
        feed = pd.concat([d for d in self._feed_deals if len(d)], ignore_index=True) if any(len(d) for d in self._feed_deals) else pd.DataFrame()
        return pd.concat([self.base_schedule, feed], ignore_index=True) if len(feed) else self.base_schedule

    # ---------- one day ----------
    def day_tables(self, day: date) -> dict[str, pd.DataFrame]:
        sched = self.schedule(day)
        new = self._feed_deals[(day - self.start).days]

        # pipeline events
        ev = []
        for r in sched.itertuples():
            if r.fate_date == day:
                ev.append((r.deal_id, day, r.fate_status.lower(), "Closed " + r.fate_status, r.fate_fee if r.fate_status == "Won" else None))
                continue
            if day >= r.fate_date:
                continue
            for stage, d_ in (("Pitch", r.pitch_date), ("Mandate", r.mandate_date), ("Execution", r.execution_date)):
                if d_ == day and (r.lost_at_stage is None or pd.isna(r.lost_at_stage) or STAGE_ORDER[stage] <= STAGE_ORDER[r.lost_at_stage]):
                    ev.append((r.deal_id, day, "stage", stage, None))
        events = pd.DataFrame(ev, columns=["deal_id", "event_date", "event_type", "new_stage", "actual_fee"])

        pipeline_new = pd.DataFrame({
            "deal_id": new.deal_id, "deal_name": new.product_id + " · " + new.client_id, "client_id": new.client_id,
            "product_id": new.product_id, "banker_id": new.banker_id, "stage": "Origination", "status": "Open",
            "probability": STAGE_PROB["Origination"], "expected_fee": new.expected_fee, "actual_fee": np.nan,
            "open_date": new.open_date, "pitch_date": None, "mandate_date": None, "execution_date": None,
            "expected_close_date": new.expected_close_date, "actual_close_date": None,
        })

        # revenue: recurring accruals + fees of deals won today
        m0 = pd.Timestamp(day.replace(day=1))
        days_in_month = (m0 + pd.offsets.MonthEnd(0)).day
        rec = self.recurring[self.recurring.month_start == m0]
        rng = _rng(self.C["seed"], day, 2)
        daily = rec.plan_revenue.to_numpy() / days_in_month * rng.normal(1.0, 0.04, len(rec))
        rev = pd.DataFrame({"client_id": rec.client_id.to_numpy(), "product_id": rec.product_id.to_numpy(), "recurring": daily, "deal_fees": 0.0})
        won = events[events.event_type == "won"].merge(sched[["deal_id", "client_id", "product_id"]], on="deal_id")
        if len(won):
            rev = pd.concat([rev, pd.DataFrame({"client_id": won.client_id, "product_id": won.product_id, "recurring": 0.0,
                                                "deal_fees": won.actual_fee.astype(float)})])
        rev = rev.groupby(["client_id", "product_id"], as_index=False)[["recurring", "deal_fees"]].sum()
        nii_share = rev.product_id.map({"P01": 0.90, "P05": 0.0, "P06": 0.45}).fillna(0.0)
        rev["nii"] = (rev.recurring * nii_share).round(2)
        rev["fees"] = (rev.recurring + rev.deal_fees - rev.nii).round(2)
        rev["revenue"] = (rev.fees + rev.nii).round(2)
        rev["revenue_date"] = day
        revenue_daily = rev[["client_id", "product_id", "revenue_date", "revenue", "fees", "nii"]]

        gl = revenue_daily.groupby("product_id", as_index=False).revenue.sum().rename(columns={"revenue": "control_total"})
        gl["control_date"], gl["source_system"] = day, "GL"

        # coverage: business days only
        cov = []
        if day.weekday() < 5:
            rng_c = _rng(self.C["seed"], day, 3)
            for banker, grp in self.meta.groupby("coverage_banker_id"):
                busy = grp.team.iloc[0] == self.e2_team
                for _ in range(rng_c.poisson(0.9 if busy else 1.0)):
                    client = grp.client_id.iloc[rng_c.integers(len(grp))]
                    meeting = rng_c.random() < 0.4
                    cov.append((banker, client, day, int(meeting), int(not meeting)))
        coverage = pd.DataFrame(cov, columns=["banker_id", "client_id", "activity_date", "meetings", "calls"])

        return {"revenue_daily": revenue_daily, "pipeline_new": pipeline_new, "pipeline_events": events,
                "coverage_daily": coverage, "gl_control_daily": gl[["product_id", "control_date", "control_total", "source_system"]]}

    def capital_month(self, day: date) -> pd.DataFrame:
        return self.capital[self.capital.month_start == pd.Timestamp(day.replace(day=1))].reset_index(drop=True)

    # ---------- write ----------
    def run(self, until: date | None = None, force: bool = False, verbose: bool = True) -> list[date]:
        until = until or yesterday_toronto()
        written = []
        day = self.start
        while day <= until:
            target = self.out / "feed" / "revenue_daily" / f"{day}.parquet"
            if force or not target.exists():
                tables = self.day_tables(day)
                for name, df in tables.items():
                    write_typed(df, name, self.out / "feed" / name / f"{day}.parquet")
                write_typed(self.capital_month(day), "fact_capital", self.out / "feed" / "fact_capital" / f"{day:%Y-%m}.parquet")
                written.append(day)
                if verbose:
                    t = tables
                    print(f"{day}  revenue ${t['revenue_daily'].revenue.sum() / 1e6:6.2f}M  new deals {len(t['pipeline_new']):2d}  "
                          f"events {len(t['pipeline_events']):2d} (won {int((t['pipeline_events'].event_type == 'won').sum())})  "
                          f"coverage {len(t['coverage_daily']):2d}")
            day += timedelta(days=1)
        if verbose:
            print(f"{len(written)} day(s) written up to {until}" if written else f"Nothing to do: feed already complete up to {until}")
        return written


def run(out_dir: str | Path = ROOT / "generator" / "out", until: date | None = None, force: bool = False) -> list[date]:
    return Feed(out_dir).run(until=until, force=force)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "generator" / "out"))
    ap.add_argument("--until", type=date.fromisoformat, default=None, help="last day to write (default: yesterday, Toronto time)")
    ap.add_argument("--force", action="store_true", help="rewrite days that already exist")
    a = ap.parse_args()
    run(a.out, a.until, a.force)
