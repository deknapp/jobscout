"""Reading ATS boards straight from their public JSON, with no model involved.

These parse recorded API shapes; nothing here touches the network.
"""
import datetime as dt

import pytest

from jobscout import fetchers

GREENHOUSE = {"jobs": [
    {"title": "Senior Data Engineer",
     "location": {"name": "Albuquerque, NM"},
     "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
     "updated_at": "2026-08-30T17:04:11-04:00",
     "content": "&lt;p&gt;Own the &lt;b&gt;data platform&lt;/b&gt;.&lt;/p&gt;"},
]}

LEVER = [
    {"text": "Platform Engineer",
     "categories": {"location": "Santa Fe, NM"},
     "workplaceType": "remote",
     "hostedUrl": "https://jobs.lever.co/acme/abc",
     "createdAt": 1788242400000,
     "descriptionPlain": "Build the deployment platform."},
]

ASHBY = {"jobs": [
    {"title": "Research Engineer", "location": "Remote - US",
     "secondaryLocations": [{"location": "Austin"}],
     "isRemote": True, "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/acme/xyz",
     "publishedAt": "2026-09-01T00:00:00.000Z",
     "descriptionPlain": "Applied research."},
    {"title": "Hidden role", "location": "Nowhere", "isListed": False},
]}

SMART = {"content": [
    {"id": "744000137413079", "name": "Analytics Engineer",
     "location": {"fullLocation": "Albuquerque, NM, US", "remote": False},
     "releasedDate": "2026-08-28T09:50:21.127Z"},
]}


def _patch(monkeypatch, payload):
    monkeypatch.setattr(fetchers, "_get_json", lambda url: payload)


def test_greenhouse(monkeypatch):
    _patch(monkeypatch, GREENHOUSE)
    result = fetchers.fetch("Acme", "https://boards.greenhouse.io/acme")
    assert result.ok and result.ats == "Greenhouse"
    job = result.postings[0]
    assert job.title == "Senior Data Engineer"
    assert job.location == "Albuquerque, NM"
    assert job.posted == "2026-08-30"
    assert "data platform" in job.summary  # HTML stripped and unescaped
    assert "<" not in job.summary


def test_lever_epoch_millis_and_workplace_type(monkeypatch):
    _patch(monkeypatch, LEVER)
    result = fetchers.fetch("Acme", "https://jobs.lever.co/acme")
    job = result.postings[0]
    assert job.posted == dt.date(2026, 9, 1).isoformat()
    # The workplace type is folded into the location so the filter can see it.
    assert job.location == "Santa Fe, NM (remote)"


def test_ashby_skips_unlisted_and_keeps_secondary_locations(monkeypatch):
    _patch(monkeypatch, ASHBY)
    result = fetchers.fetch("Acme", "https://jobs.ashbyhq.com/acme")
    assert len(result.postings) == 1
    assert "Remote - US" in result.postings[0].location
    assert "Austin" in result.postings[0].location


def test_smartrecruiters_builds_the_posting_url(monkeypatch):
    _patch(monkeypatch, SMART)
    result = fetchers.fetch("Acme", "https://jobs.smartrecruiters.com/acme")
    assert result.postings[0].url.endswith("/acme/744000137413079")


def test_an_unsupported_host_returns_none_so_the_agent_takes_over():
    assert fetchers.fetch("Acme", "https://careers.acme.com/jobs") is None
    assert not fetchers.supports("https://careers.acme.com/jobs")
    assert fetchers.supports("https://boards.greenhouse.io/acme")


def test_a_broken_api_reports_instead_of_raising(monkeypatch):
    def boom(url):
        raise OSError("connection reset")

    monkeypatch.setattr(fetchers, "_get_json", boom)
    result = fetchers.fetch("Acme", "https://jobs.lever.co/acme")
    assert result is not None and not result.ok
    assert "unreachable" in result.note


# --- Workday ---------------------------------------------------------------

WORKDAY_SEARCH = {"total": 2, "jobPostings": [
    {"title": "Senior Data Engineer", "locationsText": "Albuquerque, NM",
     "externalPath": "/job/Albuquerque-NM/Senior-Data-Engineer_R-1",
     "postedOn": "Posted 4 Days Ago"},
    {"title": "Software Engineer", "locationsText": "3 Locations",
     "externalPath": "/job/Elsewhere/Software-Engineer_R-2",
     "postedOn": "Posted 30+ Days Ago"},
]}

