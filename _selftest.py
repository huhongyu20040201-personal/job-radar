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
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false": {
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

# 渲染
md = jobradar.render_markdown(kept, [("greenhouse", "bad", "token 可能不对")])
assert "acme" in md and "bad" in md
print("\n--- markdown 预览 ---")
print(md)
print("\n全部通过 ✓")
