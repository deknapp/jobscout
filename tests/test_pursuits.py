"""Reading a live search out of ordinary mail.

The model is mocked throughout. What is under test is the split the module is
built on: Python decides what may be read and what to do about it, the model
only reports what the correspondence says. The advice rules are the part a
person will want to argue with, so they are the part pinned down here.
"""
import datetime as dt
import json

from jobscout import pursuits
from jobscout.inbox import Message
from jobscout.llm import LLM, MockBackend
from jobscout.pursuits import Advice, Evidence, Pursuit

TODAY = dt.date(2026, 9, 5)


def llm_returning(payload):
    backend = MockBackend(default=json.dumps(payload))
    return LLM(backend, model_cheap="m", model_strong="M"), backend


# --- what the model is allowed to see --------------------------------------

def test_job_alert_spam_never_reaches_a_prompt():
    """A cost control and a privacy boundary, not only a quality filter."""
    spam = Message(id="1", sender="jobs-noreply@linkedin.com",
                   date="2026-09-04T00:00:00Z", subject="Kestrel is hiring",
                   snippet="Discover roles that match your interests")
    assert not pursuits.is_human_mail(spam)
    assert pursuits.group([spam]) == {}


def test_a_person_at_an_employer_domain_is_read():
    real = Message(id="1", sender="lily@kestrel.com", to="me@gmail.com",
                   date="2026-08-19T00:00:00Z", subject="Re: the role",
                   body="Thanks Nathan - there's still no req posted.")
    assert pursuits.is_human_mail(real)
    assert pursuits.company_of(real) == "kestrel"


def test_the_domain_beats_the_prose_when_naming_the_employer():
    """"our organization" is what a real lab wrote, and a regex believed it."""
    vague = Message(id="1", sender="recruiter@somelab.gov", to="me@gmail.com",
                    date="2026-08-27T00:00:00Z", subject="Request for Coding Sample",
                    body="Thank you for your interest in the position with our "
                         "organization. As the next step we would like a code sample.")
    assert pursuits.company_of(vague) == "somelab"


def test_your_own_outbound_mail_is_part_of_the_thread():
    mine = Message(id="1", sender="me@gmail.com", to="lily@kestrel.com",
                   from_me=True, date="2026-08-31T00:00:00Z",
                   subject="Checking in", body="Any information on timing?")
    assert pursuits.is_human_mail(mine)
    assert pursuits.company_of(mine) == "kestrel"


# --- dates never come from the model ---------------------------------------

def test_dates_and_direction_are_taken_from_the_headers():
    """Asked for a date, a model produces a plausible one. The mailbox knows."""
    thread = [
        Message(id="1", sender="lily@kestrel.com", to="me@gmail.com",
                date="2026-08-19T00:00:00Z", subject="Re: role", body="No req yet."),
        Message(id="2", sender="me@gmail.com", to="lily@kestrel.com", from_me=True,
                date="2026-08-31T00:00:00Z", subject="Checking in", body="Any news?"),
    ]
    llm, _ = llm_returning([{"role": "PM Lead", "stage": "interviewing",
                             "ball_with": "them", "blocker": "no requisition posted",
                             "people": ["Lily Kim"],
                             "evidence": [{"date": "2026-08-19", "who": "Lily Kim",
                                           "quote": "there's still no req posted"}]}])
    found = pursuits.read(thread, "Kestrel", llm, today=TODAY)
    assert len(found) == 1
    assert found[0].first_contact == "2026-08-19"
    assert found[0].last_activity == "2026-08-31"
    assert found[0].last_direction == pursuits.YOU
    assert found[0].chased == 1
    assert found[0].evidence[0].quote == "there's still no req posted"


