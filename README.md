# job-radar

每天扫一遍 1200 家公司的招聘板，只告诉你**新出现**的应届/入门级岗位。
看完花 10 分钟投递。

不爬 LinkedIn / Indeed，只打各家 ATS 自己的公开 JSON 接口，所以不会被封号，
也不需要登录态和验证码。

当前配置：**1205 家公司**（`companies.yaml`），约 7.5 万个岗位，
按"应届生 + 湾区/美国远程"过滤后剩 130 个左右。抓完一轮约 60 秒。

## 已经配好的定时任务

Windows 计划任务 `job-radar`，每天 **11:00** 自动跑，日志写到 `run.log`。

```bash
schtasks /Query /TN "job-radar"      # 看下次运行时间
schtasks /Run   /TN "job-radar"      # 立刻手动跑一次
schtasks /Change /TN "job-radar" /ST 09:00   # 改时间
```

只在你登录 Windows 的状态下会跑（创建时没设开机密码）。

## 手动跑

```bash
python jobradar.py              # 每天跑，只出新的
python jobradar.py --verify     # 检查公司 token 是否还活着
python jobradar.py --all        # 忽略状态，输出所有命中
```

结果写在 `digest.md`。

## 三个命令

| 命令 | 用途 |
|---|---|
| `--verify` | 只检查公司 token，不输出岗位。加新公司时用 |
| `--all` | 忽略 `seen.json`，输出所有命中的。调过滤规则时用 |
| `--dry-run` | 跑但不写 `seen.json`。可以反复跑同样的结果 |

调规则的正确姿势是 `--all --dry-run` 组合，改一次 `config.yaml` 跑一次，
直到输出里没有明显噪音为止。

## 加公司

打开那家公司的招聘页，看地址栏：

| 地址长这样 | 填到 config 的 |
|---|---|
| `boards.greenhouse.io/stripe` | `greenhouse: [stripe]` |
| `jobs.lever.co/plaid` | `lever: [plaid]` |
| `jobs.ashbyhq.com/linear` | `ashby: [linear]` |
| `jobs.smartrecruiters.com/Acme` | `smartrecruiters: [Acme]` |

有些公司自建招聘页，但底下还是这几家 ATS —— 右键查看网页源码搜
`greenhouse` / `lever` / `ashby` 通常能找到。

填完跑 `--verify`，404 就是 token 不对。

## 挂到 GitHub Actions 上

`.github/workflows/daily.yml` 已经配好了，推到一个仓库就会每个工作日
早上自动跑，结果写进 `digest.md` 并显示在 Actions 的 Summary 页面。

想要推送到手机，在 config 里打开 telegram，然后在仓库
Settings → Secrets 里加 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。

**仓库设成 private。** `seen.json` 和 `digest.md` 会暴露你在看哪些公司。

## 每天只推 60 个，剩下的顺延

`output.max_items: 60` 是显示上限。**没展示出来的不会被记进 `seen.json`**，
所以会留到第二天继续推 —— 一次涌进来 100 多个也不会漏。
第一天有 137 个积压，大概三天推完。

## ⭐ 是什么

标题里明确写着 new grad / entry level / Engineer I / L3 之类的，会标 ⭐ 排在最前面。
其余的是标题看不出资历的工程岗（通常写着 "Software Engineer"），
这类里混着 3-5 年经验的岗位，标题过滤不掉，得你自己点进去看。

## 两个会踩的坑

**第一版一定会很吵。** 头几天的时间基本都花在往 `exclude_any` 里加词，
而不是写代码。`staff` / `principal` / `manager` 这类是大头，另外注意
`exclude_any` 是子串匹配 —— 加 `staff` 会连 `Staffing Coordinator` 一起干掉，
一般这正是你想要的。

**有些板子会刷新老岗位的时间戳。** `max_age_days` 用的是发布时间，
但去重靠的是 job id，所以一个岗位不会被重复推送 —— 除非公司把它删了重发，
那种情况没办法。

## 加新的数据源

在 `jobradar.py` 里照着 `from_greenhouse` 写一个函数，返回 `Job` 列表，
注册到 `ADAPTERS` 就行。`key` 必须全局唯一，格式 `来源:公司:岗位id`。

## 自测

```bash
python _selftest.py    # 离线跑，不联网，验证过滤和去重逻辑
```
