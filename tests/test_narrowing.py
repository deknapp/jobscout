"""A whole board is free to read, so it has to be narrowed before it costs anything."""
from jobscout.filters import narrow_to_relevant, search_terms, title_relevance
from jobscout.models import Posting

PROFILE = {
    "target_titles": ["Data Engineer", "Senior Software Engineer"],
    "adjacent_titles": ["Research Software Engineer"],
    "core_skills": ["python", "pipelines", "cheminformatics"],
    "domains": ["scientific computing"],
}


def test_relevance_ignores_seniority_filler():
    assert title_relevance("Senior Data Engineer", PROFILE) == 1.0
    assert title_relevance("Staff Data Engineer II", PROFILE) == 1.0


def test_an_unrelated_title_scores_zero():
    assert title_relevance("Regional Sales Director", PROFILE) == 0.0
    assert title_relevance("Dental Hygienist", PROFILE) == 0.0


def test_a_tiny_board_is_not_ranked_down_to_its_top_few():
    """Small boards keep everything PLAUSIBLE, rather than the best `keep`.

    The point of the size check is that on a handful of roles it is cheaper to
    rank them all than to trust a token overlap to pick the best three.
    """
    postings = [Posting(title=t) for t in
                ["Data Engineer", "Research Software Engineer",
                 "Senior Software Engineer, Pipelines"]]
    kept, dropped = narrow_to_relevant(postings, PROFILE, keep=1)
    assert dropped == 0 and len(kept) == 3


def test_a_different_profession_is_dropped_even_on_a_tiny_board():
    """The bug this rule exists for.

    Eli Lilly contributed eight in-area roles, which is not more than
    SMALL_BOARD, so the whole board went through untouched: patent counsel, a
    litigation director, a global-medical-affairs director and a
    patient-advocacy director, each then verified and scored at cost, for a
    software engineer. Every one of them scored 0.00 relevance. A zero is not
    an idiosyncratic title, it is another profession.
    """
    board = [Posting(title=t) for t in [
        "Senior Software Engineer",
        "Associate VP-Assistant General Patent Counsel IP Procurement",
        "Senior Director, Counsel - Litigation, Legal Policy",
        "Senior Director, Global Medical Affairs",
        "Senior Director, Patient Advocacy"]]
    kept, dropped = narrow_to_relevant(board, PROFILE, keep=25)
    assert [p.title for p in kept] == ["Senior Software Engineer"]
    assert dropped == 4


def test_an_idiosyncratic_but_technical_title_still_survives():
    """What the size check was protecting, with a profile that has the words.

    "Member of Technical Staff" is a real software title and must not be lost
    to the zero rule; it is not zero against a profile that mentions technical
    work, which is the case the rule is judged on.
    """
    profile = dict(PROFILE, core_skills=PROFILE["core_skills"] + ["technical leadership"])
    board = [Posting(title=t) for t in
             ["Member of Technical Staff", "Regional Sales Director"]]
    kept, dropped = narrow_to_relevant(board, profile, keep=25)
    assert [p.title for p in kept] == ["Member of Technical Staff"]
    assert dropped == 1


def test_a_research_institute_board_loses_its_animal_technicians():
    """The real case: 24 in-area roles, almost none of them for this candidate."""
    board = [Posting(title=t) for t in [
        "Accounts Payable Specialist", "Animal Resources Technician",
        "Biosafety Officer", "Boiler Operator Maintenance Worker III",
        "Faculty Professor", "Histology Technician", "Staff Scientist",
        "Research Software Engineer", "Grants Administrator",
        "Facilities Manager", "Senior Data Engineer"]]
    kept, dropped = narrow_to_relevant(board, PROFILE, keep=25)
    titles = [p.title for p in kept]
    assert "Research Software Engineer" in titles
    assert "Senior Data Engineer" in titles
    assert "Animal Resources Technician" not in titles
    assert "Boiler Operator Maintenance Worker III" not in titles
    assert dropped == len(board) - len(kept)


