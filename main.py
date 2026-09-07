"""Daily Football Ticket - analyzes today's fixtures and builds a 7-pick ticket.

Markets used: 1X2, Double Chance, Over/Under goals, BTTS, Corners.
Handicap markets are never used.

Usage:
    python main.py            # full run: fetch, analyze, save, email
    python main.py --dry-run  # analyze and save, but do not send email
    python main.py --demo     # run with built-in sample data (no API key needed)
"""

import json
import math
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

BASE_URL = "https://v3.football.api-sports.io"
SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------- config

def load_config():
    with open(SCRIPT_DIR / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


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
    add("Goals", "Under 3.5 goals", poisson_cdf(lam, 3))
    add("Goals", "Under 4.5 goals", poisson_cdf(lam, 4))

    # Both teams to score
    btts = (1.0 - math.exp(-match["lam_home"])) * (1.0 - math.exp(-match["lam_away"]))
    add("BTTS", "Both teams to score: Yes", btts)
    add("BTTS", "Both teams to score: No", 1.0 - btts)

    # Corners (league-average based prior; most matches clear 7.5 corners)
    if cfg.get("include_corners", True):
        add("Corners", "Over 7.5 total corners", 0.78)

    return cands


def select_picks(matches, cfg):
    """Pick the strongest selection per match, then keep the best 7 overall."""
    best_per_match = []
    for m in matches:
        cands = [c for c in build_candidates(m, cfg)
                 if c["prob"] >= cfg["min_pick_probability"]
                 and c["odds"] >= cfg["min_odds_per_pick"]]
        if cands:
            cands.sort(key=lambda c: c["prob"], reverse=True)
            best_per_match.append(cands)

    # Greedy: highest-probability picks first, with per-market caps for variety
    max_per_market = cfg.get("max_picks_per_market", 3)
    max_corners = cfg.get("max_corners_picks", 2)
    market_counts = {}
    picks = []

    flat = sorted((c for cands in best_per_match for c in cands[:3]),
                  key=lambda c: c["prob"], reverse=True)
    used_matches = set()
    for c in flat:
        key = id(c["match"])
        if key in used_matches:
            continue
        cap = max_corners if c["category"] == "Corners" else max_per_market
        if market_counts.get(c["category"], 0) >= cap:
            continue
        picks.append(c)
        used_matches.add(key)
        market_counts[c["category"]] = market_counts.get(c["category"], 0) + 1
        if len(picks) >= cfg["max_picks"]:
            break

    return picks


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


def send_email(cfg, html):
    email_cfg = cfg["email"]
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = "Daily Football Ticket - {}".format(date.today().strftime("%d %B %Y"))
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=30) as server:
        server.starttls()
        # Google shows app passwords with spaces; they must be removed for login
        server.login(email_cfg["username"], email_cfg["password"].replace(" ", ""))
        server.sendmail(email_cfg["from"], [email_cfg["to"]], msg.as_string())
    print("Email sent to {}".format(email_cfg["to"]))


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
        if not cfg.get("api_key") or "PUT-YOUR" in cfg["api_key"]:
            print("ERROR: no API key in config.json.")
            print("Get a free key at https://dashboard.api-football.com and paste it")
            print("into the api_key field, or run with --demo to see sample output.")
            sys.exit(1)
        matches = fetch_matches(cfg)

    if not matches:
        print("No suitable matches found today - no ticket generated.")
        sys.exit(0)

    picks = select_picks(matches, cfg)
    if len(picks) < cfg["max_picks"]:
        print("NOTE: only {} picks met the confidence threshold today.".format(len(picks)))
    if not picks:
        print("No picks met the confidence threshold - no ticket generated.")
        sys.exit(0)

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
