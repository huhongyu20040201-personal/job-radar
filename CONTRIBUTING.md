# Contributing to job-radar

Thanks for taking the time to look at this project. Contributions are welcome, from a
one-line typo fix to a new ATS adapter.

## How changes get into `main`

- Nobody except the maintainer can push to this repository directly.
- To change something, **fork the repo, make your change on a branch in your fork, and
  open a pull request**. Every PR needs the maintainer's review and approval before it
  can be merged.
- If you just want the tool to behave differently for *you* (other roles, other cities),
  you don't need a PR at all. Fork it, edit `config.yaml`, and run it from your fork.
  See [Setup](README.md#setup).

## Pull request guidelines

### Title: say what the PR does

Write the title as a short, imperative summary of the change, under about 70 characters.
Someone skimming the PR list should know what changed without opening it.

| Good | Not so good |
|---|---|
| `Add retry with backoff for Workday HTTP 429s` | `Update jobradar.py` |
| `Fix Lever postings with no location being dropped` | `bug fix` |
| `Add 12 Bay Area startups to companies.yaml` | `more companies` |

### Description: what, why, and how you tested it

The PR template asks for three things:

1. **What** changed.
2. **Why**: the problem it solves, or a link to the issue.
3. **How you tested it**: at minimum, that `python _selftest.py` passes. For fetching
   changes, paste the relevant output of a real run.

### Code: leave comments that explain *why*

- Add comments wherever the reason for the code isn't obvious from reading it. Explain
  the *why* ("Workday switches to relevance sort when searchText is set, so leave it
  empty"), not the *what* ("set searchText to empty").
- Match the style of the surrounding code: plain Python, standard library only
  (plus `pyyaml`), small functions, English comments.
- New filtering or parsing logic should come with a case in `_selftest.py`. The tests run
  offline against fake API responses, so they need no network access.

### Scope: one change per PR

- Keep each PR focused on one thing. Two unrelated fixes make two PRs.
- Don't mix in unrelated reformatting or renames. It makes the real change hard to review.

## What's likely to be accepted

- **New companies** in `companies.yaml`. Run `python jobradar.py --verify` first and make
  sure your additions resolve.
- **Bug fixes** and **new ATS adapters**. See
  [Adding a data source](README.md#adding-a-data-source).
- **Reliability improvements**, such as retries, rate-limit handling, and clearer errors.
- **Documentation fixes.**

## What's likely to be declined

- Changing the default filters in `config.yaml` to match your own search (a different
  city or seniority). Those defaults are the maintainer's. Keep them in your fork.
- Anything that scrapes sites that forbid it, needs a login, or submits applications
  automatically. This tool only reads public job-board APIs, and applying stays manual.
- Committing `seen.json`, `digest.md`, or any tokens or secrets.

## Be kind

Review comments are about the code, not the person. Please keep issues and PRs
respectful. Discussions that aren't will be closed.
