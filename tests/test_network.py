"""The network view, on invented people.

Every name, employer and date in this file is made up. The point of the module
is to read one particular real file, so the tests pin the shape of that file —
including the preamble LinkedIn puts above the header, which is the one thing a
naive reader gets wrong.
"""
import datetime as dt

import pytest

from jobscout import network as net
from jobscout.network import Affiliation, Connection, NetworkError

# LinkedIn writes three lines of apology, then a blank line, then the header.
REAL_SHAPE = '''"Notes:"
"When exporting your connection data, you may notice that some of the ..."
"the date of your export."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ada,Byron,https://www.linkedin.com/in/ada,ada@example.com,Analytical Engines,Principal Engineer,14 Mar 2021
Grace,Hopper,https://www.linkedin.com/in/grace,,Compiler Works,VP Engineering,02 Feb 2018
Alan,Turing,https://www.linkedin.com/in/alan,,Analytical Engines,Software Engineer,09 Sep 2021
Karen,Sparck,https://www.linkedin.com/in/karen,,Retrieval Corp,Technical Recruiter,11 Nov 2022
'''


def test_the_preamble_is_not_mistaken_for_a_header():
    people = net.parse_connections_csv(REAL_SHAPE)
    assert len(people) == 4
    assert people[0].name == "Ada Byron"
    assert people[0].company == "Analytical Engines"
    assert people[0].connected_date == dt.date(2021, 3, 14)


def test_a_file_with_no_header_is_an_error_not_an_empty_list():
    with pytest.raises(NetworkError):
        net.parse_connections_csv("just,some,other,csv\n1,2,3,4\n")


def test_columns_are_found_by_name_not_by_position():
    """LinkedIn has reordered and renamed these columns before now."""
    reordered = ("Position,Company,First Name,Last Name,Profile URL,Connected\n"
                 "Chef,Kitchen Co,Rene,Redzepi,https://x/rene,01 Jan 2020\n")
    people = net.parse_connections_csv(reordered)
    assert people[0].name == "Rene Redzepi"
    assert people[0].company == "Kitchen Co"
    assert people[0].position == "Chef"


def test_a_connection_with_no_date_does_not_crash_the_anchor_lookup():
    people = net.parse_connections_csv(
        "First Name,Last Name,Company,Position,Connected On\nNo,Date,Somewhere,Dev,\n")
    leads = net.rank(people, [Affiliation("Somewhere", "2020-01", "2022-01")])
    assert leads and leads[0].anchor == ""


# --- how you met -----------------------------------------------------------

HISTORY = [
    Affiliation("Analytical Engines", "2020-08", "2024-05"),
    Affiliation("Compiler Works", "2017-01", "2018-06"),
    Affiliation("Some University", "2014-09", "2017-06", kind="school"),
]


def test_someone_who_left_the_employer_you_shared_is_flagged_as_moved_on():
    """Grace was connected in 2018 while you were at Compiler Works, and is
    listed there still — no move. Ada connected in 2021 during Analytical
    Engines and is still there — no move either. The interesting one is a
    person met at an old employer who now appears somewhere else."""
    people = net.parse_connections_csv(REAL_SHAPE)
    moved = Connection(first_name="Ken", last_name="Iverson",
                       company="Array Systems", position="Staff Engineer",
                       connected_on="03 Mar 2021", url="https://x/ken")
    leads = net.rank(people + [moved], HISTORY, today=dt.date(2026, 9, 5))
    ken = next(l for l in leads if l.name == "Ken Iverson")
    assert ken.anchor == "Analytical Engines"
    assert ken.bucket == net.MOVED_ON
    assert "now at Array Systems" in " ".join(ken.reasons)

    ada = next(l for l in leads if l.name == "Ada Byron")
    assert ada.bucket != net.MOVED_ON


def test_a_job_outranks_a_degree_when_both_windows_cover_the_date():
    overlapping = [Affiliation("Some University", "2014-09", "2017-06", kind="school"),
                   Affiliation("First Job", "2017-01", "2017-12")]
    met = Connection(first_name="A", last_name="B", company="Elsewhere",
                     position="Engineer", connected_on="2017-03-01")
    lead = net.rank([met], overlapping)[0]
    assert lead.anchor == "First Job"


def test_connections_just_outside_a_window_still_anchor_to_it():
    """People accept requests late, and recruiters connect before you start."""
    late = Connection(first_name="L", last_name="M", company="Elsewhere",
                      position="Engineer", connected_on="2024-06-20")
    lead = net.rank([late], HISTORY)[0]
    assert lead.anchor == "Analytical Engines"


# --- what makes someone worth a message ------------------------------------

def test_an_employer_you_applied_to_beats_one_merely_on_the_list():
    person = Connection(first_name="I", last_name="N", company="Target Corp",
                        position="Engineer", connected_on="2021-01-01")
    # Passed as written, not pre-normalised — that is what a caller will do.
    applied = net.rank([person], [], {"Target Corp": "applied"})[0]
    tracked = net.rank([person], [], {"Target, Inc.": "tracked"})[0]
    assert applied.score > tracked.score
    assert applied.bucket == net.INSIDE_TARGET


