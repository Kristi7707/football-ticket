# Daily Football Ticket

Analyzes today's football fixtures, builds a ticket of **5-7 picks** with combined odds
of about 30-50 (stake **10+**), saves it as an HTML file, and **emails it to you every day**.

On days without enough usable matches you get a short "no ticket today" email instead,
so a quiet inbox never means a silent failure.

Markets used: **1X2, Double Chance, Over/Under goals, BTTS, Corners**.
Handicap markets are never used. Football only.

## Requirements

- Python 3.8+ (no extra packages needed — standard library only)
- A free token from [football-data.org](https://www.football-data.org/client/register)
  (free tier never expires: Premier League, Championship, Bundesliga, Serie A, La Liga,
  Ligue 1, Eredivisie, Primeira Liga, Brazilian Serie A, Champions League)

### How the picks are rated

Each team's attack and defence strength comes from the goals it scored and conceded per
game this season, compared with its league's average, and pulled toward **last season's**
final table (a newly promoted team starts weaker than average). That gives expected goals
for both sides, which become a probability for every market the app bets on (with a
correction for the extra draws and 0-0/1-1 results that plain statistics miss).

Those probabilities are then **calibrated** against how often such picks really won over
~3,700 past matches in nine leagues. This matters: the uncalibrated model was overconfident
on goals markets (it said 60-75% for outcomes that happened only about 50-65% of the time), and the ticket
builder looks for exactly the picks it feels most sure about. The result:

- **Match results** (home / away win, double chance): the model has real skill. It picks
  the right result about half the time (50%), against 44% for always guessing a home win,
  and its win probabilities are about right (a team given 70% wins about 70% of the time).
- **Goals and both-teams-to-score**: only a small edge over the average rate. Treat these
  picks as little better than a coin flip with a slight lean.
- **Draws** are close to unpredictable.

`python tools/backtest.py HISTORY_DIR` repeats this measurement (see *Checking the model*).
The older API-Football provider is still supported: set `"provider": "api-football"` and
`api_key`; it keeps its own probabilities and is not calibrated.

### European cups

- **Champions League (`CL`)** is on the free plan. When both clubs play in a domestic
  league the app has a table for, the match is rated from their **domestic form**: on real
  Champions League results this beat the cup table, which is too thin for most of the league
  phase. The probabilities are then pulled a quarter of the way toward the average rate.
  Only when a club has no domestic table (Norway, Turkey, Scotland, ...) does the app fall
  back to the cup table, if both teams have 3+ cup games, pulled halfway to the average.
  Otherwise the match is skipped and the log says why. Domestic form is an approximation: a
  team is measured against *its own* league's average, so the model does not know how strong
  one league is compared with another.
- **Europa League (`EL`)** and **Conference League (`UCL`)** are in the `competitions`
  list, but football-data.org only serves them on a paid plan. On the free plan they
  simply produce no fixtures; if the API refuses them the log says "not available on your
  football-data plan". The log also shows `by competition: ...` so you can see what was
  found. To rate more teams, add their domestic leagues to `competitions` (also paid
  plans), e.g. `"SPL"` for Scotland or `"BJL"` for Belgium.
- An invalid or revoked football-data token (HTTP 400) makes the run **fail** rather than
  report an empty day, so GitHub flags it.

## Setup

1. **Token**: register at https://www.football-data.org/client/register, copy the token
   from the email they send, and paste it into `config.json` → `"football_data_token"`.
   For the GitHub Actions run, add it as the repository secret `FOOTBALL_DATA_TOKEN`.

2. **Email** (optional but recommended): in `config.json` → `"email"`:
   - Set `"enabled": true`
   - Easiest option: a **Gmail account with an App Password**
     (Google Account → Security → 2-Step Verification → App passwords).
     Use the app password as `"password"`, your Gmail address as `"username"` and `"from"`.
   - `"to"` is already set to your address.
   - Note: Outlook/Live personal accounts no longer allow simple SMTP login,
     which is why Gmail is the recommended sender.

3. **Try it** (works even before you have an API key):
   ```
   python main.py --demo
   ```

4. **Real run** (takes a few minutes — the free API plan allows 10 requests/minute):
   ```
   python main.py
   ```

## Daily automation (GitHub Actions + cron-job.org, 13:00 Albania time)

GitHub's own scheduler can start a run hours late, so the workflow has no `schedule:`.
Instead a free timer at [cron-job.org](https://cron-job.org) starts it at exactly 13:00
`Europe/Tirane` every day (daylight saving is handled by the timezone setting). A started
run takes one to three minutes (the free plan allows 10 requests a minute, so the app
paces itself), so the email arrives shortly after 13:00.

1. **GitHub token**: GitHub → Settings → Developer settings → Personal access tokens →
   *Fine-grained tokens* → Generate. Repository access: **only** `football-ticket`.
   Permissions → Repository → **Actions: Read and write**. Pick the longest expiry and
   note the date: when it expires the timer starts failing and must get a new token.
2. **cron-job.org** → Create cronjob:
   - URL: `https://api.github.com/repos/Kristi7707/football-ticket/actions/workflows/daily-ticket.yml/dispatches`
   - Schedule: *Every day* at 13:00, timezone **Europe/Tirane**
   - Advanced → Request method **POST**, request body `{"ref":"main"}`
   - Advanced → Headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your token>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - Turn on failure notifications. GitHub answers `204 No Content` on success.
3. **Test it**: use *Test run* in cron-job.org, or Actions → Daily Football Ticket →
   *Run workflow*. A new run should appear within seconds.

## Daily automation, alternative (Windows Task Scheduler)

Run this once in a terminal (adjust the path if you move the folder, and the time if you
want a different hour — `09:00` means the ticket is emailed at 9 AM daily):

```
schtasks /Create /SC DAILY /TN "DailyFootballTicket" /TR "\"C:\path\to\football-ticket\run_daily.bat\"" /ST 09:00
```

Output of each run is appended to `run_log.txt`; every ticket is also saved in `tickets\`.

## Tuning (config.json)

| Setting | Meaning |
|---|---|
| `stake` | Ticket stake (minimum 10 is enforced) |
| `min_picks` / `max_picks` | Picks per ticket (5 to 7, one per match) |
| `target_odds_min` / `target_odds_max` | Combined odds the ticket should land in (default 30-50) |
| `min_pick_probability` | Only picks at/above this confidence are considered (default 0.42) |
| `min_odds_per_pick` | Skips picks whose odds would be too tiny (default 1.3) |
| `include_corners` / `max_corners_picks` | Allow corner picks and cap how many |
| `league_ids` | Which leagues to scan (API-Football league IDs); empty = all |
| `max_matches_analyzed` | Cap on fixtures analyzed per day (keeps within free API limits) |

## How often do tickets win?

The default ticket (5-7 picks, combined odds 30-50) needs every pick to be right, and each
pick is only about 45-55% likely, so it wins roughly **2-3% of the time** - about once every
six weeks. No amount of modelling changes that; the combined odds do. We replayed the app's
real ticket builder over every past match day (using only information available beforehand,
and only matches still to be played at the 13:00 daily run):

| Ticket type | `min_picks`/`max_picks` | `target_odds_min`-`max` | `min_pick_probability` | Won in the replay |
|---|---|---|---|---|
| Default | 5 / 7 | 30 - 50 | 0.42 | 6 of 191 (3%) |
| 3 picks | 3 / 3 | 6 - 10 | 0.55 | 47 of 232 (20%) |
| 3 picks | 3 / 3 | 4 - 6 | 0.60 | 45 of 219 (21%) |
| 2 picks | 2 / 2 | 3 - 4 | 0.60 | 91 of 246 (37%) |
| 2 picks | 2 / 2 | 2 - 2.6 | 0.65 | 109 of 235 (46%) |
| 1 pick | 1 / 1 | 1.35 - 1.7 | 0.60 | 210 of 301 (70%) |

Lower combined odds win more often, by definition. Notes:

- **These odds are the app's own estimates, not bookmaker prices.** Real prices include the
  bookmaker's margin (and are set by people with better information), so what you are paid
  for a winning ticket is lower than the "odds" shown here. This is not a way to make money.
- Each row is a few hundred tickets, so expect +/- 3 percentage points.
- The **GitHub run reads `config.example.json`** (the real `config.json` is not committed),
  so change the ticket type there.
- Corner picks are league-average assumptions, not per-match data, and cannot be checked
  against results; set `"include_corners": false` to leave them out.

## Checking the model

`tools/fetch_history.py` downloads finished matches (the manual "Fetch match history"
workflow runs it with your token and saves them as a downloadable artifact), and
`tools/backtest.py HISTORY_DIR [--tickets]` scores the app's own functions against them
and prints whether the calibration table in `main.py` has gone stale. Re-run it each
season; the table was fitted on the 2025 and 2026 seasons.