WORKDAY_DETAIL = {"jobPostingInfo": {
    "title": "Software Engineer",
    "location": "Los Alamos, NM",
    "additionalLocations": ["Denver, CO"],
    "startDate": "2026-08-29",
    "externalUrl": "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/x",
    "jobDescription": "<p>Build things.</p>",
}}


def test_workday_urls_are_parsed_into_a_cxs_endpoint():
    from jobscout.fetchers import _workday_parts

    assert _workday_parts("https://leidos.wd5.myworkdayjobs.com/External") == (
        "leidos.wd5.myworkdayjobs.com", "leidos", "External")
    # The locale segment is not the site name.
    assert _workday_parts("https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/1")[2] == "Careers"


@pytest.mark.parametrize("text,days_ago", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 12 Days Ago", 12),
    ("Posted 30+ Days Ago", 31), ("Posted 2 Months Ago", 60),
    ("Posted 1 Year Ago", 365),
])
def test_workday_relative_dates_become_real_ones(text, days_ago):
    from jobscout.fetchers import _relative_date

    today = dt.date(2026, 9, 4)
    assert _relative_date(text, today) == (today - dt.timedelta(days=days_ago)).isoformat()


def test_workday_resolves_a_multi_location_posting(monkeypatch):
    """"3 Locations" can be hiding the only location that matters."""
    monkeypatch.setattr(fetchers, "_post_json", lambda url, body: WORKDAY_SEARCH)
    monkeypatch.setattr(fetchers, "_get_json", lambda url: WORKDAY_DETAIL)

    result = fetchers.fetch("Acme", "https://acme.wd5.myworkdayjobs.com/Careers",
                            context={"titles": ["Data Engineer"]})
    by_title = {p.title: p for p in result.postings}

    assert by_title["Senior Data Engineer"].location == "Albuquerque, NM"
    resolved = by_title["Software Engineer"]
    assert "Los Alamos, NM" in resolved.location
    assert resolved.posted == "2026-08-29"      # the real date beats "30+ days"
    assert "Build things" in resolved.summary


def test_a_bad_board_url_already_on_disk_is_healed_when_loaded(tmp_path):
    """The repair has to run on read, not only at resolution.

    A malformed URL written by an older run would otherwise sit in the registry
    forever, quietly 404ing and falling back to a slow agent scan every time.
    """
    import json

    from jobscout.companies import Registry

    path = tmp_path / "companies.json"
    path.write_text(json.dumps({"companies": [
        {"name": "Descartes Labs", "careers_url": "https://jobs.lever.co/descarteslabs.com",
         "status": "resolved"}]}), encoding="utf-8")

    company = Registry(path).get("Descartes Labs")
    assert company.careers_url == "https://jobs.lever.co/descarteslabs"


# --- iCIMS -----------------------------------------------------------------

ICIMS_PAGE = '''
<div class="iCIMS_JobsTable">
  <a href="https://careers-x.icims.com/jobs/2441/accounts-payable-specialist/job?in_iframe=1"
     class="iCIMS_Anchor" title="2441 - Accounts Payable Specialist">
     <h3 > Accounts Payable Specialist</h3></a>
  <div class="col-xs-12 description">Process &amp; reconcile invoices.</div>
  <div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">
    <span class="glyphicons glyphicons-map-marker"></span></dt>
    <dd class="iCIMS_JobHeaderData"><span > US-NM-Albuquerque</span></dd></div>

  <a href="https://careers-x.icims.com/jobs/2502/research-software-engineer/job?in_iframe=1"
     class="iCIMS_Anchor" title="2502 - Research Software Engineer">
     <h3 > Research Software Engineer</h3></a>
  <div class="col-xs-12 description">Build analysis pipelines.</div>
  <div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">
    <span class="glyphicons glyphicons-map-marker"></span></dt>
    <dd class="iCIMS_JobHeaderData"><span > US-NM-Los Alamos</span></dd></div>
</div>
'''


def test_icims_is_parsed_out_of_server_rendered_html(monkeypatch):
    """iCIMS publishes no JSON, but unlike the SPA boards the jobs are really
    in the HTML, so they can be read without a model."""
    pages = [ICIMS_PAGE, ""]
    monkeypatch.setattr(fetchers, "_get_html", lambda url: pages.pop(0) if pages else "")

    result = fetchers.fetch("Lovelace", "https://careers-x.icims.com/jobs/intro",
                            context={})
    assert result.ats == "iCIMS"
    assert len(result.postings) == 2

    first, second = result.postings
    assert first.title == "Accounts Payable Specialist"   # the "2441 - " prefix goes
    assert first.location == "Albuquerque, NM"            # US-NM-Albuquerque decoded
    assert "reconcile invoices" in first.summary
    assert "?" not in first.url                           # the iframe param is dropped
    assert second.location == "Los Alamos, NM"


