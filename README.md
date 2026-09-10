# job-radar

A small job-alert bot for new-grad software engineering roles. Twice a day it scans
~2,400 company job boards, keeps only entry-level engineering jobs in the SF Bay Area
(or US remote), and pushes the ones it hasn't reported before to Telegram.

It doesn't scrape LinkedIn or Indeed. It only calls the public JSON endpoints that
applicant tracking systems (ATS) expose for each company's careers page, so it needs
no login, hits no CAPTCHAs, and can't get an account banned.

It only finds jobs. Applying stays manual.

## At a glance

| | |
|---|---|
| Boards scanned | 2,446 (`companies.yaml`) |
| Postings fetched per run | ~120,000 |
| Matches in a 14-day window | ~100 |
| Run time | ~3.5 minutes on a GitHub Actions runner |
| Schedule | 09:06 and 11:36 Pacific, daily |
| Delivery | Telegram, plus `digest.md` in the repo |
| Dependencies | Python 3.10+, `pyyaml` |

## How it works

```
companies.yaml ──► fetch (12 threads) ──► filter ──► dedup against seen.json ──► digest.md + Telegram
                   5 ATS adapters          title / location /       only jobs never           commit seen.json
                                           experience / age         shown before              back to the repo
```

1. **Fetch.** One adapter per ATS turns that vendor's JSON into a common `Job` record.
   One broken board is logged and skipped; it never fails the run.
2. **Filter.** Title allowlist and blocklist, location allowlist and blocklist,
   posting age, and the minimum years of experience read from the job description.
3. **Dedup.** Every job has a stable key (`source:company:job_id`). `seen.json` records
   the keys already shown, so a job is reported once even if its timestamp is refreshed.
