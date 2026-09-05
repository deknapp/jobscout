"""Reading a job search out of a mailbox, on invented mail.

Every company, person and address below is made up, but the *shapes* are
copied from real applicant-tracking and recruiter-relay mail, because those
shapes are the whole problem: the employer's name is in the sender address for
one vendor and buried in boilerplate for the next, and the recruiter's name is
never in a header at all.
"""
import datetime as dt
import itertools

from jobscout import inbox
from jobscout.inbox import Message


_counter = itertools.count(1)


def msg(**kwargs):
    kwargs.setdefault("id", "m%d" % next(_counter))
    return Message(**kwargs)


# --- telling the four kinds of mail apart ----------------------------------

def test_an_ats_receipt_is_an_application():
    m = msg(sender="no-reply@ashbyhq.com", date="2026-08-12T19:33:19Z",
            subject="Thanks for applying to Kestrel Bio!",
            snippet="Hi Nathan, Thank you for applying for the Forward Deployed AI "
                    "Engineer role at Kestrel Bio! We appreciate your interest.")
    assert inbox.classify(m) == inbox.APPLICATION
    assert inbox.extract_company(m) == "Kestrel Bio"


def test_a_rejection_outranks_the_pleasantries_wrapped_around_it():
    m = msg(sender="no-reply@hire.lever.co", date="2026-01-22T15:22:43Z",
            subject="Thanks for your interest in Halcyon Labs",
            snippet="Thank you for your recent interest in Halcyon Labs. After "
                    "reviewing your work, we've made the decision to move forward "
                    "with other candidates.")
    assert inbox.classify(m) == inbox.REJECTION


def test_a_filled_role_is_a_rejection_too():
    m = msg(sender="hit-reply@linkedin.com", date="2026-06-23T15:13:15Z",
            subject="Message replied: Senior Cheminformatics Scientist",
            snippet="Hi Nathan, This position has been filled and is no longer open.")
    assert inbox.classify(m) == inbox.REJECTION


def test_job_alert_spam_is_never_mistaken_for_a_human():
    """This is the difference between a list worth reading and one that isn't."""
    m = msg(sender="jobs-noreply@linkedin.com", date="2026-09-04T19:22:47Z",
            subject="Kestrel Bio is hiring for a Platform role",
            snippet="Discover roles that match your interests. We're looking for "
                    "someone with your background — an exciting opportunity!")
    assert inbox.classify(m) == inbox.ALERT


def test_a_recruiter_pitch_through_the_relay_is_a_recruiter():
    m = msg(sender="inmail-hit-reply@linkedin.com", date="2026-08-20T18:44:43Z",
            subject="Exciting opportunity for a Platform role",
            body="Hi Nathan, My name is Yasmine Zahr and I am a professional "
                 "recruiter working with Kestrel Bio.\n\nRegards,\nYasmine")
    assert inbox.classify(m) == inbox.RECRUITER
    assert inbox.extract_person(m) == "Yasmine Zahr"


def test_your_own_replies_are_not_counted_as_someone_approaching_you():
    m = msg(sender="hit-reply@linkedin.com", from_me=True,
            date="2026-08-20T19:00:00Z", subject="Message replied: Platform role",
            body="Thanks for reaching out — I'd be interested in the opportunity.")
    assert inbox.classify(m) != inbox.RECRUITER


# --- recovering the employer's name ----------------------------------------

def test_a_workday_tenant_names_the_employer_when_the_text_will_not():
    """The cheapest signal in the mailbox, and the one most easily missed."""
    m = msg(sender="halcyon@myworkday.com", date="2023-10-17T07:33:19Z",
            subject="Thank you for your interest",
            snippet="Hello Nathan, Thank you for submitting an application for the "
                    "Data Scientist position.")
    assert inbox.extract_company(m) == "Halcyon"


