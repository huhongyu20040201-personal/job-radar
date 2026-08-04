"""离线自测: 用假数据验证过滤和去重逻辑，不联网。"""
import json, sys, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jobradar

now = datetime.now(timezone.utc)
recent = (now - timedelta(days=2)).isoformat()
old = (now - timedelta(days=60)).isoformat()

FAKE = {
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": {
        "jobs": [
            {"id": 1, "title": "Software Engineer, Backend",
             "location": {"name": "San Francisco, CA"},
             "absolute_url": "https://x/1", "first_published": recent},
            {"id": 2, "title": "Engineering Manager",
             "location": {"name": "San Francisco, CA"},
             "absolute_url": "https://x/2", "first_published": recent},
            {"id": 3, "title": "Backend Engineer",
             "location": {"name": "Berlin"},
             "absolute_url": "https://x/3", "first_published": recent},
            {"id": 4, "title": "Software Engineer, Platform",
             "location": {"name": "Remote"},
             "absolute_url": "https://x/4", "first_published": old},
            {"id": 5, "title": "ML Engineer",
             "location": {"name": ""},
             "absolute_url": "https://x/5", "first_published": recent},
        ]
    },
    "https://api.lever.co/v0/postings/beta?mode=json": [
        {"id": "u1", "text": "Full Stack Engineer",
         "categories": {"location": "New York"},
         "hostedUrl": "https://y/1",
         "createdAt": int((now - timedelta(days=1)).timestamp() * 1000)},
    ],
}

jobradar.fetch_json = lambda url, retries=2: FAKE[url]

CFG = {
    "sources": {"greenhouse": ["acme"], "lever": ["beta"]},
    "filters": {
        "include_any": ["software engineer", "backend", "full stack",
                        r"re:\bML\b"],
        "exclude_any": ["manager", "intern"],
        "locations_any": ["remote", "san francisco", "new york"],
        "max_age_days": 14,
        "unknown_location": "keep",
    },
}

jobs, errors = jobradar.collect(CFG["sources"])
assert len(jobs) == 6, len(jobs)
assert not errors

kept = jobradar.apply_filters(jobs, CFG["filters"])
titles = sorted(j.title for j in kept)
print("过滤后:", titles)
assert titles == ["Full Stack Engineer", "ML Engineer",
                  "Software Engineer, Backend"], titles
# 2 被 exclude 掉, 3 地点不符, 4 太老, 5 地点为空但 keep

# 地点为空改成 drop
f2 = dict(CFG["filters"], unknown_location="drop")
assert len(jobradar.apply_filters(jobs, f2)) == 2

# 去重
with tempfile.TemporaryDirectory() as d:
    sp = Path(d) / "seen.json"
    state = jobradar.load_state(sp)
    assert state == {}
    fresh = [j for j in kept if j.key not in state]
    assert len(fresh) == 3
    ts = now.isoformat()
    for j in kept:
        state.setdefault(j.key, ts)
    jobradar.save_state(sp, state, 120)

    state2 = jobradar.load_state(sp)
    fresh2 = [j for j in kept if j.key not in state2]
    assert len(fresh2) == 0, "第二次跑应该没有新岗位"

    # 过期清理
    state2["greenhouse:acme:999"] = (now - timedelta(days=200)).isoformat()
    dropped = jobradar.save_state(sp, state2, 120)
    assert dropped == 1, dropped

# 经验年限：从描述里读最低要求
for text, want in [
    ("We require 5+ years of experience", 5),
    ("0-2 years of experience preferred", 0),
    ("2+ years of professional experience", 2),
    ("Minimum 3 to 5 years experience required", 3),
    ("You have 1-3 years of relevant experience", 1),
    ("Requires at least 2 years experience", 2),
    # 下面这些是在讲公司历史，不是经验要求，不能误判
    ("Figma was founded 5 years ago. No experience needed.", -1),
    ("Over the past 3 years our experience team grew", -1),
    ("In the last 10 years, experience has shown", -1),
    ("no numbers here", -1),
    ("", -1),
    (None, -1),
]:
    got = jobradar.extract_min_years(text)
    assert got == want, f"{text!r} -> {got}, 期望 {want}"

assert jobradar.strip_html("<p>Hi &amp; <b>bye</b></p>") == "Hi & bye"

# max_years_experience: 读不到年限(-1)的必须保留，超标的必须扔
exp_jobs = [
    jobradar.Job("a", "greenhouse", "c", "Software Engineer", "Remote", "u", "", min_years=-1),
    jobradar.Job("b", "greenhouse", "c", "Software Engineer", "Remote", "u", "", min_years=0),
    jobradar.Job("c", "greenhouse", "c", "Software Engineer", "Remote", "u", "", min_years=2),
    jobradar.Job("d", "greenhouse", "c", "Software Engineer", "Remote", "u", "", min_years=8),
]
got = {j.key for j in jobradar.apply_filters(
    exp_jobs, {"include_any": ["software engineer"], "max_years_experience": 1})}
assert got == {"a", "b"}, got

# Workday: 相对日期解析 + 翻页在不足一页时提前停
assert jobradar.parse_posted_on("Posted Today")[:4].isdigit()
assert jobradar.parse_posted_on("Posted 5 Days Ago")[:10] == \
    (now - timedelta(days=5)).date().isoformat()
assert jobradar.parse_posted_on("Posted Yesterday")[:10] == \
    (now - timedelta(days=1)).date().isoformat()
assert jobradar.parse_posted_on("Posted 30+ Days Ago")[:10] == \
    (now - timedelta(days=30)).date().isoformat()
assert jobradar.parse_posted_on("") == ""
assert jobradar.parse_posted_on(None) == ""

WD_CALLS = []


def fake_post(url, payload, retries=2):
    WD_CALLS.append(payload["offset"])
    return {"total": 3, "jobPostings": [
        {"title": "Software Engineer, New Grad", "externalPath": "/job/abc",
         "locationsText": "US, CA, Santa Clara", "postedOn": "Posted Today"},
    ]}


jobradar.post_json = fake_post
wd = jobradar.from_workday("x.wd5.myworkdayjobs.com|acme|Ext", pages=3)
assert len(wd) == 1, wd            # 只回 1 条 < 20，应该停在第一页
assert WD_CALLS == [0], WD_CALLS
assert wd[0].url == "https://x.wd5.myworkdayjobs.com/en-US/Ext/job/abc"
assert wd[0].key == "workday:acme:Ext:/job/abc"
assert wd[0].age_days == 0

# 渲染
md = jobradar.render_markdown(kept, [("greenhouse", "bad", "token 可能不对")])
assert "acme" in md and "bad" in md
print("\n--- markdown 预览 ---")
print(md)
print("\n全部通过 ✓")