def test_a_huge_board_keeps_the_plausible_roles():
    postings = ([Posting(title="Warehouse Associate %d" % i) for i in range(60)]
                + [Posting(title="Senior Data Engineer"),
                   Posting(title="Research Software Engineer, Chemistry")])
    kept, dropped = narrow_to_relevant(postings, PROFILE, keep=10)
    titles = [p.title for p in kept]
    assert "Senior Data Engineer" in titles
    assert "Research Software Engineer, Chemistry" in titles
    assert not any(t.startswith("Warehouse") for t in titles)
    assert dropped == 60


def test_no_profile_means_keep_everything():
    assert title_relevance("Anything At All", {}) == 1.0


# --- board search terms ----------------------------------------------------

#: The real profile that made Los Alamos look empty: titles written for a human,
#: full of seniority, slashes and parentheticals.
WRITTEN_PROFILE = {
    "target_titles": [
        "Senior Software Engineer",
        "Senior/Principal R&D AI (Artificial Intelligence)",
        "Computer Scientist / Scientist 3",
        "Senior Scientific Software Engineer",
        "Forward Deployed Engineer (AI)",
    ],
    "adjacent_titles": [
        "Platform Engineer, HPC & Distributed Systems",
        "Research Software Engineer",
    ],
}


def test_search_terms_are_role_nouns_not_whole_aspirations():
    terms = search_terms(WRITTEN_PROFILE)
    assert "engineer" in terms
    assert "scientist" in terms
    # The whole written title is exactly what matched nothing on a real board.
    assert not any(len(t.split()) > 2 for t in terms)
    assert "senior/principal r&d ai (artificial intelligence)" not in terms


def test_search_terms_drop_leftover_acronyms():
    """"(AI)" and "HPC" are not role names; as a board query they are noise."""
    terms = search_terms(WRITTEN_PROFILE)
    assert "ai" not in terms
    assert "hpc" not in terms


def test_the_broadest_terms_come_first():
    """A board search narrows on every extra word, so the head noun leads."""
    terms = search_terms(WRITTEN_PROFILE)
    assert terms[0] == "engineer"
    assert terms.index("engineer") < terms.index("software engineer")


def test_search_terms_match_the_real_lanl_titles_that_were_missed():
    """Substring-matched against the titles that scored zero in the field."""
    terms = search_terms(WRITTEN_PROFILE)
    for title in ["Software Developer (Scientist 2)",
                  "Computer Scientist (Scientist 1/2)",
                  "High-Power Experimental Electrical Engineer (R&D Engineer 2/3)"]:
        assert any(t in title.lower() for t in terms), title


def test_an_empty_profile_asks_for_nothing_in_particular():
    assert search_terms({}) == []


def test_a_descriptive_title_is_not_punished_for_its_length():
    """Dividing by the full word count made a lab title lose to a vague one.

    Both of these are real. The lab title says more about itself and was
    marked down for every extra word, which is how a board of specific
    scientific roles lost to a board of two-word startup titles.
    """
    lab = "Manufacturing Engineering Systems Analyst (Scientist 1)"
    assert title_relevance(lab, WRITTEN_PROFILE) >= 0.5


def test_a_descriptive_title_survives_narrowing_of_a_big_board():
    board = ([Posting(title="Warehouse Associate %d" % i) for i in range(40)]
             + [Posting(title="Manufacturing Engineering Systems Analyst "
                              "(Scientist 1)"),
                Posting(title="Software Developer (Scientist 2)")])
    kept, _dropped = narrow_to_relevant(board, WRITTEN_PROFILE, keep=10)
    titles = [p.title for p in kept]
    assert "Software Developer (Scientist 2)" in titles
    assert "Manufacturing Engineering Systems Analyst (Scientist 1)" in titles


def test_engineering_and_engineer_are_the_same_word():
    """A board full of "Engineering" titles scored zero against "Engineer"."""
    assert title_relevance("Engineering Manager, Data", PROFILE) > 0.0