def test_the_company_s_own_branding_beats_its_tenant_slug():
    """`bah@myworkday.com` is Booz Allen and `tbkbank@` is Triumph Financial.
    The slug is a fallback, not the answer, whenever the text says the name."""
    m = msg(sender="tbkbank@myworkday.com", date="2024-02-03T16:43:01Z",
            subject="Thank You for Applying!",
            snippet="Dear Nathan, Thank you for your interest in Triumph Financial! "
                    "We have received your application.")
    assert inbox.extract_company(m) == "Triumph Financial"


def test_a_job_title_is_never_returned_as_the_employer():
    """"your interest in the Software Engineer, Platform role at Benchling"
    reads exactly like a company name to a regex, and used to return one."""
    m = msg(sender="no-reply@ashbyhq.com", date="2026-07-17T01:20:13Z",
            subject="Your Benchling Application | Software Engineer, Platform "
                    "(Developer Experience)",
            snippet="Hi Nathaniel, Thank you for your interest in the Software "
                    "Engineer, Platform (Developer Experience) role at Benchling.")
    assert inbox.extract_company(m) == "Benchling"
    assert inbox.extract_role(m) == "Software Engineer, Platform (Developer Experience)"


def test_a_multi_tenant_vendor_falls_back_to_the_boilerplate():
    m = msg(sender="no-reply@ashbyhq.com", date="2025-12-28T06:31:12Z",
            subject="Thank You for Applying to Genesis Molecular AI",
            snippet="Hi Nathan, Thank you for your interest in Genesis Molecular AI! "
                    "We've received your application for the Product Manager role.")
    assert inbox.extract_company(m) == "Genesis Molecular AI"


def test_boilerplate_running_on_past_the_name_is_cut():
    m = msg(sender="no-reply@greenhouse.io", date="2026-02-02T00:00:00Z",
            subject="Application received",
            snippet="Thank you for applying to Kestrel Bio and we will review your "
                    "application shortly.")
    assert inbox.extract_company(m) == "Kestrel Bio"


def test_a_noreply_local_part_is_not_treated_as_a_company_name():
    m = msg(sender="no-reply@icims.com", date="2026-02-02T00:00:00Z",
            subject="Application received", snippet="Thank you for applying.")
    assert inbox.extract_company(m) == ""


# --- folding a thread into one application ---------------------------------

def test_repeated_receipts_for_one_job_collapse_to_a_single_application():
    """Workday sends the same acknowledgement twice, and employers send several
    over the life of one submission. Counting those as separate applications
    would inflate the history the rest of jobscout reasons from."""
    messages = [
        msg(id="1", thread_id="t1", sender="halcyon@myworkday.com",
            date="2023-10-26T19:58:34Z", subject="Thank you for applying",
            snippet="Thanks Nathan! We just received your application for the "
                    "Computational Modeling Specialist position."),
        msg(id="2", thread_id="t1", sender="halcyon@myworkday.com",
            date="2023-10-26T19:58:35Z", subject="Thank you for applying",
            snippet="Thanks Nathan! We just received your application for the "
                    "Computational Modeling Specialist position."),
        msg(id="3", thread_id="t1", sender="halcyon@myworkday.com",
            date="2023-11-10T12:31:21Z", subject="Application Status",
            snippet="We appreciate your interest. Unfortunately we have decided to "
                    "pursue other candidates."),
    ]
    found = inbox.applications(messages)
    assert len(found) == 1
    assert found[0].applied == "2023-10-26"
    assert found[0].last_heard == "2023-11-10"
    assert found[0].status == "rejected"


def test_an_interview_upgrades_the_status_but_a_rejection_still_wins():
    messages = [
        msg(id="1", thread_id="t1", sender="no-reply@ashbyhq.com",
            date="2026-07-01T00:00:00Z", subject="Thank you for applying to Kestrel Bio",
            snippet="Thank you for applying to Kestrel Bio!"),
        msg(id="2", thread_id="t1", sender="no-reply@ashbyhq.com",
            date="2026-07-20T00:00:00Z", subject="Your Upcoming Interview with Kestrel Bio",
            snippet="You have an upcoming interview with Kestrel Bio! Recruiter Screen."),
    ]
    assert inbox.applications(messages)[0].status == "interviewed"

    messages.append(msg(id="3", thread_id="t1", sender="no-reply@ashbyhq.com",
                        date="2026-08-01T00:00:00Z", subject="Kestrel Bio",
                        snippet="After reviewing, we will not be moving forward."))
    assert inbox.applications(messages)[0].status == "rejected"


