#!/usr/bin/env python3
"""
job-radar — scan company job boards daily and report only the postings that are new.

Usage:
    python jobradar.py                   # normal run
    python jobradar.py --verify          # only check that the company tokens resolve
    python jobradar.py --dry-run         # run without writing state (for tuning filters)
    python jobradar.py --all             # ignore state and print every match
    python jobradar.py --telegram-setup  # find your Telegram chat_id and send a test message
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import html
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pyyaml is required:  pip install pyyaml")

UA = "job-radar/1.0 (personal job alert script)"
TIMEOUT = 20

# The Windows console isn't UTF-8 by default; ✓/✗/⭐ would raise UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Job:
    key: str            # globally unique dedup key
    source: str         # greenhouse / lever / ...
    company: str
    title: str
    location: str
    url: str
    posted_at: str      # ISO string, "" if unknown
    starred: bool = False   # title explicitly says new grad / entry level / etc.
    min_years: int = -1     # minimum years required by the description; -1 = unknown

    @property
    def age_days(self):
        dt = parse_dt(self.posted_at)
        if dt is None:
            return None
        return (datetime.now(timezone.utc) - dt).days


def parse_dt(value):
    """Normalize each ATS's timestamp format into an aware datetime."""
    if not value:
        return None
    if isinstance(value, (int, float)):          # Lever uses epoch milliseconds
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Experience requirements: postings almost never put years in the title,
# so the only reliable place to read them is the description.
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
# "3+ years" / "3-5 years" / "3 to 5 years" — capture the leading number
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*\d{1,2}\s*\+?)?\s*year", re.I)


def strip_html(s):
    return " ".join(html.unescape(_TAG.sub(" ", s or "")).split())


def extract_min_years(text):
    """Minimum years of experience the description asks for, or -1 if none found.

    Takes the **smallest** match, because postings often say "0-2 years" or
    mention unrelated durations elsewhere. Taking the minimum biases toward
    keeping a job: better to show one extra posting than to silently hide one
    you could have applied to.
    """
    if not text:
        return -1
    low = text.lower()
    best = -1
    for m in _YEARS.finditer(low):
        # "founded 5 years ago" / "over the past 3 years" describe company
        # history, not a requirement — but they often sit right next to the
        # word "experience", so they have to be ruled out explicitly.
        if re.match(r"s?\s+ago\b", low[m.end():m.end() + 10]):
            continue
        if re.search(r"\b(?:past|last|previous|next|first)\s+$",
                     low[max(0, m.start() - 15):m.start()]):
            continue
        # Only count years that appear near "experience"
        window = low[max(0, m.start() - 80): m.end() + 80]
        if "experience" not in window and "exp." not in window:
            continue
        n = int(m.group(1))
        if n > 30:                     # clearly not a years-of-experience number
            continue
        if best < 0 or n < best:
            best = n
    return best


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:                  # noqa: BLE001
            last = exc
            if attempt < retries:
                continue
    raise last


def from_greenhouse(company):
    # content=true makes the response ~10x larger but costs no extra request,
    # and the experience requirement only lives in the description.
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    data = fetch_json(url)
    out = []
    for j in data.get("jobs", []):
        out.append(Job(
            key=f"greenhouse:{company}:{j['id']}",
            source="greenhouse",
            company=company,
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            posted_at=j.get("first_published") or j.get("updated_at") or "",
            min_years=extract_min_years(strip_html(j.get("content", ""))),
        ))
    return out


def from_lever(company):
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    data = fetch_json(url)
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append(Job(
            key=f"lever:{company}:{j['id']}",
            source="lever",
            company=company,
            title=j.get("text", ""),
            location=cats.get("location", "") or "",
            url=j.get("hostedUrl", ""),
            posted_at=j.get("createdAt", ""),
            min_years=extract_min_years(
                j.get("descriptionPlain") or j.get("description") or ""),
        ))
    return out


def from_ashby(company):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
    data = fetch_json(url)
    out = []
    for j in data.get("jobs", []):
        out.append(Job(
            key=f"ashby:{company}:{j['id']}",
            source="ashby",
            company=company,
            title=j.get("title", ""),
            location=j.get("location", "") or "",
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            posted_at=j.get("publishedAt", "") or "",
            min_years=extract_min_years(j.get("descriptionPlain") or ""),
        ))
    return out