def test_recruiters_are_called_out_separately_from_other_senior_people():
    recruiter = Connection(first_name="R", last_name="R", company="Retrieval Corp",
                           position="Technical Recruiter", connected_on="2022-11-11")
    director = Connection(first_name="D", last_name="D", company="Retrieval Corp",
                          position="Director of Engineering", connected_on="2022-11-11")
    leads = {l.name: l for l in net.rank([recruiter, director], [])}
    assert leads["R R"].bucket == net.LEVERAGE_BUCKET
    assert "recruits for a living" in " ".join(leads["R R"].reasons)
    assert "refer or to hire" in " ".join(leads["D D"].reasons)


def test_domain_terms_come_from_the_profile_and_not_from_this_source_file():
    profile = {"target_titles": ["Cheminformatics Engineer"],
               "domains": ["Molecular design"]}
    chemist = Connection(first_name="C", last_name="H", company="Pharma Ltd",
                         position="Cheminformatics Scientist", connected_on="2021-01-01")
    baker = Connection(first_name="B", last_name="K", company="Bread Ltd",
                       position="Baker", connected_on="2021-01-01")
    leads = {l.name: l for l in net.rank([chemist, baker], [], profile=profile)}
    assert leads["C H"].score > leads["B K"].score
    assert leads["C H"].bucket == net.DOMAIN


def test_a_blank_employer_is_penalised_rather_than_silently_ranked():
    blank = Connection(first_name="U", last_name="K", company="", position="",
                       connected_on="2021-01-01")
    lead = net.rank([blank], HISTORY)[0]
    assert lead.score < 0
    assert lead.bucket == net.REST


# --- the diff, which is the whole point of taking a baseline ---------------

def test_the_diff_names_movers_promotions_and_new_faces():
    before = [Connection(url="https://x/a", first_name="A", last_name="A",
                         company="Old Co", position="Engineer"),
              Connection(url="https://x/b", first_name="B", last_name="B",
                         company="Same Co", position="Engineer")]
    after = [Connection(url="https://x/a", first_name="A", last_name="A",
                        company="New Co", position="Engineer"),
             Connection(url="https://x/b", first_name="B", last_name="B",
                        company="Same Co", position="Senior Engineer"),
             Connection(url="https://x/c", first_name="C", last_name="C",
                        company="Third Co", position="Engineer")]
    kinds = {c.connection.name: c.kind for c in net.diff_snapshots(before, after)}
    assert kinds == {"A A": "moved", "B B": "promoted", "C C": "new"}


def test_a_rewritten_company_name_is_not_reported_as_a_move():
    """"Acme, Inc." and "Acme" are the same employer, and a diff that says
    otherwise makes the whole feature untrustworthy the first time it runs."""
    before = [Connection(url="https://x/a", first_name="A", last_name="A",
                         company="Acme, Inc.", position="Engineer")]
    after = [Connection(url="https://x/a", first_name="A", last_name="A",
                        company="Acme", position="Engineer")]
    assert net.diff_snapshots(before, after) == []


def test_snapshots_round_trip(tmp_path):
    people = net.parse_connections_csv(REAL_SHAPE)
    net.save_snapshot(tmp_path, people, dt.date(2026, 9, 5))
    saved = net.list_snapshots(tmp_path)
    assert len(saved) == 1
    assert [c.name for c in net.load_snapshot(saved[0])] == [c.name for c in people]


# --- coverage --------------------------------------------------------------

def test_coverage_separates_employers_you_can_reach_from_cold_ones():
    people = net.parse_connections_csv(REAL_SHAPE)
    from jobscout.corpus import normalize_company
    targets = {normalize_company("Analytical Engines"): "applied",
               normalize_company("Unreachable Corp"): "tracked"}
    names = {normalize_company("Analytical Engines"): "Analytical Engines",
             normalize_company("Unreachable Corp"): "Unreachable Corp"}
    rows = dict((name, people) for name, people in
                net.company_coverage(people, targets, names))
    assert len(rows["Analytical Engines"]) == 2
    assert rows["Unreachable Corp"] == []


# --- the export can arrive in three shapes ---------------------------------

def test_a_zip_a_folder_and_a_bare_csv_all_read(tmp_path):
    import zipfile

    csv_path = tmp_path / "Connections.csv"
    csv_path.write_text(REAL_SHAPE, encoding="utf-8")
    assert len(net.read_export(csv_path)) == 4
    assert len(net.read_export(tmp_path)) == 4

    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Connections.csv", REAL_SHAPE)
    assert len(net.read_export(archive)) == 4


def test_a_full_archive_without_connections_says_so(tmp_path):
    import zipfile

    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Profile.csv", "First Name\nX\n")
    with pytest.raises(NetworkError) as excinfo:
        net.read_export(archive)
    assert "Connections.csv" in str(excinfo.value)