4. **Deliver.** Write `digest.md`, send it to Telegram (split across messages because
   of Telegram's 4,096-character limit), then commit `seen.json` back to the repo.

## Data sources

| ATS | Boards | Job description available? | Notes |
|---|---:|---|---|
| Greenhouse | 512 | Yes, `?content=true` | Response is ~12x larger, but it's still one request |
| Ashby | 347 | Yes, `descriptionPlain` | |
| Lever | 187 | Yes, `descriptionPlain` | |
| SmartRecruiters | 159 | No | Would take one extra request per job |
| Workday | 1,241 | No | Would take one extra request per job |

The company list was built by extracting ATS tokens from the job URLs in
[SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions)
and checking each one against its live API.

**Workday quirks** (learned the hard way):
- `limit` is hard-capped at 20. Asking for 25 returns HTTP 400.
- Results are sorted newest-first **only when `searchText` is empty**. Any search text
  switches the sort to relevance, and page 1 fills up with month-old postings. So the
  adapter fetches the newest `workday_pages` pages unfiltered and filters locally.

## Filtering

All rules live in [`config.yaml`](config.yaml). Entries are case-insensitive substrings;
entries starting with `re:` are regular expressions.

| Rule | Purpose |
|---|---|
| `include_any` | Title must look like an engineering role |
| `exclude_any` | Drop senior, staff, lead, L4+, II/III, managers, interns, and non-engineering roles |
| `boost_any` | Titles that say new grad / entry level / Engineer I / L3 get a ⭐ and sort first |
| `locations_any` | Bay Area cities and `remote` |
| `locations_exclude_any` | Runs first, so "Remote – Spain" can't slip in through `remote` |
| `max_years_experience` | Drop jobs whose description asks for more years than this (currently 1) |
| `max_age_days` | Ignore postings older than this |

### Reading experience requirements from descriptions

Titles almost never state experience. A job titled just "Software Engineer" might take
new grads or might want five years. So the Greenhouse, Lever, and Ashby adapters parse
the description as it arrives and keep a single number, `min_years`. The description
text itself is thrown away, since keeping 120k of them in memory would be wasteful.

`extract_min_years` matches phrases like `3+ years`, `3-5 years`, and `3 to 5 years`,
but only when they appear near the word "experience". It skips phrases about company
history ("founded 5 years ago", "over the past 3 years"). It takes the **minimum** match,
so "0-2 years" counts as 0.

The design choice that matters: **if no requirement can be found, the job is kept.**
A false negative means you silently never see a job you could have applied to, which
is worse than an extra link to skip. Adding the description check cut matches from
145 to 87; everything it removed asked for 2+ years.

## Dedup and rollover

`output.max_items` (60) caps how many jobs go out per run. Only jobs that were actually
shown are written to `seen.json`. The rest stay "new" and roll over to the next run, so
a burst of 100+ postings is spread over a few runs instead of being silently dropped.

Entries older than `state.forget_after_days` (120) are pruned so the file doesn't grow forever.

## Scheduling

The scan runs on GitHub Actions ([`.github/workflows/daily.yml`](.github/workflows/daily.yml)),
so it runs whether or not any personal machine is on.

GitHub's scheduled workflows are best-effort, and runs set for the top of the hour
queue badly. With `0 18 * * *`, the observed start delays were **4h17m and 7h53m**.
The workflow now:

- schedules at off-peak minutes: `6 16` and `36 18` UTC, which is 09:06 and 11:36 PDT;
- runs **twice**, so at least one run is likely done by noon. The second run also
  catches jobs posted that morning, and dedup means the two runs never repeat a job;
- has `timeout-minutes: 45`;
- sends a Telegram alert with the run URL if any step fails. Without that, a failed
  run looks exactly like a day with no new jobs.

cron is UTC and ignores daylight saving. In winter (PST, UTC-8) both runs fire an hour
earlier in local time; add 1 to each hour field to compensate.

## Setup

1. **Create a Telegram bot.** Message `@BotFather`, send `/newbot`, and copy the token.
2. **Find your chat ID.** Open the new bot's chat, press **Start**, send it any message, then:
   ```bash
   TELEGRAM_BOT_TOKEN="<token>" python jobradar.py --telegram-setup
   ```
   This prints your `chat_id`.
3. **Push this repo to a private GitHub repository.** `seen.json` and `digest.md` show
   which companies you're tracking.
4. **Add repository secrets** under Settings → Secrets and variables → Actions:
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. **Allow the workflow to push.** Settings → Actions → General → Workflow permissions →
   **Read and write permissions**. Without this, `seen.json` never gets saved and the
   same jobs come back every day.
6. **Test it:** Actions tab → `job-radar` → **Run workflow**.

## Running locally

```bash
pip install -r requirements.txt
python jobradar.py              # normal run: only new jobs, updates seen.json
python jobradar.py --verify     # check every company token still resolves
python jobradar.py --all        # ignore seen.json and print every match
python jobradar.py --dry-run    # run without writing seen.json
```

To tune filters, combine `--all --dry-run`: edit `config.yaml`, rerun, and repeat until
the output is free of noise.

[`run.bat`](run.bat) is an optional entry point for Windows Task Scheduler. It's not in
use now. Running it alongside the GitHub workflow would give you two `seen.json` files
that drift apart.

## Adding companies

Look at the company's careers page URL:

| URL looks like | Add to `companies.yaml` |
|---|---|
| `boards.greenhouse.io/stripe` | `greenhouse: [stripe]` |
| `jobs.lever.co/plaid` | `lever: [plaid]` |
| `jobs.ashbyhq.com/linear` | `ashby: [linear]` |
| `jobs.smartrecruiters.com/Acme` | `smartrecruiters: [Acme]` |
| `nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite` | `workday: ["nvidia.wd5.myworkdayjobs.com\|nvidia\|NVIDIAExternalCareerSite"]` |

Custom careers pages usually still run on one of these ATSs underneath. Searching the
page source for `greenhouse`, `lever`, or `ashby` usually finds it. Run `--verify`
afterwards; a 404 means the token is wrong.

## Adding a data source

Write a function modeled on `from_greenhouse` that returns a list of `Job`, and register
it in `ADAPTERS`. The `key` must be globally unique; use `source:company:job_id`. If the
API returns descriptions, set `min_years=extract_min_years(...)` so the experience
filter applies.

## Tests

```bash
python _selftest.py
```

Runs offline against fake API responses. Covers filtering, dedup and pruning, the
experience parser (including the "N years ago" false-positive cases), Workday
relative-date parsing and pagination, and Markdown rendering.

## Known limitations

- **Some big employers aren't covered.** Apple, Amazon, and TikTok run their own careers
  sites, and JPMorgan and others use Oracle Cloud. None of them have a public JSON feed
  like the ATSs above.
- **No experience filter for SmartRecruiters or Workday.** Their list endpoints don't
  include descriptions, so those jobs are filtered on title only.
- **Workday rate limits.** About 2,500 Workday requests per run occasionally draw HTTP
  429s, and GitHub's shared runner IPs make that more likely. Affected boards are listed
  under "Fetch errors" in the digest. Retrying with backoff would help.
- **Schedule timing isn't guaranteed.** See [Scheduling](#scheduling).
