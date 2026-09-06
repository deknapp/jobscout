"""One employer under two names.

The interesting case is the one no string comparison can catch: a company that
renamed or was acquired, whose old and new domains share nothing.
"""
from jobscout.aliases import Aliases, infer


def test_variants_resolve_to_the_name_you_want_to_see(tmp_path):
    aliases = Aliases(tmp_path / "aliases.json")
    aliases.add("SandboxAQ", "sandboxquantum", "sandbox aq")
    assert aliases.canonical("sandboxquantum") == "SandboxAQ"
    assert aliases.canonical("SandboxAQ") == "SandboxAQ"
    assert aliases.key("sandboxquantum") == aliases.key("SandboxAQ")


def test_an_unknown_name_is_left_alone(tmp_path):
    aliases = Aliases(tmp_path / "aliases.json")
    assert aliases.canonical("Some Other Company") == "Some Other Company"


def test_aliases_round_trip(tmp_path):
    aliases = Aliases(tmp_path / "aliases.json")
    aliases.add("SandboxAQ", "sandboxquantum")
    aliases.save()
    assert Aliases(tmp_path / "aliases.json").canonical("sandboxquantum") == "SandboxAQ"


def test_one_person_writing_from_two_domains_is_proposed_as_a_merge():
    """This is what an acquisition looks like from outside: everybody's address
    changes at once and nothing else does."""
    proposed = infer([("jesper", "eyesopen"), ("jesper", "cadence"),
                      ("someone", "kestrel")])
    assert proposed == [("cadence", "eyesopen")]


def test_inference_proposes_and_never_writes():
    """A wrong merge is silent and hard to notice later, so a person confirms
    it. The function returns suggestions and nothing else."""
    assert infer([("a", "one"), ("a", "two")]) == [("one", "two")]
    assert infer([("a", "one")]) == []


def test_a_shared_relay_address_is_not_a_shared_person():
    """Every recruiter on a messaging platform writes from the same address, so
    its local part is the same string for all of them. Treating that as one
    person proposed merging three unrelated companies on the first real run —
    the caller must exclude relays, and this pins the shape of the failure."""
    as_if_relay = [("inmail-hit-reply", "Kestrel Bio"),
                   ("inmail-hit-reply", "Halcyon Labs"),
                   ("inmail-hit-reply", "Third Co")]
    assert len(infer(as_if_relay)) == 3      # what happens if you do not exclude them
    assert infer([]) == []
