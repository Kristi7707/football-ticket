"""Daily Football Ticket - analyzes today's fixtures and builds a short ticket (3 picks by default).

Markets used: 1X2, Double Chance, Over/Under goals, BTTS, Corners.
Handicap markets are never used.

Usage:
    python main.py            # full run: fetch, analyze, save, email
    python main.py --dry-run  # analyze and save, but do not send email
    python main.py --demo     # run with built-in sample data (no API key needed)
"""

import json
import math
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

BASE_URL = "https://v3.football.api-sports.io"
SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------- config

def load_config():
    path = SCRIPT_DIR / "config.json"
    if not path.exists():
        # e.g. in GitHub Actions, where the real config is not committed
        path = SCRIPT_DIR / "config.example.json"
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    # Environment variables (GitHub Actions secrets) override the file
    if os.environ.get("API_FOOTBALL_KEY"):
        cfg["api_key"] = os.environ["API_FOOTBALL_KEY"]
    if os.environ.get("FOOTBALL_DATA_TOKEN"):
        cfg["football_data_token"] = os.environ["FOOTBALL_DATA_TOKEN"]
    if os.environ.get("EMAIL_USERNAME"):
        cfg["email"]["username"] = os.environ["EMAIL_USERNAME"]
        cfg["email"]["from"] = os.environ["EMAIL_USERNAME"]
    if os.environ.get("EMAIL_TO"):
        cfg["email"]["to"] = os.environ["EMAIL_TO"]
    if os.environ.get("EMAIL_PASSWORD"):
        cfg["email"]["password"] = os.environ["EMAIL_PASSWORD"]
        cfg["email"]["enabled"] = True
    return cfg


# ---------------------------------------------------------------- API

def api_get(cfg, path, params):
    query = urllib.parse.urlencode(params)
    url = "{}/{}?{}".format(BASE_URL, path, query)
    req = urllib.request.Request(url, headers={"x-apisports-key": cfg["api_key"]})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("errors"):
        # API returns errors as dict or list; surface them clearly
        raise RuntimeError("API error: {}".format(data["errors"]))
    return data


def to_float(value, fallback):
    try:
        result = float(value)
        if result <= 0:
            return fallback
        return result
    except (TypeError, ValueError):
        return fallback


def parse_percent(value):
    try:
        return float(str(value).replace("%", "").strip()) / 100.0
    except (TypeError, ValueError):
        return None


def fetch_matches(cfg):
    """Fetch today's fixtures and per-fixture predictions from API-Football."""
    today = date.today().isoformat()
    print("Fetching fixtures for {} ...".format(today))
    data = api_get(cfg, "fixtures", {"date": today, "timezone": cfg["timezone"]})

    league_ids = set(cfg.get("league_ids") or [])
    fixtures = []
    for f in data.get("response", []):
        if f["fixture"]["status"]["short"] != "NS":
            continue  # only matches that have not started
        if league_ids and f["league"]["id"] not in league_ids:
            continue
        fixtures.append(f)

    limit = cfg.get("max_matches_analyzed", 30)
    fixtures = fixtures[:limit]
    print("Analyzing {} fixtures (free API plan is throttled to ~10 requests/min,"
          " so this can take a few minutes)...".format(len(fixtures)))

    matches = []
    for i, f in enumerate(fixtures, 1):
        fixture_id = f["fixture"]["id"]
        try:
            pred_data = api_get(cfg, "predictions", {"fixture": fixture_id})
        except (RuntimeError, urllib.error.URLError) as e:
            print("  [{}/{}] skipped fixture {}: {}".format(i, len(fixtures), fixture_id, e))
            time.sleep(6.5)
            continue

        resp = pred_data.get("response") or []
        if not resp:
            time.sleep(6.5)
            continue
        r0 = resp[0]
        percents = (r0.get("predictions") or {}).get("percent") or {}
        p_home = parse_percent(percents.get("home"))
        p_draw = parse_percent(percents.get("draw"))
        p_away = parse_percent(percents.get("away"))
        if p_home is None or p_draw is None or p_away is None:
            time.sleep(6.5)
            continue

        teams = r0.get("teams") or {}

        def goal_avg(side, direction):
            try:
                return to_float(
                    teams[side]["league"]["goals"][direction]["average"]["total"], 1.3)
            except (KeyError, TypeError):
                return 1.3

        lam_home = (goal_avg("home", "for") + goal_avg("away", "against")) / 2.0
        lam_away = (goal_avg("away", "for") + goal_avg("home", "against")) / 2.0

        kickoff = f["fixture"]["date"][11:16]
        matches.append({
            "home": f["teams"]["home"]["name"],
            "away": f["teams"]["away"]["name"],
            "league": "{} - {}".format(f["league"]["country"], f["league"]["name"]),
            "kickoff": kickoff,
            "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
            "lam_home": lam_home, "lam_away": lam_away,
        })
        print("  [{}/{}] {} vs {}".format(i, len(fixtures), f["teams"]["home"]["name"],
                                          f["teams"]["away"]["name"]))
        time.sleep(6.5)  # respect the 10 requests/minute free-plan limit

    return matches


