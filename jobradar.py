#!/usr/bin/env python3
"""
job-radar — 每天扫一遍你关心的公司招聘板，只告诉你新出现的岗位。

用法:
    python jobradar.py                 # 正常跑一次
    python jobradar.py --verify        # 只检查 config 里的公司 token 对不对
    python jobradar.py --dry-run       # 跑但不写 state（反复调过滤规则时用）
    python jobradar.py --all           # 忽略 state，输出所有命中的岗位
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
    sys.exit("需要 pyyaml:  pip install pyyaml")

UA = "job-radar/1.0 (personal job alert script)"
TIMEOUT = 20

# Windows 控制台默认不是 UTF-8，中文和 ✓/✗ 会直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Job:
    key: str            # 全局唯一去重键
    source: str         # greenhouse / lever / ...
    company: str
    title: str
    location: str
    url: str
    posted_at: str      # ISO 字符串，拿不到就是 ""
    starred: bool = False   # 标题明确写着 new grad / entry level 之类
    min_years: int = -1     # 描述里要求的最低年限；-1 = 没读到描述或没提年限

    @property
    def age_days(self):
        dt = parse_dt(self.posted_at)
        if dt is None:
            return None
        return (datetime.now(timezone.utc) - dt).days


def parse_dt(value):
    """把各家五花八门的时间格式统一成 aware datetime。"""
    if not value:
        return None
    if isinstance(value, (int, float)):          # lever 用毫秒时间戳
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
# 经验年限：招聘启事几乎从不把年限写进标题，只能从描述里读
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
# "3+ years" / "3-5 years" / "3 to 5 years"，取打头那个数字
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*\d{1,2}\s*\+?)?\s*year", re.I)


def strip_html(s):
    return " ".join(html.unescape(_TAG.sub(" ", s or "")).split())


def extract_min_years(text):
    """描述里要求的最低经验年限。读不出来返回 -1。

    取所有匹配里的**最小值**，因为岗位常写 "0-2 years" 或者在别处提到
    "founded 5 years ago"。取最小值会偏向保留 —— 宁可多推一个让你自己看，
    也不要把能投的岗位悄悄藏掉。
    """
    if not text:
        return -1
    low = text.lower()
    best = -1
    for m in _YEARS.finditer(low):
        # 只认 "experience" 附近的年限，否则 "5 years ago" 之类会误伤
        # "founded 5 years ago" / "over the past 3 years" 是在讲公司历史，
        # 不是经验要求 —— 但它们常常离 "experience" 很近，不排掉会误伤。
        if re.match(r"s?\s+ago\b", low[m.end():m.end() + 10]):
            continue
        if re.search(r"\b(?:past|last|previous|next|first)\s+$",
                     low[max(0, m.start() - 15):m.start()]):
            continue
        window = low[max(0, m.start() - 80): m.end() + 80]
        if "experience" not in window and "exp." not in window:
            continue
        n = int(m.group(1))
        if n > 30:                     # 明显不是年限
            continue
        if best < 0 or n < best:
            best = n
    return best


# --------------------------------------------------------------------------
# 抓取
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
    # content=true 会让响应大 10 倍，但不多花一个请求，
    # 而经验年限只写在描述里，值这个流量。
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
            # 不要用 j["ref"]，那是 API 地址，人点不进去
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


# Workday 不给日期，只给 "Posted 3 Days Ago" 这种相对文本
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
    """spec 格式: host|tenant|site

    Workday 的 limit 硬上限是 20，但结果按发布时间倒序，
    所以每天只取最新的一两页就够了 —— 不用翻完几千条。
    """
    try:
        host, tenant, site = spec.split("|")
    except ValueError:
        raise ValueError(f"workday 条目格式应为 host|tenant|site，收到: {spec}")

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
    """并发跑遍所有配置的公司。单个公司挂掉不影响其他的。

    公司数上千时串行要跑半小时，所以这里用线程池。workers 别调太高，
    12 左右已经能在 1 分钟内跑完 1200 家，再高容易吃到对方限流。
    """
    tasks, errors = [], []
    for source, companies in (sources or {}).items():
        if source not in ADAPTERS:
            errors.append((source, "-", "未知来源，支持: " + ", ".join(ADAPTERS)))
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
            return task, [], ("token 可能不对" if exc.code == 404
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
                    print(f"  ✓ {source}/{company}: {len(found)} 个岗位",
                          file=sys.stderr)
            else:
                errors.append((source, company, err))
                if verbose:
                    print(f"  ✗ {source}/{company}: {err}", file=sys.stderr)
            if not verbose and done % 200 == 0:
                print(f"  … {done}/{len(tasks)}", file=sys.stderr)
    return jobs, errors


def load_sources(config, config_path):
    """公司名单可以放在 config 里，也可以拆到单独文件（上千家时更好管）。"""
    ref = config.get("sources_file")
    if not ref:
        return config.get("sources")
    p = Path(ref)
    if not p.is_absolute():
        p = Path(config_path).parent / p
    loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    merged = dict(loaded.get("sources") or {})
    # config.yaml 里额外写的公司会并进来，方便临时加几家
    for source, extra in (config.get("sources") or {}).items():
        merged[source] = sorted(set(merged.get(source) or []) | set(extra or []))
    return merged


# --------------------------------------------------------------------------
# 过滤
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
        # 地点黑名单先跑：光靠 "remote" 白名单会把 Remote Spain 之类放进来
        if loc_exclude and matches_any(job.location, loc_exclude):
            continue
        if locations:
            if not job.location.strip():
                if unknown_loc == "drop":
                    continue
            elif not matches_any(job.location, locations):
                continue
        # 经验年限。min_years == -1 表示读不到描述或描述没提年限，那就放行 ——
        # 宁可让你自己点进去看一眼，也不要悄悄藏掉能投的岗位。
        if max_years is not None and job.min_years >= 0 and job.min_years > max_years:
            continue
        if max_age is not None:
            age = job.age_days
            if age is not None and age > max_age:
                continue
        job.starred = bool(boost) and matches_any(job.title, boost)
        kept.append(job)
    # 明确写着应届的排前面，其余按新旧排
    kept.sort(key=lambda j: (not j.starred, j.age_days if j.age_days is not None else 999))
    return kept


# --------------------------------------------------------------------------
# 状态（去重的核心）
# --------------------------------------------------------------------------

def load_state(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"警告: {path} 读不动，当作空的处理", file=sys.stderr)
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
# 输出
# --------------------------------------------------------------------------

def render_markdown(jobs, errors):
    today = datetime.now().strftime("%Y-%m-%d")
    if not jobs:
        body = f"# 今日新岗位 · {today}\n\n没有新的。\n"
    else:
        def entry(j):
            meta = j.location or "地点未标注"
            age = j.age_days
            if age is not None:
                meta += f" · {age} 天前"
            return f"- [{j.title}]({j.url}) — **{j.company}**  \n  {meta}"

        starred = [j for j in jobs if j.starred]
        rest = [j for j in jobs if not j.starred]
        lines = [f"# 今日新岗位 · {today}", "", f"共 {len(jobs)} 个。", ""]
        if starred:
            lines += [f"## ⭐ 明确写着应届 / 入门级（{len(starred)} 个）", ""]
            lines += [entry(j) for j in starred] + [""]
        if rest:
            lines += [f"## 其余工程岗（{len(rest)} 个）", ""]
            lines += [entry(j) for j in rest] + [""]
        body = "\n".join(lines)

    if errors:
        body += "\n---\n\n**抓取失败**\n\n"
        for source, company, why in errors:
            body += f"- `{source}/{company}` — {why}\n"
    return body


TG_LIMIT = 3800          # Telegram 硬上限是 4096，留点余量


def escape_html(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_telegram(jobs):
    """切成多条消息返回。60 个岗位远超单条 4096 字符上限，不切会被 Telegram 拒收。"""
    if not jobs:
        return []
    header = f"<b>今日新岗位 {len(jobs)} 个</b>"
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
        print("跳过 Telegram: 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID",
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
            print(f"Telegram 已发送 {i+1}/{len(messages)}", file=sys.stderr)
        except urllib.error.HTTPError as exc:
            print(f"Telegram 发送失败: HTTP {exc.code} "
                  f"{exc.read()[:200].decode('utf-8', 'replace')}", file=sys.stderr)
        except Exception as exc:                  # noqa: BLE001
            print(f"Telegram 发送失败: {exc}", file=sys.stderr)


def cmd_telegram_setup():
    """帮你把 chat_id 找出来。先给你的 bot 随便发条消息，再跑这个。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("先设好 TELEGRAM_BOT_TOKEN 环境变量（找 @BotFather 建 bot 拿）",
              file=sys.stderr)
        return 1
    try:
        data = fetch_json(f"https://api.telegram.org/bot{token}/getUpdates")
    except Exception as exc:                      # noqa: BLE001
        print(f"拿不到 getUpdates，token 可能不对: {exc}", file=sys.stderr)
        return 1
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            name = chat.get("username") or chat.get("title") or chat.get("first_name", "")
            chats[chat["id"]] = name
    if not chats:
        print("没看到任何消息。先在 Telegram 里给你的 bot 发一条（随便什么），再跑一次。",
              file=sys.stderr)
        return 1
    print("找到这些 chat_id：", file=sys.stderr)
    for cid, name in chats.items():
        print(f"  TELEGRAM_CHAT_ID = {cid}    ({name})", file=sys.stderr)
    if os.environ.get("TELEGRAM_CHAT_ID"):
        send_telegram(["<b>job-radar</b> 连通测试 ✅"])
    return 0