# --- who approached you ----------------------------------------------------

RELAY = "inmail-hit-reply@linkedin.com"


def test_an_unanswered_approach_is_recorded_as_unanswered():
    thread = [msg(id="1", thread_id="t1", sender=RELAY, date="2026-07-09T02:44:39Z",
                  subject="High Profile Search",
                  body="Hi Nathan, I'm working with the founder of an early stage "
                       "company. My name is Dana Fell.")]
    entry = inbox.outreach(thread)[0]
    assert entry.replied is False
    assert entry.person == "Dana Fell"


def test_a_conversation_is_recorded_as_a_rapport():
    thread = [
        msg(id="1", thread_id="t2", sender=RELAY, date="2026-08-05T10:44:40Z",
            subject="Senior Backend Engineer",
            body="Hey Nathan, I'm working with a well-funded startup. My name is Dana Fell."),
        msg(id="2", thread_id="t2", sender="hit-reply@linkedin.com", from_me=True,
            date="2026-08-05T13:00:00Z", subject="Message replied: Senior Backend Engineer",
            body="Happy to chat."),
        msg(id="3", thread_id="t2", sender="hit-reply@linkedin.com",
            date="2026-08-06T13:43:14Z", subject="Message replied: Senior Backend Engineer",
            body="Great, look forward to our call."),
    ]
    entry = inbox.outreach(thread)[0]
    assert entry.replied is True
    assert entry.messages == 3


def test_a_dead_role_is_marked_closed_rather_than_dropped():
    """The role is gone; the recruiter who works your niche is not."""
    thread = [
        msg(id="1", thread_id="t3", sender=RELAY, date="2026-06-01T00:00:00Z",
            subject="Senior Cheminformatics Scientist",
            body="Hi Nathan, I'm a recruiter reaching out about a role. My name is Dana Fell."),
        msg(id="2", thread_id="t3", sender="hit-reply@linkedin.com",
            date="2026-06-23T15:13:15Z", subject="Message replied",
            body="Hi Nathan, This position has been filled and is no longer open."),
    ]
    assert inbox.outreach(thread)[0].outcome == "closed"


# --- ranking ---------------------------------------------------------------

TODAY = dt.date(2026, 9, 5)


def test_a_recruiter_for_a_target_employer_outranks_a_stranger():
    on_target = inbox.Outreach(person="A B", company="Kestrel Bio",
                               first_contact="2026-01-01", last_contact="2026-01-01")
    stranger = inbox.Outreach(person="C D", company="Unrelated Ltd",
                              first_contact="2026-01-01", last_contact="2026-01-01")
    ranked = inbox.follow_ups([stranger, on_target],
                              target_keys={"Kestrel Bio": "tracked"}, today=TODAY)
    assert ranked[0].outreach.company == "Kestrel Bio"


def test_a_conversation_from_last_week_is_pushed_down_not_up():
    """Chasing someone you spoke to on Tuesday is the fastest way to look
    desperate, so recency is a penalty here, not a bonus."""
    fresh = inbox.Outreach(person="A B", company="X", last_contact="2026-09-01")
    stale = inbox.Outreach(person="C D", company="Y", last_contact="2026-05-01")
    ranked = inbox.follow_ups([fresh, stale], today=TODAY)
    assert ranked[0].outreach.person == "C D"
    assert "too soon to chase" in " ".join(ranked[-1].reasons)


