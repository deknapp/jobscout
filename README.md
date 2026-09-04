# jobscout

An agentic job finder that starts from the applications you have **already
written**, works out who else would want you, reads those employers' own job
boards, and enforces your location constraint in code rather than in a prompt.

It is built for the situation where a job search has gone on long enough that
the generic advice has stopped helping: you have a folder of tailored resumes
and cover letters, you know roughly what you are aiming at, and what you
actually need is a short list of *real, open, reachable* roles you have not
already seen.

```
$ jobscout find

reading 18 document(s) from your applications folder…
proposing 20 employer(s) that fit your background and location…
finding careers boards for 10 employer(s)…
reading 12 job board(s)…
found 47 raw posting(s) across 12 board(s)
verifying 9 posting(s) by fetching each listing…
scoring 6 surviving role(s)…
```

---

## Why not just search a job site

Because the open web's job layer is mostly noise. Scrapers republish listings
that closed months ago, staffing mills post phantom roles to harvest resumes,
and half of what an LLM returns when you ask it for jobs is a plausible-looking
URL it made up. Worse, "remote" frequently means *remote if you live in
California* — which is useless if you don't, and no amount of prompt-engineering
reliably stops a model from waving that through.

jobscout is built the other way round:

1. **Read what you have already written.** Your resumes, cover letters and the
   job descriptions you targeted say more about what you want than any form you
   would fill in.
2. **Decide who to ask.** An agent proposes real employers who plausibly want
   this background *and* satisfy your location rule — big local institutions,
   remote-first companies in your domain, places whose work matches what makes
   you unusual.
3. **Find each employer's real board, once.** Greenhouse, Lever, Ashby, Workday,
   SmartRecruiters, iCIMS, or a `.gov`/`.edu` careers page. That URL is cached
   forever, so the expensive step happens one time per employer.
4. **Read those boards directly — usually without a model at all.** Greenhouse,
   Lever, Ashby, SmartRecruiters and Workable all publish their listings as free
   public JSON, so jobscout asks the API. See below: this is the difference
   between the tool working and not.
5. **Filter in Python, not in the prompt.** Location, freshness, source and
   duplicates are decided by code the model cannot talk its way past.
6. **Verify every survivor.** Each remaining posting's page is fetched and
   checked: is this role real, is it open, and does the page's own location text
   still pass the filter?
7. **Score what's left** on three separate axes — fit, likelihood and recency —
   and report the blend as a percentile against everything it has ever scored
   for you.

## Reading boards without a model

The first live run read ten job boards and found **one** posting, for four
dollars. The cause was structural, not a prompt problem: Greenhouse, Lever,
Ashby, Workday and iCIMS boards are JavaScript applications. Fetching one
returns an empty shell, so the agent looked at the shell and — correctly, and
without inventing anything — reported an empty board.

But every one of those systems publishes the same listings as free, public JSON:

```
Greenhouse       boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
Lever            api.lever.co/v0/postings/<slug>?mode=json
Ashby            api.ashbyhq.com/posting-api/job-board/<slug>
SmartRecruiters  api.smartrecruiters.com/v1/companies/<slug>/postings
Workable         apply.workable.com/api/v1/widget/accounts/<slug>
Workday          <tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
```

Workday needs a little more care, and gets it. Its boards run to thousands of
roles, so jobscout drives the board's own search with your target titles rather
than paging the whole thing. It reports dates as "Posted 30+ Days Ago", which are
converted to real dates — and "30+" resolves to 31, deliberately just past the
default freshness limit, because a board that has stopped counting is telling you
something. And when it collapses a role's location to "3 Locations", jobscout
opens that role individually, since one of those three could be the only one that
matters to you.

So jobscout asks the API instead. This is better in every direction that
matters. It is free and instant. It returns the **complete** board rather than
whatever survived one page fetch. The dates and location strings are the
employer's own fields rather than a model's reading of them. And a hallucinated
job stops being unlikely and becomes *impossible*, because nothing is generated.