# ---------------------------------------------------------------- football-data.org

FD_BASE = "https://api.football-data.org/v4"


def fd_get(cfg, path, params=None):
    url = FD_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Auth-Token": cfg["football_data_token"]})
    attempts = 4
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 429 = rate limit, 5xx = server trouble: both are worth retrying
            if e.code not in (429, 500, 502, 503, 504) or attempt == attempts:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == attempts:
                raise
        time.sleep(10 * attempt)


def outcome_probs(lam_home, lam_away, max_goals=10):
    """Home/draw/away probabilities from independent Poisson scorelines."""
    p_h = p_d = p_a = 0.0
    home = [math.exp(-lam_home) * lam_home ** i / math.factorial(i) for i in range(max_goals + 1)]
    away = [math.exp(-lam_away) * lam_away ** j / math.factorial(j) for j in range(max_goals + 1)]
    for i, ph in enumerate(home):
        for j, pa in enumerate(away):
            if i > j:
                p_h += ph * pa
            elif i == j:
                p_d += ph * pa
            else:
                p_a += ph * pa
    total = p_h + p_d + p_a
    return p_h / total, p_d / total, p_a / total


def local_kickoff(utc_iso, tz_name):
    """Return (local date, 'HH:MM') for a UTC timestamp like 2026-10-01T18:30:00Z."""
    dt = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ")
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        pass  # no timezone database available: fall back to UTC
    return dt.date(), dt.strftime("%H:%M")


# Continental cups. A cup table says little until both teams have played a few cup
# games, so cup fixtures are rated from each team's DOMESTIC league form when both clubs
# have one (this beat the cup-table method on real Champions League results), and from the
# cup table only as a cautious fallback. Europa League (EL) and Conference League (UCL)
# need a paid football-data plan; the free plan has only CL.
EURO_CODES = {"CL", "EL", "UCL"}
PAID_CODES = {"EL", "UCL"}
DEFAULT_COMPETITIONS = ["PL", "ELC", "BL1", "SA", "PD", "FL1", "DED", "PPL", "BSA",
                        "CL", "EL", "UCL"]
MIN_GAMES = 3  # games a team needs before its table row says anything about its strength

# Rating model. Chosen and checked against ~3,700 real matches (PL, ELC, BL1, SA, PD, FL1,
# DED, PPL, BSA; 2025 and 2026 seasons) with tools/backtest.py; see the README.
LEGACY_K = 5.0         # shrink weight (in games) when last season's table is unavailable
PRIOR_K = 20.0         # games' worth of trust in last season's rating
PRIOR_KEEP = 0.7       # share of last season's distance from average that carries over
NEW_TEAM = (0.9, 1.1)  # (attack, defence) assumed for a team with no row last season
DC_RHO = -0.10         # Dixon-Coles: 0-0 and 1-1 happen more often than plain Poisson says
EURO_TRUST = 0.75      # pull cup fixtures rated from domestic form toward the average rate
CUP_ONLY_TRUST = 0.5   # same, for the weaker cup-table fallback

# Calibration: p' = sigmoid(a + b * logit(p)), fitted on the same matches. b < 1 means the
# raw model was overconfident (all goals markets); b ~ 1 means it was about right (results).
CALIBRATION = {
    "1 (Home win)": (-0.012, 1.086),
    "X (Draw)": (-0.362, 0.648),
    "2 (Away win)": (0.153, 1.144),
    "1X (Home or Draw)": (-0.153, 1.144),
    "X2 (Draw or Away)": (0.012, 1.086),
    "12 (Home or Away)": (0.362, 0.648),
    "Over 1.5 goals": (0.557, 0.553),
    "Over 2.5 goals": (0.106, 0.543),
    "Under 2.5 goals": (-0.106, 0.543),
    "Over 3.5 goals": (-0.304, 0.647),
    "Under 3.5 goals": (0.304, 0.647),
    "Under 4.5 goals": (0.649, 0.684),
    "Both teams to score: Yes": (0.132, 0.583),
    "Both teams to score: No": (-0.132, 0.583),
}
# Average rate of each market in that sample (what cup fixtures are pulled toward).
BASE_RATE = {
    "1 (Home win)": 0.4411,
    "X (Draw)": 0.2619,
    "2 (Away win)": 0.2970,
    "1X (Home or Draw)": 0.7030,
    "X2 (Draw or Away)": 0.5589,
    "12 (Home or Away)": 0.7381,
    "Over 1.5 goals": 0.7724,
    "Over 2.5 goals": 0.5364,
    "Under 2.5 goals": 0.4636,
    "Over 3.5 goals": 0.3002,
    "Under 3.5 goals": 0.6998,
    "Under 4.5 goals": 0.8610,
    "Both teams to score: Yes": 0.5536,
    "Both teams to score: No": 0.4464,
}


