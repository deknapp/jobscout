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
4. **Read those boards directly.** A posting on the employer's own ATS is the
   employer's own listing: they pay for the board, and a filled role comes off it.
5. **Filter in Python, not in the prompt.** Location, freshness, source and
   duplicates are decided by code the model cannot talk its way past.
6. **Verify every survivor.** Each remaining posting's page is fetched and
   checked: is this role real, is it open, and does the page's own location text
   still pass the filter?
7. **Score what's left** against your actual background, with the reasons stated.

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

## Install

```bash
git clone https://github.com/deknapp/jobscout.git
cd jobscout
python3 -m venv .venv && ./.venv/bin/pip install -e .
```

You need the [Claude Code CLI](https://claude.com/claude-code) installed and
logged in. jobscout shells out to `claude -p` in headless mode, so calls are
billed to the account you already have, and the hosted `WebSearch` / `WebFetch`
tools come with it — which is what makes live discovery possible at all. Each
agent runs in an empty temp directory with only those two tools allowed.

Prefer an API key? `JOBSCOUT_BACKEND=anthropic` with `ANTHROPIC_API_KEY` set,
plus `pip install -e '.[api]'`. `JOBSCOUT_BACKEND=mock` runs the whole pipeline
offline for free, which is how the test suite works.

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
| `JOBSCOUT_MAX_SCANS_PER_RUN` | 12 | one cheap call per board read |
| `JOBSCOUT_MAX_VERIFY_PER_RUN` | 20 | one cheap fetch per surviving posting |
| `JOBSCOUT_COMPANY_TARGET` | 30 | one strong call when the registry is short |
| `JOBSCOUT_RESCAN_AFTER_DAYS` | 3 | how often a known board is re-read |

Runs get cheaper over time: board resolution happens once per employer, and the
history stops the pipeline from re-verifying anything it has already ruled out.

## Layout

| File | What it does |
|---|---|
| `config.py` | settings, the location policy, and the personal-path guard |
| `corpus.py` | reads your applications folder (PDF, DOCX, text) |
| `agents.py` | the six prompts: profile, propose, resolve, scan, verify, rank |
| `sources.py` | which hosts count as a real posting |
| `filters.py` | the hard location / freshness / verification gates |
| `companies.py` | the employer registry that accumulates across runs |
| `history.py` | the append-only log that prevents repeats |
| `pipeline.py` | wires the stages together, with per-run budgets |
| `report.py` | the Markdown report, including what got filtered out and why |
| `llm.py` | claude-CLI / Anthropic-API / mock backends |

## Tests

```bash
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/python -m pytest
```

They run entirely offline against the mock backend — including a full pipeline
run over a realistic mix of postings (one good local role, one genuinely remote
role, one "remote" role fenced to another state, one from an aggregator, one
that is a year old) asserting that exactly the right two survive.

## Licence

MIT.