The agent-driven scan is still there, for in-house and government boards with no
API. It is the fallback now, not the default — and board resolution actively
hunts for the ATS **behind** a marketing careers page rather than settling for
the page.

An API hands back the whole board, so two free filters run right at the source,
before anything costs money: the hard location gate, and — only when a board
still has more than 25 in-area roles — a crude title-overlap trim. Below that
cap nothing is trimmed, because an unusual title is exactly what token overlap
throws away by mistake.

## The hard location filter

This is the part most job tools get wrong, so it is the part with the most tests.

| Posting says | Verdict (policy: NM, remote OK) |
|---|---|
| `Albuquerque, NM` | ✅ in an accepted location |
| `Santa Fe, NM (hybrid, 2 days in office)` | ✅ hybrid is fine *here* |
| `Remote - US` | ✅ genuinely remote |
| `Remote (must reside in California)` | ❌ remote in name only |
| `Remote — open to candidates in TX and CO` | ❌ fenced to states you don't live in |
| `Remote (US) — TX, CO and New Mexico` | ✅ the fence includes you |
| `Hybrid - Austin, TX (3 days in office)` | ❌ you are not relocating |
| `Denver, CO` | ❌ close, but no |
| *(no location given)* | ❌ unknown ≠ acceptable |

The default is **reject**. A false accept costs you an afternoon writing an
application you can't take; a false reject costs you one listing out of many.

The agents are told the rule too — an agent that knows the constraint wastes
fewer searches — but they are never trusted to apply it. Every candidate is
re-checked in `filters.py` after discovery, and checked *again* against the
live page's own wording after verification.

## Sources it will and won't trust

**Trusted:** applicant-tracking boards (Greenhouse, Lever, Ashby, Workday,
SmartRecruiters, iCIMS, Workable, Jobvite, …), the employer's own careers
domain, and `.gov` / `.mil` / `.edu` sites.

**Denied by host:** Indeed, LinkedIn, ZipRecruiter, Glassdoor, Dice, Monster,
Talent.com, Adzuna, Lensa, Builtin, Wellfound, WeWorkRemotely, RemoteOK, Jobot,
Robert Half, TEKsystems, Insight Global and the rest of the aggregator,
staffing-mill and SEO-farm layer. An unrecognised host is rejected as well, not
merely flagged.

## Never recommending the same thing twice

Every posting the pipeline *evaluates* goes into an append-only history file, not
just the ones it recommends — so a later run neither repeats a recommendation nor
pays to re-check a rejection. Rejections come in two kinds:

* **permanent** (wrong state, too old, employer excluded) — suppressed forever
* **transient** (a page that wouldn't load) — retried after a week

Roles that were verified and in-location but simply didn't fit in this run's
report are *deliberately not recorded*, so tomorrow's run can still surface them.

## Your personal information never enters this repository

The repo is public. Your application materials are not part of it and cannot
become part of it:

* Your applications live in a folder **you** point at (`JOBSCOUT_APPLICATIONS_DIR`).
* Everything jobscout learns — profile, employer registry, history, reports —
  is written to a data dir **outside** the repo (`JOBSCOUT_DATA_DIR`, default `~/.jobscout`).
* `config.py` **refuses to start** if either path resolves inside the repo.
* `.gitignore` covers `.env`, every document format, and every state file.
* `tests/test_privacy.py` fails the build if a `.env` or a PDF is ever tracked,
  or if a personal path is hardcoded anywhere in the source.

Nothing about any particular person is baked into the code. The same clone works
for anyone.

## Ranking: fit, likelihood, recency

Most job tools give you one number. That number quietly conflates two different
questions, and the answers often point opposite ways:

* **fit** — how well your background matches what the role asks for
* **likelihood** — your realistic chance of actually *getting* it, weighing how
  many people will apply, whether you clear hard gates (citizenship, clearance,
  licence, degree), whether you are local to a role that prefers local
  candidates, and whether you have a warm signal already
* **recency** — a job posted three days ago is meaningfully more gettable than
  the same job posted three weeks ago

A perfect-fit staff role at a famous employer with 800 applicants can be a 95
fit and a 12 likelihood. A merely-good role where you clear a gate most
applicants do not can be a 72 fit and a 78 likelihood — and it is the better
use of your afternoon. jobscout scores both, and asks the model to be realistic
rather than kind.

Recency decays **exponentially** (half-life 14 days by default) rather than
linearly, because that is how a requisition actually ages: fast at first, then
it barely matters whether it has been open 40 days or 60.

The blend is then reported as a **percentile** against every role jobscout has
ever scored for you, because "88th percentile" answers the question you are
actually asking — *is this better than what usually crosses my desk?* — which a
bare score out of 100 does not. Weights are configurable, and the web app turns
them into sliders.

## The web app

```bash
jobscout serve
```

Opens `http://127.0.0.1:8765` — a local board you work through, rather than a
wall of terminal text you re-read:

* roles ranked by percentile, each with its fit / likelihood / recency breakdown
* **weight sliders that re-rank the whole board instantly**, with no model calls:
  the component scores are already stored, so this costs nothing
* filter by status, work mode or text; mark a role **applied** or **dismissed**
  and it writes straight to the history, so the next run stops offering it
* start a run from the page and watch the log as it happens
* the employer registry, with a link to each board jobscout found

It is built on the Python standard library — no framework, no CDN, no external
requests of any kind — because the page is looking at your job search and that
should not leave your machine. Everything it displays comes from your data dir.

## Install

```bash
git clone https://github.com/deknapp/jobscout.git
cd jobscout
python3 -m venv .venv
./.venv/bin/pip install -e .
```

Python 3.9 or newer. The only hard dependencies are `pypdf` and `python-docx`,
for reading your application materials.

## Connecting Claude

jobscout needs a model that can search and fetch the web. There are two ways to
give it one, plus an offline mode for trying it out.

### Option 1 — your Claude Code login (recommended, no API key)

```bash
JOBSCOUT_BACKEND=cli          # this is the default
```

jobscout shells out to `claude -p` in headless mode, so calls are billed to the
Claude account you are already logged into, and the hosted `WebSearch` and
`WebFetch` tools come with it — which is what makes live job discovery possible
at all.

```bash
npm install -g @anthropic-ai/claude-code   # or see claude.com/claude-code
claude                                     # run once, log in, then quit
claude --version                           # confirm it is on your PATH
```

That is the whole setup. Each agent runs in an empty temporary directory with
**only** `WebSearch` and `WebFetch` allowed — it cannot read your files, your
repo or your home directory. Headless mode denies any tool not on that list.

### Option 2 — an Anthropic API key

If you would rather bill an API account, or the CLI is not available on the
machine:

```bash
./.venv/bin/pip install -e '.[api]'
```

Get a key from [console.anthropic.com](https://console.anthropic.com/settings/keys)
(**Settings → API keys → Create key**), then put it in your git-ignored `.env`
alongside the rest of your configuration:

```ini
JOBSCOUT_BACKEND=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

The key is read from the environment only, is never written anywhere by
jobscout, and `.env` is git-ignored (and blocked by the pre-commit hook).

Web search on this path uses Anthropic's server-side `web_search` tool, which is
billed per search on top of tokens. `WebFetch` is not available through the API
in the same form, so verification is weaker here than on the CLI path — the CLI
backend is genuinely the better one, not just the cheaper one.

### Which model

Two tiers, both configurable:

```ini
JOBSCOUT_MODEL_CHEAP=claude-haiku-4-5    # board scans, verification, board lookup
JOBSCOUT_MODEL_STRONG=claude-opus-5      # profile, employer proposals, ranking
```

The cheap tier does the high-volume mechanical work (read this board, fetch this
page). The strong tier does the three judgement calls: who you are, who would
want you, and how good each role really is.

### Option 3 — offline

```bash
JOBSCOUT_BACKEND=mock
```

Runs the entire pipeline against canned responses, at zero cost and with no
network. This is how the test suite works, and it is a good way to see the shape
of the thing before spending anything.

## Use

```bash
# one-time setup: point it at your applications and state your constraint
jobscout init --applications "~/path/to/your applications" \
              --states NM \
              --cities "Albuquerque,Santa Fe,Los Alamos,Rio Rancho,Las Cruces"

jobscout status            # what it can read, and what the filters are set to
jobscout profile           # the profile it inferred from your materials
jobscout find              # the actual run: prints a report, saves a copy

jobscout companies                    # the employer registry it has built up
jobscout companies --add "Some Lab"   # add one it missed
jobscout companies --ignore "Acme"    # never suggest this employer again

jobscout serve             # the local web app: the board, sliders, run button

jobscout history                      # what it has already shown you
jobscout mark a1b2c3d4e5f6 --applied  # so it stops offering that one
```

`jobscout find` prints Markdown to stdout and progress to stderr, so
`jobscout find > today.md` gives you a clean file.

## Cost

Every model call is capped per run, and the caps are the dial between a cheap
run and a thorough one:

| Setting | Default | What it costs |
|---|---:|---|
| `JOBSCOUT_MAX_RESOLVE_PER_RUN` | 10 | one cheap web-search call per new employer, once ever |
| `JOBSCOUT_MAX_SCANS_PER_RUN` | 12 | **free** for an ATS board; one cheap call only for boards with no API |
| `JOBSCOUT_MAX_VERIFY_PER_RUN` | 20 | one cheap fetch per surviving posting |
| `JOBSCOUT_COMPANY_TARGET` | 30 | one strong call when the registry is short |
| `JOBSCOUT_RESCAN_AFTER_DAYS` | 3 | how often a known board is re-read |

Ranking weights are free to change — they operate on scores already stored:

| Setting | Default |
|---|---:|
| `JOBSCOUT_WEIGHT_FIT` | 0.45 |
| `JOBSCOUT_WEIGHT_LIKELIHOOD` | 0.30 |
| `JOBSCOUT_WEIGHT_RECENCY` | 0.25 |
| `JOBSCOUT_RECENCY_HALFLIFE_DAYS` | 14 |

Runs get cheaper over time: board resolution happens once per employer, and the
history stops the pipeline from re-verifying anything it has already ruled out.

## Layout

| File | What it does |
|---|---|
| `config.py` | settings, the location policy, and the personal-path guard |
| `corpus.py` | reads your applications folder (PDF, DOCX, text) |
| `agents.py` | the six prompts: profile, propose, resolve, scan, verify, rank |
| `sources.py` | which hosts count as a real posting |
| `fetchers.py` | reads Greenhouse/Lever/Ashby/SmartRecruiters/Workable boards from their JSON APIs |
| `filters.py` | the hard location / freshness / verification gates |
| `companies.py` | the employer registry that accumulates across runs |
| `history.py` | the append-only log that prevents repeats |
| `scoring.py` | fit / likelihood / recency blend, and percentile ranking |
| `board.py` | the persistent working list the web app reads |
| `web.py` + `static/` | the local web app (standard library only) |
| `pipeline.py` | wires the stages together, with per-run budgets |
| `report.py` | the Markdown report, including what got filtered out and why |
| `llm.py` | claude-CLI / Anthropic-API / mock backends |

## Tests

```bash
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/python -m pytest
```

86 tests, all offline against the mock backend — the fetcher tests parse
recorded API shapes and the pipeline test stubs the network out entirely. They include a full pipeline run
over a realistic mix of postings — one good local role, one genuinely remote
role, one "remote" role fenced to another state, one from an aggregator, one
that is a year old — asserting that exactly the right two survive; the location
table above, case by case; and the ranking, including that a high-fit lottery
ticket loses to a realistic bet.

## Licence

MIT.