def from_smartrecruiters(company):
    url = (f"https://api.smartrecruiters.com/v1/companies/{company}"
           f"/postings?limit=100")
    data = fetch_json(url)
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        city = loc.get("city", "")
        country = loc.get("country", "")
        out.append(Job(
            key=f"smartrecruiters:{company}:{j['id']}",
            source="smartrecruiters",
            company=company,
            title=j.get("name", ""),
            location=", ".join(x for x in (city, country) if x),
            # Don't use j["ref"] — that's an API URL, not a page a person can open
            url=f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
            posted_at=j.get("releasedDate", "") or "",
        ))
    return out


def post_json(url, payload, retries=2):
    data = json.dumps(payload).encode()
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:                  # noqa: BLE001
            last = exc
            if attempt < retries:
                continue
    raise last


# Workday gives no dates, only relative text like "Posted 3 Days Ago"
_POSTED = re.compile(r"(\d+)\s*\+?\s*days?", re.IGNORECASE)


def parse_posted_on(text):
    t = (text or "").lower()
    if "today" in t or "just posted" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = _POSTED.search(t)
        if not m:
            return ""
        days = int(m.group(1))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def from_workday(spec, pages=1):
    """spec format: host|tenant|site

    Workday hard-caps page size at 20, but results come back newest-first,
    so the first page or two per day is enough — no need to page through thousands.
    """
    try:
        host, tenant, site = spec.split("|")
    except ValueError:
        raise ValueError(f"workday entries must look like host|tenant|site, got: {spec}")

    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    for page in range(pages):
        data = post_json(api, {"appliedFacets": {}, "limit": 20,
                               "offset": page * 20, "searchText": ""})
        postings = data.get("jobPostings") or []
        for j in postings:
            path = j.get("externalPath", "")
            out.append(Job(
                key=f"workday:{tenant}:{site}:{path}",
                source="workday",
                company=tenant,
                title=j.get("title", ""),
                location=j.get("locationsText", "") or "",
                url=f"https://{host}/en-US/{site}{path}",
                posted_at=parse_posted_on(j.get("postedOn")),
            ))
        if len(postings) < 20:
            break
    return out


ADAPTERS = {
    "greenhouse": from_greenhouse,
    "lever": from_lever,
    "ashby": from_ashby,
    "smartrecruiters": from_smartrecruiters,
    "workday": from_workday,
}


def collect(sources, workers=12, verbose=False, workday_pages=1):
    """Fetch every configured board concurrently. One failing board doesn't affect the rest.

    With thousands of boards a serial run would take half an hour, hence the
    thread pool. Don't push `workers` much higher: 12 gets through ~2,400
    boards in 3-4 minutes, and more mostly just earns HTTP 429s.
    """
    tasks, errors = [], []
    for source, companies in (sources or {}).items():
        if source not in ADAPTERS:
            errors.append((source, "-", "unknown source; supported: " + ", ".join(ADAPTERS)))
            continue
        for company in companies or []:
            tasks.append((source, company))

    def one(task):
        source, company = task
        try:
            if source == "workday":
                return task, from_workday(company, pages=workday_pages), None
            return task, ADAPTERS[source](company), None
        except urllib.error.HTTPError as exc:
            return task, [], ("token may be wrong" if exc.code == 404
                              else f"HTTP {exc.code}")
        except Exception as exc:                  # noqa: BLE001
            return task, [], str(exc)[:80]

    jobs = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (source, company), found, err in pool.map(one, tasks):
            done += 1
            if err is None:
                jobs.extend(found)
                if verbose:
                    print(f"  ✓ {source}/{company}: {len(found)} jobs",
                          file=sys.stderr)
            else:
                errors.append((source, company, err))
                if verbose:
                    print(f"  ✗ {source}/{company}: {err}", file=sys.stderr)
            if not verbose and done % 200 == 0:
                print(f"  … {done}/{len(tasks)}", file=sys.stderr)
    return jobs, errors


def load_sources(config, config_path):
    """The company list can live in config.yaml or in a separate file (easier with thousands)."""
    ref = config.get("sources_file")
    if not ref:
        return config.get("sources")
    p = Path(ref)
    if not p.is_absolute():
        p = Path(config_path).parent / p
    loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    merged = dict(loaded.get("sources") or {})
    # Extra companies listed directly in config.yaml get merged in — handy for quick additions
    for source, extra in (config.get("sources") or {}).items():
        merged[source] = sorted(set(merged.get(source) or []) | set(extra or []))
    return merged


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def matches_any(text, patterns):
    text = (text or "").lower()
    for p in patterns or []:
        if p.startswith("re:"):
            if re.search(p[3:], text, re.IGNORECASE):
                return True
        elif p.lower() in text:
            return True
    return False