# --------------------------------------------------------------------------

def cmd_verify(config, sources, workers, wd_pages=1):
    n = sum(len(v or []) for v in (sources or {}).values())
    print(f"检查 {n} 个公司的 token …\n", file=sys.stderr)
    _, errors = collect(sources, workers=workers, verbose=(n <= 50),
                        workday_pages=wd_pages)
    print("", file=sys.stderr)
    if errors:
        print("以下需要修:", file=sys.stderr)
        for source, company, why in errors:
            print(f"  {source}/{company} — {why}", file=sys.stderr)
        return 1
    print("全部正常。", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--verify", action="store_true", help="只检查公司 token")
    ap.add_argument("--dry-run", action="store_true", help="不写 state")
    ap.add_argument("--all", action="store_true", help="忽略 state，输出全部命中")
    ap.add_argument("--telegram-setup", action="store_true",
                    help="找出你的 chat_id 并发一条测试消息")
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
    print(f"抓取 {n_co} 家公司 …", file=sys.stderr)
    jobs, errors = collect(sources, workers=workers, verbose=(n_co <= 50),
                           workday_pages=wd_pages)
    print(f"\n共 {len(jobs)} 个岗位，开始过滤", file=sys.stderr)

    jobs = apply_filters(jobs, config.get("filters") or {})
    print(f"过滤后 {len(jobs)} 个", file=sys.stderr)

    state = load_state(state_path)
    if args.all:
        fresh = jobs
    else:
        fresh = [j for j in jobs if j.key not in state]
    print(f"其中新出现 {len(fresh)} 个", file=sys.stderr)

    max_items = out_cfg.get("max_items")
    shown = fresh[:max_items] if max_items else fresh
    if len(fresh) > len(shown):
        print(f"（只展示前 {len(shown)} 个）", file=sys.stderr)

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
        # 只记实际展示出来的。被 max_items 截掉的留到明天继续推，
        # 否则一次涌进来 100+ 个的时候，没展示的那些就永远看不到了。
        for j in shown:
            state.setdefault(j.key, now)
        dropped = save_state(state_path, state,
                             state_cfg.get("forget_after_days", 120))
        if dropped:
            print(f"清理了 {dropped} 条过期记录", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