def test_one_employer_can_hold_several_separate_processes():
    """A lab running three searches is three pursuits. Merging them gives advice
    that is wrong in both directions — a rejection on one says nothing about
    the others."""
    thread = [Message(id="1", sender="r@somelab.gov", to="me@gmail.com",
                      date="2026-08-01T00:00:00Z", subject="IRC1 and IRC2",
                      body="Two openings.")]
    llm, _ = llm_returning([
        {"role": "Computer Scientist 2/3", "requisition": "IRC145054",
         "stage": "assignment", "ball_with": "you", "people": ["Kim"]},
        {"role": "Computer Scientist 3", "requisition": "IRC144588",
         "stage": "closed", "ball_with": "nobody", "people": ["Bee"]},
    ])
    found = pursuits.read(thread, "Somelab", llm, today=TODAY)
    assert len(found) == 2
    assert {p.requisition for p in found} == {"IRC145054", "IRC144588"}
    assert found[0].key != found[1].key


def test_a_stage_the_model_invents_is_not_accepted():
    llm, _ = llm_returning([{"role": "X", "stage": "extremely promising",
                             "ball_with": "maybe"}])
    found = pursuits.read([Message(id="1", sender="a@b.com", date="2026-01-01T00:00:00Z")],
                          "B", llm, today=TODAY)
    assert found[0].stage == "enquiry"
    assert found[0].ball_with == pursuits.THEM


# --- the advice rules ------------------------------------------------------

def advice_for(**kwargs):
    kwargs.setdefault("company", "Kestrel")
    kwargs.setdefault("people", ["Lily"])
    return pursuits.recommend(Pursuit(**kwargs), today=TODAY)


def test_an_outstanding_deliverable_outranks_everything():
    a = advice_for(stage="assignment", ball_with="you", last_activity="2026-09-04")
    assert a.urgency == "now"
    assert "Send what Lily asked for" in a.action


def test_a_stated_structural_blocker_is_not_a_silence_problem():
    """The employer has told you why it is stalled. Chasing the person who told
    you does not change it — the useful move is sideways, to someone who knows
    whether the blocker is real."""
    a = advice_for(stage="interviewing", ball_with="them", last_activity="2026-08-31",
                   people=["Lily", "Ash", "Josh"], blocker="no requisition posted yet")
    assert "ask someone else inside Kestrel" in a.action
    assert "no requisition posted yet" in a.why
    assert a.urgency != "now"


def test_five_days_of_silence_is_not_yet_a_problem():
    a = advice_for(stage="interviewing", ball_with="them", last_activity="2026-09-01")
    assert a.urgency == "none"
    assert "Wait" in a.action


def test_two_unanswered_follow_ups_is_an_answer():
    a = advice_for(stage="screening", ball_with="them",
                   last_activity="2026-06-01", chased=2)
    assert a.urgency == "none"
    assert "dead" in a.action.lower()


def test_a_rejection_keeps_the_person_even_though_the_role_is_gone():
    a = advice_for(stage="closed", ball_with="nobody", last_activity="2026-09-01",
                   blocker="They went with another candidate.")
    assert a.urgency == "none"
    assert "Keep Lily as a contact" in a.action


def test_what_needs_doing_now_sorts_above_what_can_wait():
    now = Pursuit(company="A", people=["X"], stage="assignment", ball_with="you",
                  last_activity="2026-09-04")
    later = Pursuit(company="B", people=["Y"], stage="interviewing", ball_with="them",
                    last_activity="2026-09-01")
    ranked = sorted([pursuits.recommend(later, TODAY), pursuits.recommend(now, TODAY)],
                    key=lambda a: a.sort_key)
    assert ranked[0].pursuit.company == "A"


def test_review_reads_each_employer_once():
    thread = [Message(id="1", sender="lily@kestrel.com", to="me@gmail.com",
                      date="2026-08-19T00:00:00Z", subject="Re: role", body="No req."),
              Message(id="2", sender="jobs-noreply@linkedin.com",
                      date="2026-09-01T00:00:00Z", subject="spam",
                      snippet="Discover roles that match your interests")]
    llm, backend = llm_returning([{"role": "PM Lead", "stage": "interviewing",
                                   "ball_with": "them", "people": ["Lily Kim"]}])
    found = pursuits.review(thread, llm, today=TODAY)
    assert len(found) == 1
    assert len(backend.prompts) == 1          # the spam never reached a prompt
    assert "jobs-noreply" not in backend.prompts[0]


# --- the relay is not an employer ------------------------------------------

RELAY = "inmail-hit-reply@linkedin.com"


