"""A whole board is free to read, so it has to be narrowed before it costs anything."""
from jobscout.filters import narrow_to_relevant, title_relevance
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


def test_a_short_board_is_never_trimmed():
    """Below the cap, an unusual title is exactly what a token overlap loses."""
    postings = [Posting(title=t) for t in
                ["Data Engineer", "Chief of Staff", "Molecular Modeller"]]
    kept, dropped = narrow_to_relevant(postings, PROFILE, keep=25)
    assert dropped == 0 and len(kept) == 3


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