def table_goal_rate(rows):
    """Average goals scored per team per game in a league table."""
    games = sum(r["playedGames"] for r in rows.values())
    goals = sum(r["goalsFor"] for r in rows.values())
    rate = goals / games if games else 0.0
    return rate if rate > 0 else 1.3


def season_prior(rows):
    """{team id: (attack, defence)} from a finished season's table, relative to that
    league's average (1.0 = average)."""
    avg = table_goal_rate(rows)
    out = {}
    for team_id, r in rows.items():
        n = r["playedGames"]
        if n <= 0:
            continue
        w = n / (n + 5.0)
        out[team_id] = (w * ((r["goalsFor"] / n) / avg) + (1 - w),
                        w * ((r["goalsAgainst"] / n) / avg) + (1 - w))
    return out


def team_strength(row, team_id, league_avg, prior=None):
    """(attack, defence) relative to the league average; 1.0 is average. Pulled toward
    last season's rating when `prior` (from season_prior) is given, otherwise toward 1.0."""
    n = row["playedGames"]
    if prior is None:
        k, target = LEGACY_K, (1.0, 1.0)
    else:
        k = PRIOR_K
        p = prior.get(team_id)
        target = NEW_TEAM if p is None else (1.0 + PRIOR_KEEP * (p[0] - 1.0),
                                             1.0 + PRIOR_KEEP * (p[1] - 1.0))
    attack = (k * target[0] + n * ((row["goalsFor"] / n) / league_avg)) / (k + n)
    defence = (k * target[1] + n * ((row["goalsAgainst"] / n) / league_avg)) / (k + n)
    return attack, defence


def expected_goals(avg, home, away):
    """Poisson means for (home, away), with home advantage. `home` and `away` are
    (attack, defence) pairs."""
    return avg * 1.15 * home[0] * away[1], avg * 0.87 * away[0] * home[1]


def _poisson_pmf(lam, kmax):
    p = [math.exp(-lam)]
    for k in range(1, kmax + 1):
        p.append(p[-1] * lam / k)
    return p


def scoreline_markets(lam_home, lam_away, rho=DC_RHO, kmax=10):
    """Raw probability of every market the app bets on, from the scoreline distribution."""
    ph, pa = _poisson_pmf(lam_home, kmax), _poisson_pmf(lam_away, kmax)
    g = [[ph[i] * pa[j] for j in range(kmax + 1)] for i in range(kmax + 1)]
    if rho:
        g[0][0] *= max(0.0, 1 - lam_home * lam_away * rho)
        g[0][1] *= max(0.0, 1 + lam_home * rho)
        g[1][0] *= max(0.0, 1 + lam_away * rho)
        g[1][1] *= max(0.0, 1 - rho)
    total = sum(map(sum, g))
    home = draw = away = both = 0.0
    goals_ge = [0.0] * 6  # goals_ge[k] = P(total goals >= k)
    for i in range(kmax + 1):
        for j in range(kmax + 1):
            p = g[i][j] / total
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
            for k in range(1, 6):
                if i + j >= k:
                    goals_ge[k] += p
            if i > 0 and j > 0:
                both += p
    return {
        "1 (Home win)": home, "X (Draw)": draw, "2 (Away win)": away,
        "1X (Home or Draw)": home + draw, "X2 (Draw or Away)": draw + away,
        "12 (Home or Away)": home + away,
        "Over 1.5 goals": goals_ge[2], "Over 2.5 goals": goals_ge[3],
        "Under 2.5 goals": 1 - goals_ge[3], "Over 3.5 goals": goals_ge[4],
        "Under 3.5 goals": 1 - goals_ge[4], "Under 4.5 goals": 1 - goals_ge[5],
        "Both teams to score: Yes": both, "Both teams to score: No": 1 - both,
    }


def _logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def calibrate(label, p, trust=1.0):
    """Turn a raw model probability into one that matches how often such picks really
    won. trust < 1 additionally pulls it toward the market's average rate."""
    a, b = CALIBRATION[label]
    q = 1.0 / (1.0 + math.exp(-(a + b * _logit(p))))
    if trust != 1.0:
        base = _logit(BASE_RATE[label])
        q = 1.0 / (1.0 + math.exp(-(base + trust * (_logit(q) - base))))
    return q


def season_start_year(st, code, today):
    """First year of the season a standings response describes."""
    try:
        return int(st["season"]["startDate"][:4])
    except (KeyError, TypeError, ValueError):
        # BSA runs within a calendar year; the European leagues start in the summer
        return today.year if code == "BSA" or today.month >= 7 else today.year - 1