def test_two_recruiters_on_one_platform_are_two_pursuits():
    """Every recruiter on LinkedIn writes from the same address. Filing them by
    that domain made nine unrelated approaches into one company called
    "linkedin" — and each then inherited the newest date in the pile, so a June
    approach was reported as "quiet 0 days" and marked urgent."""
    first = Message(id="1", thread_id="t1", sender=RELAY, to="me@gmail.com",
                    date="2026-06-24T00:00:00Z", subject="Opportunity at Kestrel",
                    body="We are growing our team here at Kestrel Bio.")
    second = Message(id="2", thread_id="t2", sender=RELAY, to="me@gmail.com",
                     date="2026-08-20T00:00:00Z", subject="A different role",
                     body="Hi Nathan, I hope all is well! I just wanted to reach out.")
    buckets = pursuits.group([first, second])
    assert len(buckets) == 2
    assert not any(key == "linkedin" for key in buckets)


def test_a_dated_thread_keeps_its_own_dates():
    old_approach = Message(id="1", thread_id="t1", sender=RELAY, to="me@gmail.com",
                           date="2026-06-24T00:00:00Z", subject="Old approach",
                           body="Reaching out about a role.")
    llm, _ = llm_returning([{"role": "X", "stage": "enquiry", "ball_with": "you"}])
    found = pursuits.read([old_approach], "Kestrel", llm, today=TODAY)
    assert found[0].last_activity == "2026-06-24"
    assert found[0].days_quiet(TODAY) == 73


def test_going_sideways_needs_somebody_to_go_sideways_to():
    """"Ask someone else inside" is not advice you can act on when the only
    person you know there is the one who told you."""
    alone = advice_for(stage="interviewing", ball_with="them", people=["Harry"],
                       last_activity="2026-08-25", blocker="waiting on the client")
    assert "ask someone else" not in alone.action.lower()

    crowd = advice_for(stage="interviewing", ball_with="them",
                       people=["Lily", "Ash", "Josh"],
                       last_activity="2026-08-31", blocker="no requisition posted yet")
    assert "ask someone else" in crowd.action.lower()


def test_each_process_is_dated_by_its_own_messages():
    """A lab running two searches had both dated by whichever mail arrived last
    at the lab — so a rejection on one requisition reset the clock on the other,
    and a code sample sent two weeks ago looked four days old."""
    thread = [
        Message(id="1", sender="kim@somelab.gov", to="me@gmail.com",
                date="2026-08-27T00:00:00Z", subject="Code sample",
                body="We would like to request a code sample."),
        Message(id="2", sender="me@gmail.com", to="kim@somelab.gov", from_me=True,
                date="2026-08-27T01:00:00Z", subject="Re: Code sample",
                body="Here is a code sample."),
        Message(id="3", sender="bee@somelab.gov", to="me@gmail.com",
                date="2026-09-01T00:00:00Z", subject="Interview follow-up",
                body="We're not going to move forward with an onsite interview."),
    ]
    llm, _ = llm_returning([
        {"role": "CS 3", "requisition": "IRC1", "stage": "assignment",
         "ball_with": "them", "people": ["Kim"], "messages": [0, 1]},
        {"role": "CS 2", "requisition": "IRC2", "stage": "closed",
         "ball_with": "nobody", "people": ["Bee"], "messages": [2]},
    ])
    found = {p.requisition: p for p in pursuits.read(thread, "Somelab", llm, today=TODAY)}
    assert found["IRC1"].last_activity == "2026-08-27"
    assert found["IRC2"].last_activity == "2026-09-01"
    assert found["IRC1"].days_quiet(TODAY) == 9


def test_an_unusable_message_map_falls_back_to_the_whole_thread():
    thread = [Message(id="1", sender="a@b.com", to="me@gmail.com",
                      date="2026-08-01T00:00:00Z", subject="x", body="y")]
    llm, _ = llm_returning([{"role": "X", "stage": "enquiry", "ball_with": "them",
                             "messages": ["nonsense", 99]}])
    assert pursuits.read(thread, "B", llm, today=TODAY)[0].last_activity == "2026-08-01"
