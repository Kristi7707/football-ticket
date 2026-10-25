"""Back-test the daily model against real results.

Usage:  python tools/backtest.py HISTORY_DIR [--tickets]

HISTORY_DIR holds the CODE_SEASON.json files written by tools/fetch_history.py (run the
"Fetch match history" workflow, or the script itself, and unzip the result).

Walk-forward: every match is predicted only from results played BEFORE it, with the app's
own functions (main.team_strength, season_prior, scoreline_markets, calibrate), then scored
against what really happened. Prints, per market the app bets on:
  naive   always guess the league's average rate for that market
  plain   the previous release (this season's table only, independent Poisson, no calibration)
  new     last season's table as the starting point + draw correction, uncalibrated
  calib   new + calibration fitted on the OTHER league-seasons (leave-one-out), i.e. what the
          app does, scored honestly
Numbers are log loss (lower is better). "gain" is naive - calib: above 0 means the model
knows something the average rate does not.

--tickets also replays the app's real ticket builder over every past match day.
"""

import importlib.util
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("app", ROOT / "main.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

DOMESTIC = ["PL", "ELC", "BL1", "SA", "PD", "FL1", "DED", "PPL", "BSA"]
LABELS = [label for _, label in app.MARKETS]


def load_history(directory):
    """{(code, season): finished regular-season matches sorted by date}"""
    out = {}
    for p in sorted(Path(directory).glob("*_*.json")):
        code, season = p.stem.split("_")
        if code not in DOMESTIC:
            continue
        import json
        ms = [m for m in json.loads(p.read_text(encoding="utf-8"))
              if m["status"] == "FINISHED" and m["hg"] is not None and m["ag"] is not None
              and m["home"]["id"] and m["away"]["id"]
              and m.get("stage") in (None, "REGULAR_SEASON")]
        ms.sort(key=lambda m: m["date"])
        for m in ms:
            m["_t"] = datetime.strptime(m["date"][:19], "%Y-%m-%dT%H:%M:%S")
        if ms:
            out[(code, int(season))] = ms
    return out


def truth(m):
    hg, ag = m["hg"], m["ag"]
    t = hg + ag
    h, d, a = hg > ag, hg == ag, hg < ag
    return {
        "1 (Home win)": h, "X (Draw)": d, "2 (Away win)": a,
        "1X (Home or Draw)": h or d, "X2 (Draw or Away)": d or a, "12 (Home or Away)": h or a,
        "Over 1.5 goals": t >= 2, "Over 2.5 goals": t >= 3, "Under 2.5 goals": t < 3,
        "Over 3.5 goals": t >= 4, "Under 3.5 goals": t < 4, "Under 4.5 goals": t < 5,
        "Both teams to score: Yes": hg > 0 and ag > 0,
        "Both teams to score: No": not (hg > 0 and ag > 0),
    }


def final_table(ms):
    rows = {}
    for m in ms:
        add_result(rows, m)
    return rows


def add_result(rows, m):
    for side, gf, ga in (("home", m["hg"], m["ag"]), ("away", m["ag"], m["hg"])):
        tid = m[side]["id"]
        r = rows.setdefault(tid, {"team": {"id": tid}, "playedGames": 0, "goalsFor": 0,
                                  "goalsAgainst": 0})
        r["playedGames"] += 1
        r["goalsFor"] += gf
        r["goalsAgainst"] += ga


def walk(history, seasons):
    """One record per rateable match: raw probabilities of the plain and the new model."""
    records = []
    for (code, season), ms in sorted(history.items()):
        if season not in seasons:
            continue
        prev = history.get((code, season - 1))
        prior = app.season_prior(final_table(prev)) if prev else None
        rows = {}
        for m in ms:
            h, a = rows.get(m["home"]["id"]), rows.get(m["away"]["id"])
            if h and a and h["playedGames"] >= app.MIN_GAMES and a["playedGames"] >= app.MIN_GAMES:
                avg = app.table_goal_rate(rows)
                hid, aid = m["home"]["id"], m["away"]["id"]
                plain = app.expected_goals(avg, app.team_strength(h, hid, avg),
                                           app.team_strength(a, aid, avg))
                new = app.expected_goals(avg, app.team_strength(h, hid, avg, prior),
                                         app.team_strength(a, aid, avg, prior))
                records.append({
                    "code": code, "season": season, "m": m, "truth": truth(m),
                    "plain": app.scoreline_markets(plain[0], plain[1], rho=0.0),
                    "new": app.scoreline_markets(new[0], new[1]),
                })
            add_result(rows, m)
    return records


def sig(x):
    return 1 / (1 + math.exp(-x))


def logit(p):
    return app._logit(p)


def fit_platt(pairs, iters=50):
    """Maximum-likelihood a, b for p' = sigmoid(a + b*logit(p)) (Newton's method)."""
    a, b = 0.0, 1.0
    xs = [(logit(p), 1.0 if h else 0.0) for p, h in pairs]
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in xs:
            q = sig(a + b * x)
            e, w = q - y, q * (1 - q) + 1e-9
            g0 += e; g1 += e * x
            h00 += w; h01 += w * x; h11 += w * x * x
        g1 += 1e-3 * (b - 1.0)
        h00 += 1e-6; h11 += 1e-3
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            break
        da, db = (h11 * g0 - h01 * g1) / det, (-h01 * g0 + h00 * g1) / det
        a -= da; b -= db
        if abs(da) + abs(db) < 1e-9:
            break
    return a, b


def ll(p, hit):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if hit else 1 - p)