def load_standings(cfg, code, season=None):
    """(TOTAL standings as {team id: row}, season start year) of a competition, for the
    current season or the given one; None if unavailable."""
    time.sleep(6.5)  # free plan: 10 requests/minute
    try:
        st = fd_get(cfg, "/competitions/{}/standings".format(code),
                    {"season": season} if season else None)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print("  skipping {}{}: could not load standings ({})".format(
            code, " {}".format(season) if season else "", e))
        return None
    rows = {}
    for block in st.get("standings", []):
        if block.get("type") != "TOTAL":
            continue
        for row in block.get("table", []):
            rows[row["team"]["id"]] = row
    return rows, season_start_year(st, code, date.today())


def fetch_fixture_list(cfg, codes, today):
    """Raw fixtures for today and tomorrow in the given competitions.

    football-data.org answers HTTP 403 when the plan excludes a requested competition
    (EL and UCL on the free plan). Then ask again without those two and try each of them
    on its own, skipping the refused ones. Any other error - above all HTTP 400, which
    is what a bad or revoked token returns - is raised, so the run fails visibly instead
    of reporting an empty day."""
    params = {"dateFrom": today.isoformat(),
              "dateTo": (today + timedelta(days=1)).isoformat()}

    def ask(group):
        return fd_get(cfg, "/matches", dict(params, competitions=",".join(group))).get(
            "matches", [])

    try:
        return ask(codes)
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
        print("  fixtures request refused (HTTP 403): the plan excludes some competitions")
    core = [c for c in codes if c not in PAID_CODES]
    found = []
    if core:
        time.sleep(6.5)
        found = ask(core)  # refused again: a token/plan problem, not just EL/UCL - raise
    for code in codes:
        if code not in PAID_CODES:
            continue
        time.sleep(6.5)
        try:
            found += ask([code])
        except urllib.error.HTTPError as e:
            if e.code != 403:
                raise
            print("  skipping {}: not available on your football-data plan".format(code))
    return found


