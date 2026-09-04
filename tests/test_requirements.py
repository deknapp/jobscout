"""A filter must know the difference between "no" and "didn't say".

The failure this module exists to prevent: you ask for a $150k floor, and every
posting that simply doesn't print a salary — which is most of them — silently
disappears. You are never told, and you conclude the market is empty.
"""
import pytest

from jobscout.models import Posting
from jobscout.requirements import (EXCLUDE, FAIL, INCLUDE, PASS, UNKNOWN,
                                   Requirements, detect_employment_type,
                                   parse_salary, requires_clearance)


# --- the headline behaviour ------------------------------------------------

def test_a_posting_with_no_salary_is_kept_or_dropped_by_YOUR_choice():
    quiet = Posting(title="Senior Data Engineer", summary="Great team.")

    lenient = Requirements(salary_min=150_000, unknown_salary=INCLUDE).normalized()
    accepted, reason = lenient.check(quiet)
    assert accepted and "kept by your setting" in reason

    strict = Requirements(salary_min=150_000, unknown_salary=EXCLUDE).normalized()
    accepted, reason = strict.check(quiet)
    assert not accepted and "asked to drop unknowns" in reason


def test_each_filter_decides_unknowns_independently():
    """Strict about salary need not mean strict about everything."""
    posting = Posting(title="Senior Engineer", summary="No pay or type stated.")
    mixed = Requirements(salary_min=150_000, unknown_salary=INCLUDE,
                         employment_types=["full-time"],
                         unknown_employment=EXCLUDE).normalized()
    accepted, reason = mixed.check(posting)
    assert not accepted
    assert "employment type" in reason      # dropped on type, not on salary


def test_a_stated_salary_still_gets_judged_on_its_merits():
    low = Posting(title="Engineer", salary="$90,000 - $110,000 per year")
    high = Posting(title="Engineer", salary="$160,000 - $200,000 per year")
    rules = Requirements(salary_min=150_000, unknown_salary=INCLUDE).normalized()
    assert not rules.check(low)[0]
    assert rules.check(high)[0]


def test_the_top_of_a_range_is_what_clears_your_floor():
    """A $140-165k role clears a $150k floor: it can pay it."""
    posting = Posting(title="Engineer", salary="Salary range $140,000-$165,000")
    assert Requirements(salary_min=150_000).normalized().check(posting)[0]


def test_a_ceiling_screens_out_roles_that_start_too_high():
    posting = Posting(title="VP Engineering", salary="Base pay $300,000-$350,000")
    assert not Requirements(salary_max=250_000).normalized().check(posting)[0]


# --- salary parsing --------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$150,000 - $180,000 annually", (150_000, 180_000)),
    ("Salary range: $150K-$180K", (150_000, 180_000)),
    ("Compensation: USD 210,000", (210_000, 210_000)),
    ("The salary for this role is $120,000", (120_000, 120_000)),
])
def test_salary_shapes_that_appear_in_the_wild(text, expected):
    assert parse_salary(text) == expected


def test_an_hourly_rate_is_annualised_so_it_can_be_compared():
    low, high = parse_salary("Base pay $95/hour")
    assert low == high == 95 * 2080


def test_a_401k_is_not_a_401000_dollar_salary():
    """The classic false positive."""
    assert parse_salary("We offer 401k matching and a 403(b)") is None


def test_no_pay_information_reads_as_unknown_not_as_zero():
    assert parse_salary("Great team, competitive benefits") is None
    assert parse_salary("") is None


# --- employment type and clearance ----------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Senior Engineer (Contract)", "contract"),
    ("Full-time position", "full-time"),
    ("Summer Intern", "internship"),
    ("Part-time Analyst", "part-time"),
    ("Engineer", None),
])
def test_employment_type_detection(text, expected):
    assert detect_employment_type(text) == expected


def test_a_contract_role_is_dropped_when_you_asked_for_permanent():
    posting = Posting(title="Data Engineer (Contract)")
    rules = Requirements(employment_types=["full-time"]).normalized()
    accepted, reason = rules.check(posting)
    assert not accepted and "contract" in reason


@pytest.mark.parametrize("text,expected", [
    ("Requires an active TS/SCI clearance", True),
    ("Must possess an active Q-clearance", True),
    ("Clearance eligible; we will sponsor", False),
    ("Ability to obtain a clearance", False),
    ("Nothing about clearance here", None),
])
def test_clearance_detection_distinguishes_holding_from_obtaining(text, expected):
    assert requires_clearance(text) is expected


def test_clearance_unknown_is_your_call_too():
    posting = Posting(title="Engineer", summary="No mention either way.")
    assert Requirements(exclude_clearance_required=True,
                        unknown_clearance=INCLUDE).normalized().check(posting)[0]
    assert not Requirements(exclude_clearance_required=True,
                            unknown_clearance=EXCLUDE).normalized().check(posting)[0]


# --- titles ----------------------------------------------------------------

def test_title_word_screens():
    rules = Requirements(exclude_title_words=["sales", "intern"]).normalized()
    assert not rules.check(Posting(title="Enterprise Sales Engineer"))[0]
    assert rules.check(Posting(title="Senior Data Engineer"))[0]

    required = Requirements(require_title_words=["engineer", "scientist"]).normalized()
    assert required.check(Posting(title="Research Scientist"))[0]
    assert not required.check(Posting(title="Program Manager"))[0]


def test_an_invalid_unknown_policy_falls_back_to_the_safe_default():
    rules = Requirements(unknown_salary="maybe", unknown_location="whatever").normalized()
    assert rules.unknown_salary == INCLUDE      # a missing salary is worth a look
    assert rules.unknown_location == EXCLUDE    # a missing location, when you can't move, is not


def test_the_summary_states_the_unknown_policy_not_just_the_threshold():
    rules = Requirements(salary_min=150_000, unknown_salary=EXCLUDE).normalized()
    assert "no salary stated: exclude" in rules.summary()