# --- finding the ATS behind a vanity careers domain ------------------------

def test_a_vanity_careers_domain_is_followed_to_its_real_board(monkeypatch):
    """jobs.<company>.com is usually a wrapper. NVIDIA's is a Workday board."""
    page = ('<a href="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/login">'
            'Search jobs</a>')

    class Response:
        def geturl(self): return "https://jobs.nvidia.com/careers"
        def read(self, *a): return page.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", lambda *a, **k: Response())
    found = fetchers.discover_ats("https://jobs.nvidia.com/")
    # The deep link is trimmed back to the board itself.
    assert found == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    assert fetchers.supports(found)


def test_a_careers_page_that_redirects_straight_to_an_ats(monkeypatch):
    """ARA's careers domain simply forwards to UltiPro."""
    class Response:
        def geturl(self):
            return ("https://recruiting.ultipro.com/APP1010ARAI/JobBoard/"
                    "07442cec-d18e-4589-ab15-8342edc29af7")
        def read(self, *a): return b""
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", lambda *a, **k: Response())
    found = fetchers.discover_ats("https://careers.ara.com/")
    assert fetchers.supports(found) and "ultipro" in found


def test_a_page_with_no_ats_behind_it_says_so(monkeypatch):
    class Response:
        def geturl(self): return "https://rs21.io/careers"
        def read(self, *a): return b"<p>Email us your resume.</p>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", lambda *a, **k: Response())
    assert fetchers.discover_ats("https://rs21.io/careers") == ""


ULTIPRO = {"totalCount": 2, "opportunities": [
    {"Id": 111, "Title": "Structural Engineer",
     "Locations": [{"LocalizedDescription": "NM01 - Albuquerque"}],
     "PostedDate": "2026-09-03T19:09:22.346Z", "BriefDescription": "<p>Build.</p>"},
    {"Id": 222, "Title": "Senior Project Manager", "JobLocationType": "Remote",
     "Locations": [], "PostedDate": "2026-09-01T00:00:00.000Z"},
]}


def test_ultipro_boards_are_read_directly(monkeypatch):
    monkeypatch.setattr(fetchers, "_post_json", lambda url, body: ULTIPRO)
    result = fetchers.fetch(
        "ARA", "https://recruiting.ultipro.com/APP1010ARAI/JobBoard/"
               "07442cec-d18e-4589-ab15-8342edc29af7", context={})
    assert result.ats == "UltiPro" and len(result.postings) == 2
    first, second = result.postings
    assert first.location == "NM01 - Albuquerque"   # the state code is still findable
    assert first.posted == "2026-09-03"
    assert "opportunityId=111" in first.url
    assert second.location == "Remote"


BREEZY = [{"name": "Research Engineer",
           "url": "https://acme.breezy.hr/p/abc-research-engineer",
           "published_date": "2026-08-30T14:37:22.684Z",
           "location": {"city": "Albuquerque", "state": {"id": "NM"},
                        "country": {"name": "United States"}, "is_remote": False},
           "salary": "$150,000 - $180,000"}]

RIPPLING = [{"uuid": "abc", "name": "Platform Engineer",
             "url": "https://ats.rippling.com/acme/jobs/abc",
             "workLocation": {"label": "Santa Fe, NM"}}]


def test_breezy(monkeypatch):
    monkeypatch.setattr(fetchers, "_get_json", lambda url: BREEZY)
    job = fetchers.fetch("Acme", "https://acme.breezy.hr/", context={}).postings[0]
    assert job.location == "Albuquerque, NM, United States"
    assert job.posted == "2026-08-30"
    assert job.salary == "$150,000 - $180,000"


def test_rippling_has_no_dates_and_says_so(monkeypatch):
    """Its board API carries none, so the undated-posting rule decides."""
    monkeypatch.setattr(fetchers, "_get_json", lambda url: RIPPLING)
    job = fetchers.fetch("Acme", "https://ats.rippling.com/acme/jobs",
                         context={}).postings[0]
    assert job.location == "Santa Fe, NM"
    assert job.posted == ""


# --- .jobs sites: sitemap first, page fetches only for survivors ------------