def fetch_matches_footballdata(cfg):
    """Today's fixtures from football-data.org, rated from league tables.

    Each team's strength is its goals scored/conceded per game versus its league's average,
    pulled toward last season's final rating (see team_strength), turned into scoreline
    probabilities and then calibrated against how often such picks really won
    (see calibrate). Domestic fixtures use their own league. Continental fixtures
    (CL/EL/UCL) use each club's domestic league when both clubs have one; strength is then
    measured against its own league's average, so the model does not know how strong one
    league is compared with another, and those probabilities are pulled toward the average.
    Otherwise they use the cup table, more cautiously. Anything else is skipped."""
    today = date.today()
    wanted = cfg.get("competitions", DEFAULT_COMPETITIONS)
    print("Fetching fixtures for {} ...".format(today.isoformat()))

    raw = fetch_fixture_list(cfg, wanted, today)
    raw.sort(key=lambda m: m.get("utcDate") or "")  # kickoff order, whatever the request mix
    fixtures = []
    for f in raw:
        if f.get("status") not in ("SCHEDULED", "TIMED"):
            continue
        local_date, kickoff = local_kickoff(f["utcDate"], cfg["timezone"])
        if local_date != today:
            continue
        fixtures.append((f, kickoff))
    print("Found {} fixtures today.".format(len(fixtures)))
    if fixtures:
        counts = {}
        for f, _ in fixtures:
            code = f["competition"]["code"]
            counts[code] = counts.get(code, 0) + 1
        print("  by competition: {}".format(
            ", ".join("{} {}".format(c, n) for c, n in sorted(counts.items()))))
    cap = cfg.get("max_matches_analyzed", 30)
    if len(fixtures) > cap:
        dropped = ", ".join("{} vs {}".format(
            f["homeTeam"].get("shortName") or f["homeTeam"].get("name") or "TBD",
            f["awayTeam"].get("shortName") or f["awayTeam"].get("name") or "TBD")
            for f, _ in fixtures[cap:])
        print("  analyzing only the first {} (max_matches_analyzed); not analyzed: {}".format(
            cap, dropped))
        fixtures = fixtures[:cap]

    # Standings: every competition playing today, plus every domestic league when a
    # continental fixture may need team strengths from home.
    playing = {f["competition"]["code"] for f, _ in fixtures}
    needed = set(playing)
    if playing & EURO_CODES:
        needed |= {c for c in wanted if c not in EURO_CODES}
    tables, years, failed = {}, {}, set()
    for code in sorted(needed):
        loaded = load_standings(cfg, code)
        if loaded is None:
            failed.add(code)
        else:
            tables[code], years[code] = loaded

    # Last season's final table of each domestic league: the starting point for ratings.
    priors = {}
    for code in sorted(c for c in tables if c not in EURO_CODES):
        loaded = load_standings(cfg, code, season=years[code] - 1)
        if loaded is None:
            print("  {}: no last-season table, using plain ratings".format(code))
        elif loaded[0]:
            priors[code] = season_prior(loaded[0])

    domestic = {}  # team id -> (table row, league goal rate, last-season prior or None)
    for code, rows in tables.items():
        if code in EURO_CODES:
            continue
        rate = table_goal_rate(rows)
        for team_id, row in rows.items():
            if row["playedGames"] >= MIN_GAMES:
                domestic.setdefault(team_id, (row, rate, priors.get(code)))

    matches = []
    for f, kickoff in fixtures:
        code = f["competition"]["code"]
        home_team, away_team = f["homeTeam"], f["awayTeam"]
        home_name = home_team.get("shortName") or home_team.get("name") or "TBD"
        away_name = away_team.get("shortName") or away_team.get("name") or "TBD"
        label = "{} vs {} [{}]".format(home_name, away_name, code)
        trust = 1.0

        if code in EURO_CODES:
            cup = tables.get(code, {})
            ch, ca = cup.get(home_team["id"]), cup.get(away_team["id"])
            dh, da = domestic.get(home_team["id"]), domestic.get(away_team["id"])
            if dh and da:
                avg = (dh[1] + da[1]) / 2.0
                home_s = team_strength(dh[0], home_team["id"], dh[1], dh[2])
                away_s = team_strength(da[0], away_team["id"], da[1], da[2])
                trust = EURO_TRUST
            elif (ch and ca and ch["playedGames"] >= MIN_GAMES
                  and ca["playedGames"] >= MIN_GAMES):
                avg = table_goal_rate(cup)
                home_s = team_strength(ch, home_team["id"], avg)
                away_s = team_strength(ca, away_team["id"], avg)
                trust = CUP_ONLY_TRUST
            else:
                missing = [n for n, x in ((home_name, dh), (away_name, da)) if not x]
                note = (" (standings failed to load for: {})".format(", ".join(sorted(failed)))
                        if failed else "")
                print("  skipped {}: no domestic league table with {}+ games for {}, "
                      "and the cup table is not ready{}".format(
                          label, MIN_GAMES, " and ".join(missing), note))
                continue
        else:
            rows = tables.get(code, {})
            h, a = rows.get(home_team["id"]), rows.get(away_team["id"])
            if not h or not a:
                print("  skipped {}: team not in the {} standings ({} teams loaded)".format(
                    label, code, len(rows)))
                continue
            if h["playedGames"] < MIN_GAMES or a["playedGames"] < MIN_GAMES:
                # not enough games to estimate team strength
                print("  skipped {}: fewer than {} games played ({} / {})".format(
                    label, MIN_GAMES, h["playedGames"], a["playedGames"]))
                continue
            avg = table_goal_rate(rows)
            home_s = team_strength(h, home_team["id"], avg, priors.get(code))
            away_s = team_strength(a, away_team["id"], avg, priors.get(code))

        lam_home, lam_away = expected_goals(avg, home_s, away_s)
        probs = {lab: calibrate(lab, p, trust)
                 for lab, p in scoreline_markets(lam_home, lam_away).items()}
        result = [probs["1 (Home win)"], probs["X (Draw)"], probs["2 (Away win)"]]

        matches.append({
            "home": home_name,
            "away": away_name,
            "league": f["competition"]["name"],
            "kickoff": kickoff,
            "p_home": result[0] / sum(result), "p_draw": result[1] / sum(result),
            "p_away": result[2] / sum(result),
            "lam_home": lam_home, "lam_away": lam_away,
            "probs": probs,
        })
        print("  {} vs {} [{}]".format(home_name, away_name, code))
    return matches


# ---------------------------------------------------------------- analysis

def poisson_cdf(lam, k):
    """P(X <= k) for Poisson with mean lam."""
    total = 0.0
    for i in range(k + 1):
        total += math.exp(-lam) * lam ** i / math.factorial(i)
    return total


def est_odds(prob):
    """Estimate realistic bookmaker odds from a probability (8% margin)."""
    return max(1.05, round(0.92 / prob, 2))


MARKETS = [  # (category, label): every market the app can bet on, apart from corners
    ("1X2", "1 (Home win)"), ("1X2", "X (Draw)"), ("1X2", "2 (Away win)"),
    ("Double Chance", "1X (Home or Draw)"), ("Double Chance", "X2 (Draw or Away)"),
    ("Double Chance", "12 (Home or Away)"),
    ("Goals", "Over 1.5 goals"), ("Goals", "Over 2.5 goals"), ("Goals", "Under 2.5 goals"),
    ("Goals", "Over 3.5 goals"), ("Goals", "Under 3.5 goals"), ("Goals", "Under 4.5 goals"),
    ("BTTS", "Both teams to score: Yes"), ("BTTS", "Both teams to score: No"),
]


