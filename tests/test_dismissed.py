"""Ruling a lead out, and having it stay out.

A recommender that cannot be told "no" is only useful once. These tests pin the
two properties that make the kill-list worth having: it matches a person under
whichever identity a given file happens to carry, and it never loses the reason.
"""
import datetime as dt

import pytest

from jobscout import inbox, network
from jobscout.config import LocationPolicy
from jobscout.dismissed import Dismissals, EMPLOYER, PERSON
from jobscout.inbox import Outreach
from jobscout.network import Affiliation, Connection


def store(tmp_path):
    return Dismissals(tmp_path / "dismissed.json")


def test_a_person_is_matched_on_whichever_identity_you_have(tmp_path):
    """The connections file has a profile URL, the mailbox has an address, and
    a recruiter relay has only a name. One dismissal has to cover all three."""
    killed = store(tmp_path)
    killed.add(PERSON, "https://www.linkedin.com/in/someone/", "wasted my time")
    assert killed.person("https://linkedin.com/in/someone") is not None
    assert killed.person("someone@elsewhere.com") is None      # a different key

    killed.add(PERSON, "recruiter@agency.com", "contract roles only")
    assert killed.person("Recruiter@Agency.com") is not None


def test_an_employer_is_matched_however_it_is_spelled(tmp_path):
    killed = store(tmp_path)
    killed.add(EMPLOYER, "Kestrel Bio, Inc.", "on-site only")
    assert killed.employer("Kestrel Bio") is not None
    assert killed.employer("Unrelated Ltd") is None


def test_a_dismissal_without_a_reason_is_still_recorded_but_says_so(tmp_path):
    killed = store(tmp_path)
    entry = killed.add(PERSON, "A Name", "")
    assert "no reason given" in entry.summary


def test_the_reason_survives_a_round_trip(tmp_path):
    killed = store(tmp_path)
    killed.add(EMPLOYER, "Kestrel Bio", "the role is in person")
    killed.save()
    assert store(tmp_path).employer("Kestrel Bio").reason == "the role is in person"


def test_undoing_puts_someone_back(tmp_path):
    killed = store(tmp_path)
    killed.add(PERSON, "someone@example.com", "dead end")
    assert killed.remove("someone@example.com") is not None
    assert killed.person("someone@example.com") is None


def test_nothing_identifiable_is_refused(tmp_path):
    with pytest.raises(ValueError):
        store(tmp_path).add(PERSON, "   ", "why")


# --- the rankers honour it -------------------------------------------------

def test_a_killed_contact_sinks_but_keeps_its_reason(tmp_path):
    """Deleting would lose the reason, and hiding silently is the thing this
    tool must never do to someone job hunting."""
    killed = store(tmp_path)
    killed.add(PERSON, "https://x/dead", "promised a role then vanished")
    live = Connection(first_name="A", last_name="Live", url="https://x/live",
                      company="Kestrel Bio", position="Director",
                      connected_on="2021-01-01")
    dead = Connection(first_name="B", last_name="Dead", url="https://x/dead",
                      company="Kestrel Bio", position="Director",
                      connected_on="2021-01-01")
    ranked = network.rank([dead, live], [], killed=killed)
    assert ranked[0].name == "A Live"
    assert ranked[-1].killed == "promised a role then vanished"
    assert "you ruled this out" in " ".join(ranked[-1].reasons)


def test_a_killed_employer_covers_everyone_inside_it(tmp_path):
    killed = store(tmp_path)
    killed.add(EMPLOYER, "Kestrel Bio", "in-person only")
    inside = Connection(first_name="A", last_name="B", url="https://x/a",
                        company="Kestrel Bio, Inc.", position="Director",
                        connected_on="2021-01-01")
    assert network.rank([inside], [], killed=killed)[0].killed == "in-person only"


def test_an_inbound_recruiter_can_be_killed_too(tmp_path):
    killed = store(tmp_path)
    killed.add(PERSON, "Dana Fell", "only ever pitches contract work")
    entry = Outreach(person="Dana Fell", company="Kestrel Bio",
                     last_contact="2026-01-01")
    ranked = inbox.follow_ups([entry], killed=killed, today=dt.date(2026, 9, 5))
    assert ranked[0].killed == "only ever pitches contract work"


# --- location ---------------------------------------------------------------

POLICY = LocationPolicy(allowed_states=["NM"], allowed_cities=["albuquerque"],
                        allow_remote=True).normalized()


def test_an_explicitly_on_site_role_somewhere_you_will_not_go_is_flagged():
    text = ("I'm recruiting for a frontier AI company. We're in San Francisco "
            "(waterfront office) and this is an in-person role.")
    assert "outside where you will work" in inbox.location_warning(text, POLICY)


def test_on_site_somewhere_you_will_go_is_not_flagged():
    text = "This full-time, on-site position in Albuquerque could be a great fit."
    assert inbox.location_warning(text, POLICY) == ""


def test_a_remote_role_that_names_a_head_office_is_not_flagged():
    """Naming a city is not the same as requiring you to be in it."""
    text = "We're based in San Francisco but this role is fully remote."
    assert inbox.location_warning(text, POLICY) == ""


def test_silence_about_location_is_not_treated_as_evidence():
    """Most approaches never say. Guessing would bury good roles or wave
    through bad ones, so the honest answer is to say nothing."""
    assert inbox.location_warning("Are you on the job market by any chance?", POLICY) == ""


def test_a_flagged_role_is_pushed_down_the_follow_up_list():
    far = Outreach(person="A", company="X", last_contact="2026-01-01",
                   said_about_place="This is an in-person role. We're in Boston.")
    near = Outreach(person="B", company="Y", last_contact="2026-01-01")
    ranked = inbox.follow_ups([far, near], policy=POLICY, today=dt.date(2026, 9, 5))
    assert ranked[0].outreach.person == "B"