DOT_JOBS_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex><sitemap><loc>https://acme.jobs/sitemaps/jobs_1.xml</loc></sitemap>
<sitemap><loc>https://acme.jobs/sitemaps/pages.xml</loc></sitemap></sitemapindex>"""

# One with the place in the path, one without — the two shapes seen in the wild.
DOT_JOBS_LIST = """<?xml version="1.0" encoding="UTF-8"?><urlset>
<url><loc>https://acme.jobs/springfield-nm/senior-data-engineer/AB12CD34EF56/job/</loc>
     <lastmod>2026-09-01</lastmod></url>
<url><loc>https://acme.jobs/springfield-nm/warehouse-forklift-operator/99AA88BB77CC/job/</loc>
     <lastmod>2026-09-01</lastmod></url>
<url><loc>https://acme.jobs/springfield-nm/retired-data-engineer/1122334455/job/</loc>
     <lastmod>2020-01-01</lastmod></url>
<url><loc>https://acme.jobs/search/jobdetails/staff-data-engineer/4f22308e-1ca4-4eb6-a99d-752a2adda8c4</loc>
     <lastmod>2026-09-02</lastmod></url>
</urlset>"""

DOT_JOBS_DETAIL = """<html><head>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Staff Data Engineer",
 "datePosted": "9/2/2026", "description": "<p>Own the <b>pipelines</b>.</p>",
 "jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress",
   "addressLocality": "Springfield", "addressRegion": "New Mexico"}}]}
</script></head><body></body></html>"""


def _patch_dot_jobs(monkeypatch, detail=DOT_JOBS_DETAIL):
    pages = {
        "https://acme.jobs/sitemap.xml": DOT_JOBS_INDEX,
        "https://acme.jobs/sitemaps/jobs_1.xml": DOT_JOBS_LIST,
    }
    fetched = []

    def fake(url):
        fetched.append(url)
        if url in pages:
            return pages[url]
        if url.startswith("https://acme.jobs/search/jobdetails/"):
            return detail
        raise AssertionError("unexpected fetch: %s" % url)

    monkeypatch.setattr(fetchers, "_get_html", fake)
    return fetched


def test_a_dot_jobs_board_is_read_from_its_sitemap(monkeypatch):
    """The employer's own careers page is a JS shell; the sitemap is not."""
    fetched = _patch_dot_jobs(monkeypatch)
    result = fetchers.fetch("Acme Labs", "https://acme.jobs/",
                            {"max_age_days": 30})
    assert result.ok
    assert result.ats == ".jobs"
    titles = sorted(p.title for p in result.postings)
    assert titles == ["Senior Data Engineer", "Staff Data Engineer",
                      "Warehouse Forklift Operator"]
    # The stale one is dropped from the sitemap's own lastmod, for free.
    assert "Retired Data Engineer" not in titles


def test_a_place_in_the_path_costs_no_request(monkeypatch):
    """Only roles whose URL does not name a location are worth fetching."""
    fetched = _patch_dot_jobs(monkeypatch)
    result = fetchers.fetch("Acme Labs", "https://acme.jobs/",
                            {"max_age_days": 30})
    detail_fetches = [u for u in fetched if "/jobdetails/" in u]
    assert len(detail_fetches) == 1, "one role lacked a location in its path"

    by_title = {p.title: p for p in result.postings}
    assert by_title["Senior Data Engineer"].location == "Springfield, NM"
    assert by_title["Senior Data Engineer"].posted == "2026-09-01"
    # The fetched one gets its location and date from schema.org JSON-LD.
    assert by_title["Staff Data Engineer"].location == "Springfield, New Mexico"
    assert by_title["Staff Data Engineer"].posted == "2026-09-02"
    assert "pipelines" in by_title["Staff Data Engineer"].summary


def test_titles_narrow_the_sitemap_before_anything_is_fetched(monkeypatch):
    fetched = _patch_dot_jobs(monkeypatch)
    result = fetchers.fetch("Acme Labs", "https://acme.jobs/",
                            {"max_age_days": 30, "titles": ["forklift"]})
    assert [p.title for p in result.postings] == ["Warehouse Forklift Operator"]
    assert not [u for u in fetched if "/jobdetails/" in u]


def test_a_dot_jobs_site_with_no_sitemap_defers_to_the_agent(monkeypatch):
    monkeypatch.setattr(fetchers, "_get_html",
                        lambda url: "<html><body>nothing here</body></html>")
    result = fetchers.fetch("Acme Labs", "https://acme.jobs/", {})
    assert not result.ok, "an unreadable board must fall back, not report zero"