def apply_filters(jobs, f):
    include = f.get("include_any") or []
    exclude = f.get("exclude_any") or []
    locations = f.get("locations_any") or []
    loc_exclude = f.get("locations_exclude_any") or []
    boost = f.get("boost_any") or []
    max_years = f.get("max_years_experience")
    max_age = f.get("max_age_days")
    unknown_loc = (f.get("unknown_location") or "keep").lower()

    kept = []
    for job in jobs:
        if include and not matches_any(job.title, include):
            continue
        if matches_any(job.title, exclude):
            continue
        # Location blocklist runs first: the "remote" allowlist entry alone
        # would otherwise let things like "Remote Spain" through.
        if loc_exclude and matches_any(job.location, loc_exclude):
            continue
        if locations:
            if not job.location.strip():
                if unknown_loc == "drop":
                    continue
            elif not matches_any(job.location, locations):
                continue
        # Experience. min_years == -1 means no description or no years mentioned;
        # keep those — better to let you click through than silently hide a job you could apply to.
        if max_years is not None and job.min_years >= 0 and job.min_years > max_years:
            continue
        if max_age is not None:
            age = job.age_days
            if age is not None and age > max_age:
                continue
        job.starred = bool(boost) and matches_any(job.title, boost)
        kept.append(job)
    # Explicit new-grad roles first, then newest first
    kept.sort(key=lambda j: (not j.starred, j.age_days if j.age_days is not None else 999))
    return kept


# --------------------------------------------------------------------------
# State (the core of deduplication)
# --------------------------------------------------------------------------

def load_state(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"warning: can't parse {path}, treating it as empty", file=sys.stderr)
        return {}


def save_state(path, state, forget_after_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=forget_after_days)
    pruned = {}
    for key, seen_at in state.items():
        dt = parse_dt(seen_at)
        if dt is None or dt >= cutoff:
            pruned[key] = seen_at
    Path(path).write_text(json.dumps(pruned, indent=1, sort_keys=True),
                          encoding="utf-8")
    return len(state) - len(pruned)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def render_markdown(jobs, errors):
    today = datetime.now().strftime("%Y-%m-%d")
    if not jobs:
        body = f"# New jobs · {today}\n\nNothing new today.\n"
    else:
        def entry(j):
            meta = j.location or "location not listed"
            age = j.age_days
            if age is not None:
                meta += f" · {age}d ago"
            return f"- [{j.title}]({j.url}) — **{j.company}**  \n  {meta}"

        starred = [j for j in jobs if j.starred]
        rest = [j for j in jobs if not j.starred]
        lines = [f"# New jobs · {today}", "", f"{len(jobs)} total.", ""]
        if starred:
            lines += [f"## ⭐ Explicitly new grad / entry level ({len(starred)})", ""]
            lines += [entry(j) for j in starred] + [""]
        if rest:
            lines += [f"## Other engineering roles ({len(rest)})", ""]
            lines += [entry(j) for j in rest] + [""]
        body = "\n".join(lines)

    if errors:
        body += "\n---\n\n**Fetch errors**\n\n"
        for source, company, why in errors:
            body += f"- `{source}/{company}` — {why}\n"
    return body


TG_LIMIT = 3800          # Telegram's hard limit is 4096; leave some headroom


def escape_html(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_telegram(jobs):
    """Return a list of messages. 60 jobs far exceed the 4096-char limit of a
    single message, and Telegram rejects oversize messages outright."""
    if not jobs:
        return []
    header = f"<b>{len(jobs)} new jobs today</b>"
    entries = []
    for j in jobs:
        star = "⭐ " if j.starred else "• "
        entries.append(
            f'{star}<a href="{j.url}">{escape_html(j.title)}</a>'
            f' — {escape_html(j.company)} ({escape_html(j.location or "?")})')

    msgs, cur = [], [header, ""]
    size = len(header) + 1
    for e in entries:
        if size + len(e) + 1 > TG_LIMIT and len(cur) > 2:
            msgs.append("\n".join(cur))
            cur, size = [], 0
        cur.append(e)
        size += len(e) + 1
    if cur:
        msgs.append("\n".join(cur))
    if len(msgs) > 1:
        msgs = [f"{m}\n\n<i>({i+1}/{len(msgs)})</i>" for i, m in enumerate(msgs)]
    return msgs


def send_telegram(messages):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("skipping Telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set",
              file=sys.stderr)
        return
    for i, text in enumerate(messages):
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"User-Agent": UA})
        try:
            urllib.request.urlopen(req, timeout=TIMEOUT).read()
            print(f"Telegram sent {i+1}/{len(messages)}", file=sys.stderr)
        except urllib.error.HTTPError as exc:
            print(f"Telegram send failed: HTTP {exc.code} "
                  f"{exc.read()[:200].decode('utf-8', 'replace')}", file=sys.stderr)
        except Exception as exc:                  # noqa: BLE001
            print(f"Telegram send failed: {exc}", file=sys.stderr)