def build_candidates(match, cfg):
    """All allowed-market picks for one match, each with an estimated probability."""
    cands = []
    p_h, p_d, p_a = match["p_home"], match["p_draw"], match["p_away"]
    total = p_h + p_d + p_a
    if total > 0:
        p_h, p_d, p_a = p_h / total, p_d / total, p_a / total

    def add(category, label, prob):
        if prob < 0.01:
            return  # zero-probability outcomes can't be priced
        prob = min(prob, 0.97)  # never claim near-certainty
        cands.append({
            "match": match, "category": category, "label": label,
            "prob": prob, "odds": est_odds(prob),
        })

    probs = match.get("probs")
    if probs:
        # Calibrated probabilities from the football-data model (see calibrate()).
        for category, label in MARKETS:
            add(category, label, probs[label])
    else:
        # 1X2
        add("1X2", "1 (Home win)", p_h)
        add("1X2", "X (Draw)", p_d)
        add("1X2", "2 (Away win)", p_a)

        # Double chance
        add("Double Chance", "1X (Home or Draw)", p_h + p_d)
        add("Double Chance", "X2 (Draw or Away)", p_d + p_a)
        add("Double Chance", "12 (Home or Away)", p_h + p_a)

        # Goals over/under (Poisson on expected total goals)
        lam = match["lam_home"] + match["lam_away"]
        add("Goals", "Over 1.5 goals", 1.0 - poisson_cdf(lam, 1))
        add("Goals", "Over 2.5 goals", 1.0 - poisson_cdf(lam, 2))
        add("Goals", "Under 2.5 goals", poisson_cdf(lam, 2))
        add("Goals", "Over 3.5 goals", 1.0 - poisson_cdf(lam, 3))
        add("Goals", "Under 3.5 goals", poisson_cdf(lam, 3))
        add("Goals", "Under 4.5 goals", poisson_cdf(lam, 4))

        # Both teams to score
        btts = (1.0 - math.exp(-match["lam_home"])) * (1.0 - math.exp(-match["lam_away"]))
        add("BTTS", "Both teams to score: Yes", btts)
        add("BTTS", "Both teams to score: No", 1.0 - btts)

    # Corners (league-average based prior; most matches clear 7.5 corners)
    if cfg.get("include_corners", True):
        add("Corners", "Over 7.5 total corners", 0.78)
        add("Corners", "Over 9.5 total corners", 0.52)
        add("Corners", "Under 11.5 total corners", 0.66)

    return cands


def combo_stats(combo):
    odds = 1.0
    prob = 1.0
    for c in combo:
        odds *= c["odds"]
        prob *= c["prob"]
    return odds, prob


def combo_valid(combo, cfg):
    """One pick per match is guaranteed by construction; enforce market caps."""
    caps = {"Corners": cfg.get("max_corners_picks", 2)}
    default_cap = cfg.get("max_picks_per_market", 3)
    counts = {}
    for c in combo:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
        if counts[c["category"]] > caps.get(c["category"], default_cap):
            return False
    return True


def combo_score(combo, cfg):
    """Higher is better. Inside the target odds range: maximize hit probability.
    Outside it: heavily penalized by how far away it is (in log space)."""
    odds, prob = combo_stats(combo)
    lo, hi = cfg["target_odds_min"], cfg["target_odds_max"]
    if odds < lo:
        gap = math.log(lo / odds)
    elif odds > hi:
        gap = math.log(odds / hi)
    else:
        gap = 0.0
    return math.log(prob) - 100.0 * gap


def select_picks(matches, cfg):
    """Find min_picks..max_picks picks (one per match, at most 7) whose combined odds land
    in the target range, maximizing the chance the whole ticket wins."""
    import random

    per_match = []
    for m in matches:
        cands = [c for c in build_candidates(m, cfg)
                 if c["prob"] >= cfg["min_pick_probability"]
                 and c["odds"] >= cfg["min_odds_per_pick"]]
        if cands:
            per_match.append(cands)

    min_picks = cfg.get("min_picks", 5)
    max_picks = min(cfg.get("max_picks", 7), 7)
    if len(per_match) < min_picks:
        return []

    rng = random.Random(date.today().toordinal())
    best, best_score = None, None

    for n in range(min_picks, min(max_picks, len(per_match)) + 1):
        for _ in range(150):
            chosen = rng.sample(range(len(per_match)), n)
            combo = [rng.choice(per_match[i]) for i in chosen]
            if not combo_valid(combo, cfg):
                continue
            # Hill-climb: swap one pick (same match, or a different unused match)
            improved = True
            while improved:
                improved = False
                cur = combo_score(combo, cfg)
                used = {id(c["match"]) for c in combo}
                for idx in range(len(combo)):
                    options = []
                    for cands in per_match:
                        mid = id(cands[0]["match"])
                        if mid in used and mid != id(combo[idx]["match"]):
                            continue
                        options.extend(cands)
                    for opt in options:
                        trial = combo[:idx] + [opt] + combo[idx + 1:]
                        if not combo_valid(trial, cfg):
                            continue
                        s = combo_score(trial, cfg)
                        if s > cur + 1e-9:
                            combo, cur, improved = trial, s, True
                            used = {id(c["match"]) for c in combo}
            s = combo_score(combo, cfg)
            if best_score is None or s > best_score:
                best, best_score = combo, s

    if best is None:
        return []
    best.sort(key=lambda c: c["match"]["kickoff"])
    return best