# --- one employer, however they spell it -----------------------------------

def test_a_rename_is_not_two_employers():
    """OpenEye became "OpenEye, Cadence Molecular Sciences" mid-career. Reading
    that as two jobs makes everyone met either side of it look like a mover."""
    assert net.same_employer("OpenEye Scientific", "OpenEye, Cadence Molecular Sciences")
    assert net.same_employer("Gate Bioscience", "Gate Bioscience, Inc.")
    assert net.same_employer("Genesis Molecular AI", "Genesis")


def test_a_shared_generic_word_is_not_a_shared_employer():
    """This is the guard that keeps the rule from collapsing every lab and
    every university into one another."""
    assert not net.same_employer("Sandia National Laboratories",
                                 "National Renewable Energy Laboratory")
    assert not net.same_employer("Broad Institute", "Allen Institute")
    assert not net.same_employer("Recursion Pharmaceuticals", "Novartis Pharmaceuticals")


def test_consecutive_spells_at_one_employer_become_one_window():
    spells = [Affiliation("OpenEye Scientific", "2020-08-01", "2022-08-01"),
              Affiliation("OpenEye, Cadence Molecular Sciences", "2022-09-01", "2024-05-01")]
    merged = net._merge_spells(spells)
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == ("2020-08-01", "2024-05-01")
    assert merged[0].name == "OpenEye, Cadence Molecular Sciences"


def test_a_job_you_still_hold_has_no_end_date():
    merged = net._merge_spells([Affiliation("Kestrel Bio", "2020-01-01", "2022-01-01"),
                                Affiliation("Kestrel Bio", "2022-01-01", "")])
    assert merged[0].end == ""


def test_a_renamed_employer_is_not_reported_as_a_move():
    before = [Connection(url="https://x/a", first_name="A", last_name="A",
                         company="OpenEye Scientific", position="Engineer")]
    after = [Connection(url="https://x/a", first_name="A", last_name="A",
                        company="OpenEye, Cadence Molecular Sciences", position="Engineer")]
    assert net.diff_snapshots(before, after) == []


def test_someone_you_emailed_last_week_is_not_offered_as_a_fresh_lead():
    """The connections file and the mailbox describe the same relationships and
    share no identifier. Without a name fallback, the tool cheerfully
    recommends contacting a person you wrote to on Monday."""
    from jobscout.inbox import Correspondent

    talked = net.conversations_from_mail([
        Correspondent(email="mary@example.com", name="Mary Pitman",
                      sent=1, last_sent="2026-08-31")])
    person = Connection(first_name="Mary", last_name="Pitman", url="https://x/mary",
                        company="Kestrel Bio", position="Head of Simulation",
                        connected_on="2021-01-01")
    lead = net.rank([person], [], {"Kestrel Bio": "applied"},
                    conversations=talked, today=dt.date(2026, 9, 5))[0]
    assert "5 days ago" in " ".join(lead.reasons)


def test_someone_contacted_recently_is_marked_so_the_view_can_hide_them():
    """A 30-point penalty was not enough: a contact at an employer the user had
    applied to still topped the list five days after he emailed her, because
    the target-employer bonus is larger. Whether to show her is the view's
    decision, so the fact is recorded rather than fudged into the score."""
    from jobscout.inbox import Correspondent

    talked = net.conversations_from_mail([
        Correspondent(email="mary.pitman@example.com", sent=1, last_sent="2026-08-31")])
    person = Connection(first_name="Mary", last_name="Pitman", url="https://x/m",
                        company="Kestrel Bio", position="Head of Simulation",
                        connected_on="2021-01-01")
    lead = net.rank([person], [], {"Kestrel Bio": "applied"},
                    conversations=talked, today=dt.date(2026, 9, 5))[0]
    assert lead.spoke_recently == 5


def test_linkedin_and_mail_are_combined_rather_than_one_winning():
    """Each system holds half the history. Looking up the profile URL *instead
    of* the address reported a 2024 conversation and hid an email sent five
    days ago, so the contact came back marked "warm, and long overdue"."""
    from jobscout.inbox import Correspondent

    talked = net.conversations_from_mail([
        Correspondent(email="mary.pitman@example.com", sent=1, last_sent="2026-08-31")])
    talked["mary"] = net.Conversation(name="Mary Pitman", url="https://x/in/mary",
                                      sent=3, received=3, last_sent="2024-04-15",
                                      last_received="2024-04-15")
    person = Connection(first_name="Mary", last_name="Pitman",
                        url="https://www.linkedin.com/in/mary",
                        company="Kestrel Bio", position="Head of Simulation",
                        connected_on="2021-01-01")
    lead = net.rank([person], [], {"Kestrel Bio": "applied"},
                    conversations=talked, today=dt.date(2026, 9, 5))[0]
    assert lead.spoke_recently == 5
    assert "long enough ago to pick up" not in " ".join(lead.reasons)