def block_fits(records):
    """{(code, season): {label: (a, b)}} fitted on every OTHER league-season."""
    blocks = defaultdict(list)
    for r in records:
        blocks[(r["code"], r["season"])].append(r)
    fits = {}
    for key in blocks:
        fits[key] = {lab: fit_platt([(r["new"][lab], r["truth"][lab]) for k2, b2 in blocks.items()
                                     if k2 != key for r in b2]) for lab in LABELS}
    return fits


def report(records):
    fits = block_fits(records)
    for r in records:
        f = fits[(r["code"], r["season"])]
        r["calib"] = {lab: sig(f[lab][0] + f[lab][1] * logit(r["new"][lab])) for lab in LABELS}
    n = len(records)
    print("%d rateable matches (both teams 3+ games played), %s seasons\n" % (
        n, sorted({r["season"] for r in records})))
    print("%-26s %7s %7s %7s %7s %9s" % ("market", "naive", "plain", "new", "calib", "gain"))
    tot = defaultdict(float)
    for lab in LABELS:
        rate = sum(1 for r in records if r["truth"][lab]) / n
        row = {
            "naive": sum(ll(rate, r["truth"][lab]) for r in records) / n,
            "plain": sum(ll(r["plain"][lab], r["truth"][lab]) for r in records) / n,
            "new": sum(ll(r["new"][lab], r["truth"][lab]) for r in records) / n,
            "calib": sum(ll(r["calib"][lab], r["truth"][lab]) for r in records) / n,
        }
        for k, v in row.items():
            tot[k] += v / len(LABELS)
        print("%-26s %7.4f %7.4f %7.4f %7.4f %+9.4f" % (
            lab, row["naive"], row["plain"], row["new"], row["calib"], row["naive"] - row["calib"]))
    print("%-26s %7.4f %7.4f %7.4f %7.4f %+9.4f" % (
        "ALL (mean)", tot["naive"], tot["plain"], tot["new"], tot["calib"], tot["naive"] - tot["calib"]))

    refit = {lab: fit_platt([(r["new"][lab], r["truth"][lab]) for r in records]) for lab in LABELS}
    worst = max(max(abs(refit[l][0] - app.CALIBRATION[l][0]), abs(refit[l][1] - app.CALIBRATION[l][1]))
                for l in LABELS)
    print("\nCalibration in main.py vs a fresh fit on this data: largest difference %.3f" % worst)
    if worst > 0.15:
        print("  -> the table in main.py looks stale; paste these into CALIBRATION:")
        for lab in LABELS:
            print('    "%s": (%.3f, %.3f),' % (lab, *refit[lab]))
    return fits