# ---------------------------------------------------------------- output

def ticket_totals(picks, cfg):
    stake = max(10, cfg.get("stake", 10))
    combined_odds = 1.0
    combined_prob = 1.0
    for p in picks:
        combined_odds *= p["odds"]
        combined_prob *= p["prob"]
    return stake, round(combined_odds, 2), combined_prob


def build_html(picks, cfg):
    stake, combined_odds, combined_prob = ticket_totals(picks, cfg)
    today = date.today().strftime("%d %B %Y")
    rows = ""
    for i, p in enumerate(picks, 1):
        m = p["match"]
        rows += (
            "<tr>"
            "<td style='padding:8px;border-bottom:1px solid #ddd'>{}</td>"
            "<td style='padding:8px;border-bottom:1px solid #ddd'><b>{} vs {}</b>"
            "<br><span style='color:#666;font-size:12px'>{} &middot; {}</span></td>"
            "<td style='padding:8px;border-bottom:1px solid #ddd'>{}</td>"
            "<td style='padding:8px;border-bottom:1px solid #ddd;text-align:center'>{:.0f}%</td>"
            "<td style='padding:8px;border-bottom:1px solid #ddd;text-align:center'>{:.2f}</td>"
            "</tr>"
        ).format(i, m["home"], m["away"], m["league"], m["kickoff"],
                 p["label"], p["prob"] * 100, p["odds"])

    return """
<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:auto">
  <h2 style="color:#1a5c2e">Daily Football Ticket - {today}</h2>
  <table style="border-collapse:collapse;width:100%;font-size:14px">
    <tr style="background:#1a5c2e;color:#fff">
      <th style="padding:8px;text-align:left">#</th>
      <th style="padding:8px;text-align:left">Match</th>
      <th style="padding:8px;text-align:left">Pick</th>
      <th style="padding:8px">Confidence</th>
      <th style="padding:8px">Est. odds</th>
    </tr>
    {rows}
  </table>
  <table style="margin-top:14px;font-size:14px">
    <tr><td style="padding:4px 12px 4px 0"><b>Stake:</b></td><td>{stake:.2f}</td></tr>
    <tr><td style="padding:4px 12px 4px 0"><b>Combined odds:</b></td><td>{odds:.2f}</td></tr>
    <tr><td style="padding:4px 12px 4px 0"><b>Potential return:</b></td><td>{ret:.2f}</td></tr>
    <tr><td style="padding:4px 12px 4px 0"><b>Ticket hit probability:</b></td><td>~{prob:.0f}%</td></tr>
  </table>
  <p style="color:#888;font-size:12px;margin-top:16px">
    Odds are estimates - check your bookmaker for exact values. No handicap markets are used.<br>
    Predictions are statistical estimates, not guarantees. Even strong picks lose.
    Only bet what you can afford to lose. 18+ | begambleaware.org
  </p>
</div>""".format(today=today, rows=rows, stake=float(stake), odds=combined_odds,
                 ret=stake * combined_odds, prob=combined_prob * 100)


def print_summary(picks, cfg):
    stake, combined_odds, combined_prob = ticket_totals(picks, cfg)
    print("")
    print("=" * 74)
    print("DAILY FOOTBALL TICKET - {}".format(date.today().strftime("%d %B %Y")))
    print("=" * 74)
    for i, p in enumerate(picks, 1):
        m = p["match"]
        print("{}. {} vs {}  [{}, {}]".format(i, m["home"], m["away"], m["league"], m["kickoff"]))
        print("   Pick: {}  |  confidence {:.0f}%  |  est. odds {:.2f}".format(
            p["label"], p["prob"] * 100, p["odds"]))
    print("-" * 74)
    print("Stake: {:.2f}   Combined odds: {:.2f}   Potential return: {:.2f}".format(
        stake, combined_odds, stake * combined_odds))
    print("Ticket hit probability: ~{:.0f}%".format(combined_prob * 100))
    print("=" * 74)


def send_email(cfg, html, subject=None):
    email_cfg = cfg["email"]
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject or "Daily Football Ticket - {}".format(
        date.today().strftime("%d %B %Y"))
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=30) as server:
        server.starttls()
        # Google shows app passwords with spaces; they must be removed for login
        server.login(email_cfg["username"], email_cfg["password"].replace(" ", ""))
        server.sendmail(email_cfg["from"], [email_cfg["to"]], msg.as_string())
    print("Email sent to {}".format(email_cfg["to"]))