def cmd_telegram_setup():
    """Find your chat_id. Send your bot any message first, then run this."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set the TELEGRAM_BOT_TOKEN environment variable first "
              "(create a bot with @BotFather to get one)", file=sys.stderr)
        return 1
    try:
        data = fetch_json(f"https://api.telegram.org/bot{token}/getUpdates")
    except Exception as exc:                      # noqa: BLE001
        print(f"getUpdates failed, the token may be wrong: {exc}", file=sys.stderr)
        return 1
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            name = chat.get("username") or chat.get("title") or chat.get("first_name", "")
            chats[chat["id"]] = name
    if not chats:
        print("No messages found. Send your bot any message in Telegram, then run this again.",
              file=sys.stderr)
        return 1
    print("Found these chat_ids:", file=sys.stderr)
    for cid, name in chats.items():
        print(f"  TELEGRAM_CHAT_ID = {cid}    ({name})", file=sys.stderr)
    if os.environ.get("TELEGRAM_CHAT_ID"):
        send_telegram(["<b>job-radar</b> connectivity test ✅"])
    return 0


# --------------------------------------------------------------------------

def cmd_verify(config, sources, workers, wd_pages=1):
    n = sum(len(v or []) for v in (sources or {}).values())
    print(f"Checking {n} company tokens …\n", file=sys.stderr)
    _, errors = collect(sources, workers=workers, verbose=(n <= 50),
                        workday_pages=wd_pages)
    print("", file=sys.stderr)
    if errors:
        print("These need fixing:", file=sys.stderr)
        for source, company, why in errors:
            print(f"  {source}/{company} — {why}", file=sys.stderr)
        return 1
    print("All good.", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--verify", action="store_true", help="only check company tokens")
    ap.add_argument("--dry-run", action="store_true", help="don't write state")
    ap.add_argument("--all", action="store_true", help="ignore state, print every match")
    ap.add_argument("--telegram-setup", action="store_true",
                    help="find your chat_id and send a test message")
    args = ap.parse_args()

    if args.telegram_setup:
        return cmd_telegram_setup()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    sources = load_sources(config, args.config)
    fetch_cfg = config.get("fetch") or {}
    workers = fetch_cfg.get("workers", 12)
    wd_pages = fetch_cfg.get("workday_pages", 1)

    if args.verify:
        return cmd_verify(config, sources, workers, wd_pages)

    out_cfg = config.get("output") or {}
    state_cfg = config.get("state") or {}
    state_path = state_cfg.get("path", "seen.json")

    n_co = sum(len(v or []) for v in (sources or {}).values())
    print(f"Fetching {n_co} boards …", file=sys.stderr)
    jobs, errors = collect(sources, workers=workers, verbose=(n_co <= 50),
                           workday_pages=wd_pages)
    print(f"\n{len(jobs)} jobs fetched, filtering", file=sys.stderr)

    jobs = apply_filters(jobs, config.get("filters") or {})
    print(f"{len(jobs)} after filters", file=sys.stderr)

    state = load_state(state_path)
    if args.all:
        fresh = jobs
    else:
        fresh = [j for j in jobs if j.key not in state]
    print(f"{len(fresh)} of them are new", file=sys.stderr)

    max_items = out_cfg.get("max_items")
    shown = fresh[:max_items] if max_items else fresh
    if len(fresh) > len(shown):
        print(f"(showing the first {len(shown)})", file=sys.stderr)

    md = render_markdown(shown, errors)
    md_path = out_cfg.get("markdown")
    if md_path:
        Path(md_path).write_text(md, encoding="utf-8")
    print("\n" + md)

    if (out_cfg.get("telegram") or {}).get("enabled"):
        msgs = render_telegram(shown)
        if msgs:
            send_telegram(msgs)

    if not args.dry_run:
        now = datetime.now(timezone.utc).isoformat()
        # Only record what was actually shown. Anything cut off by max_items
        # rolls over to the next run; otherwise, on a day with 100+ new jobs,
        # the ones past the cutoff would never be seen.
        for j in shown:
            state.setdefault(j.key, now)
        dropped = save_state(state_path, state,
                             state_cfg.get("forget_after_days", 120))
        if dropped:
            print(f"pruned {dropped} expired entries", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