# ---------------------------------------------------------------- ticket replay
class FakeDate(date):
    current = date(2026, 1, 1)

    @classmethod
    def today(cls):
        return cls.current


def tickets(records, hour_utc=11):
    """Replay app.select_picks on each past match day with the out-of-fold calibrated odds."""
    app.date = FakeDate
    days = defaultdict(list)
    for r in records:
        if r["m"]["_t"].hour >= hour_utc:  # the daily run is at 13:00 Albania time = 11:00 UTC
            days[r["m"]["_t"].date()].append(r)
    base = dict(min_picks=5, max_picks=7, target_odds_min=30, target_odds_max=50,
                min_pick_probability=0.42, min_odds_per_pick=1.3, include_corners=False,
                max_corners_picks=2, max_picks_per_market=3)
    scenarios = [
        ("5-7 picks, odds 30-50 (current)", {}),
        ("3 picks, odds 6-10", dict(min_picks=3, max_picks=3, target_odds_min=6, target_odds_max=10,
                                    min_pick_probability=0.55)),
        ("3 picks, odds 4-6", dict(min_picks=3, max_picks=3, target_odds_min=4, target_odds_max=6,
                                   min_pick_probability=0.60)),
        ("2 picks, odds 3-4", dict(min_picks=2, max_picks=2, target_odds_min=3, target_odds_max=4,
                                   min_pick_probability=0.60)),
        ("2 picks, odds 2-2.6", dict(min_picks=2, max_picks=2, target_odds_min=2.0,
                                     target_odds_max=2.6, min_pick_probability=0.65)),
        ("1 pick, odds 1.35-1.7", dict(min_picks=1, max_picks=1, target_odds_min=1.35,
                                       target_odds_max=1.7, min_pick_probability=0.60)),
    ]
    print("\nTicket replay on %d match days (kickoffs after %d:00 UTC), picks by the app's own builder"
          % (len(days), hour_utc))
    print("%-34s %8s %12s %16s" % ("ticket type", "tickets", "stated win%", "actual wins"))
    for name, over in scenarios:
        cfg = dict(base, **over)
        n = hits = 0
        stated = 0.0
        for day in sorted(days):
            ms = []
            for r in days[day]:
                p = r["calib"]
                res = [p["1 (Home win)"], p["X (Draw)"], p["2 (Away win)"]]
                ms.append({"home": r["m"]["home"]["name"], "away": r["m"]["away"]["name"],
                           "league": r["code"], "kickoff": r["m"]["date"][11:16],
                           "p_home": res[0] / sum(res), "p_draw": res[1] / sum(res),
                           "p_away": res[2] / sum(res), "lam_home": 0.0, "lam_away": 0.0,
                           "probs": p, "_truth": r["truth"]})
            if len(ms) < cfg["min_picks"]:
                continue
            FakeDate.current = day
            picks = app.select_picks(ms, cfg)
            if not picks:
                continue
            n += 1
            hits += all(c["match"]["_truth"][c["label"]] for c in picks)
            win = 1.0
            for c in picks:
                win *= c["prob"]
            stated += win
        if n:
            print("%-34s %8d %11.1f%% %9d (%4.1f%%)" % (name, n, 100 * stated / n, hits, 100 * hits / n),
                  flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    history = load_history(sys.argv[1])
    available = sorted({s for _, s in history})
    seasons = {s for s in available if s - 1 in available}  # seasons with a previous one for the prior
    recs = walk(history, seasons)
    report(recs)
    if "--tickets" in sys.argv:
        tickets(recs)