def build_no_ticket_html(reason):
    return """
<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:auto">
  <h2 style="color:#1a5c2e">Daily Football Ticket - {today}</h2>
  <p>No ticket today.</p>
  <p style="color:#666">{reason}</p>
</div>""".format(today=date.today().strftime("%d %B %Y"), reason=reason)


def exit_without_ticket(cfg, reason, send):
    """End a run that produced no ticket. The run still counts as a success, so
    when emailing is on, tell the user why instead of leaving them guessing."""
    print(reason)
    if send and cfg["email"].get("enabled"):
        subject = "Daily Football Ticket - {} - no ticket today".format(
            date.today().strftime("%d %B %Y"))
        try:
            send_email(cfg, build_no_ticket_html(reason), subject)
        except Exception as e:
            print("Email failed: {}".format(e))
    sys.exit(0)


# ---------------------------------------------------------------- demo data

def demo_matches():
    """Sample fixtures so the app can be tried without an API key."""
    raw = [
        ("Arsenal", "Sheffield United", "England - Premier League", "16:00", 0.68, 0.20, 0.12, 2.2, 0.7),
        ("Bayern Munich", "Heidenheim", "Germany - Bundesliga", "15:30", 0.75, 0.15, 0.10, 2.6, 0.8),
        ("Real Madrid", "Las Palmas", "Spain - La Liga", "21:00", 0.70, 0.19, 0.11, 2.3, 0.7),
        ("Inter", "Empoli", "Italy - Serie A", "20:45", 0.66, 0.22, 0.12, 2.1, 0.6),
        ("PSG", "Clermont", "France - Ligue 1", "21:00", 0.72, 0.17, 0.11, 2.4, 0.8),
        ("Ajax", "Volendam", "Netherlands - Eredivisie", "14:30", 0.64, 0.22, 0.14, 2.0, 0.9),
        ("Celtic", "Ross County", "Scotland - Premiership", "16:00", 0.71, 0.18, 0.11, 2.3, 0.7),
        ("Benfica", "Arouca", "Portugal - Primeira Liga", "19:00", 0.63, 0.23, 0.14, 1.9, 0.8),
        ("Galatasaray", "Kayserispor", "Turkey - Super Lig", "18:00", 0.62, 0.23, 0.15, 1.9, 0.9),
        ("Brighton", "Everton", "England - Premier League", "18:30", 0.48, 0.27, 0.25, 1.6, 1.1),
    ]
    return [
        {"home": h, "away": a, "league": lg, "kickoff": ko,
         "p_home": ph, "p_draw": pd, "p_away": pa, "lam_home": lh, "lam_away": la}
        for h, a, lg, ko, ph, pd, pa, lh, la in raw
    ]


# ---------------------------------------------------------------- main

def main():
    demo = "--demo" in sys.argv
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()

    if demo:
        print("DEMO MODE - using built-in sample matches (no API calls, no email).")
        matches = demo_matches()
    else:
        if cfg.get("provider", "football-data") == "api-football":
            if not cfg.get("api_key") or "PUT-YOUR" in cfg["api_key"]:
                print("ERROR: no API key in config.json (api_key).")
                sys.exit(1)
            matches = fetch_matches(cfg)
        else:
            token = cfg.get("football_data_token", "")
            if not token or "PUT-YOUR" in token:
                print("ERROR: no football-data.org token in config.json.")
                print("Get a free token at https://www.football-data.org/client/register")
                print("and paste it into football_data_token, or run with --demo.")
                sys.exit(1)
            matches = fetch_matches_footballdata(cfg)

    send = not (demo or dry_run)
    if not matches:
        exit_without_ticket(cfg, "No suitable matches found today - no ticket generated.", send)

    picks = select_picks(matches, cfg)
    if not picks:
        exit_without_ticket(cfg, "Fewer than {} usable matches today - no ticket generated.".format(
            cfg.get("min_picks", 5)), send)
    odds, _ = combo_stats(picks)
    if not cfg["target_odds_min"] <= odds <= cfg["target_odds_max"]:
        print("NOTE: today's matches could not reach the {}-{} odds target; "
              "closest ticket is {:.1f}.".format(cfg["target_odds_min"],
                                                  cfg["target_odds_max"], odds))

    print_summary(picks, cfg)

    html = build_html(picks, cfg)
    out_dir = SCRIPT_DIR / "tickets"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "ticket_{}.html".format(date.today().isoformat())
    out_file.write_text(html, encoding="utf-8")
    print("Ticket saved to {}".format(out_file))

    if demo or dry_run:
        print("(email skipped in demo/dry-run mode)")
    elif cfg["email"].get("enabled"):
        try:
            send_email(cfg, html)
        except Exception as e:
            print("Email failed: {}".format(e))
            print("The ticket HTML file is still saved locally.")
    else:
        print("Email is disabled in config.json (email.enabled = false).")


if __name__ == "__main__":
    main()