def test_an_entry_with_no_name_is_penalised_and_says_why():
    named = inbox.Outreach(person="A B", company="X", last_contact="2026-01-01")
    nameless = inbox.Outreach(person="", company="X", last_contact="2026-01-01")
    ranked = {f.outreach.person: f for f in inbox.follow_ups([named, nameless], today=TODAY)}
    assert ranked["A B"].score > ranked[""].score
    assert "no name recovered" in " ".join(ranked[""].reasons)


# --- storage ---------------------------------------------------------------

def test_messages_round_trip_and_deduplicate(tmp_path):
    first = [Message(id="a", date="2026-01-01T00:00:00Z", subject="one")]
    second = [Message(id="a", date="2026-01-01T00:00:00Z", subject="one"),
              Message(id="b", date="2026-02-01T00:00:00Z", subject="two")]
    inbox.save_messages(tmp_path / "one.json", first)
    inbox.save_messages(tmp_path / "two.json", second)
    loaded = inbox.load_messages(tmp_path)
    assert [m.id for m in loaded] == ["a", "b"]


# --- the relay's own layout ------------------------------------------------

RELAY_BODY = """Exciting opportunity for an IT Infrastructure role
Exciting opportunity for an IT Infrastructure role

      Dana Fell
        Reply
        https://www.linkedin.com/messaging/thread/2-abc==/

Hi Nathan, My name is Dana Fell and I am a professional recruiter with Kestrel
Staffing. I'm reaching out on behalf of our client who is currently hiring.

Best regards,
Dana
"""


def test_the_relay_layout_names_the_sender_even_without_an_introduction():
    """LinkedIn writes the sender's name alone on the line above "Reply". That
    holds even when the recruiter never says who they are — and most don't."""
    body = RELAY_BODY.replace("Hi Nathan, My name is Dana Fell and I am a "
                              "professional recruiter with Kestrel\nStaffing. ", "")
    m = msg(sender=RELAY, date="2026-08-20T00:00:00Z",
            subject="Exciting opportunity for an IT Infrastructure role", body=body)
    assert inbox.extract_person(m) == "Dana Fell"


def test_the_staffing_firm_is_recovered_separately_from_the_hiring_company():
    m = msg(sender=RELAY, date="2026-08-20T00:00:00Z",
            subject="Exciting opportunity for an IT Infrastructure role",
            body=RELAY_BODY)
    assert inbox.extract_agency(m) == "Kestrel Staffing"


def test_the_job_is_taken_from_the_subject_not_the_adjective_in_front_of_it():
    m = msg(sender=RELAY, date="2026-08-20T00:00:00Z",
            subject="Exciting opportunity for an IT Infrastructure role",
            body=RELAY_BODY)
    assert inbox.extract_role(m) == "IT Infrastructure"


def test_inmail_is_a_recruiter_approach_by_definition():
    """InMail is LinkedIn's paid recruiting product — nobody sends one to say
    hello. Requiring a recognisable pitch on top of that dropped real
    approaches whose authors opened with flattery instead of a job."""
    m = msg(sender="inmail-hit-reply@linkedin.com", date="2026-07-21T20:44:40Z",
            subject="Kestrel Bio / Scientific Software Engineer / Remote friendly",
            body="Hi Nathan- Interested in your Engineering and bio experience"
                 "--looks to match up nicely with a key need we have here at "
                 "Kestrel Bio within our Simulation vertical.")
    assert inbox.classify(m) == inbox.RECRUITER
    assert inbox.extract_company(m) == "Kestrel Bio"


def test_a_credential_after_a_name_is_not_part_of_the_name():
    body = ("Subject line\nSubject line\n\n      Greg Taylor, PHR\n        Reply\n"
            "        https://example.com/\n\nNathan,\n\nI'm recruiting a Software "
            "Engineer for Kestrel Bio, a frontier AI company.\n")
    m = msg(sender="inmail-hit-reply@linkedin.com", date="2026-07-27T00:00:00Z",
            subject="Subject line", body=body)
    assert inbox.extract_person(m) == "Greg Taylor"
    assert inbox.extract_company(m) == "Kestrel Bio"
