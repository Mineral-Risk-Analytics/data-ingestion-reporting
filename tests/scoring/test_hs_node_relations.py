"""Unit tests for the 11.1.A parent/child HS-mapping propagation helper.

The pure function ``_classify_hs_relations`` takes a normalised HS prefix
+ a list of (id, prefix) siblings (same-material mappings) and returns
``(parent_ids, child_ids)``.  Same-material filtering is the caller's
responsibility; here we test the prefix-relation logic in isolation.
"""

from __future__ import annotations

from app.services.scoring.hs_node_scorer import _classify_hs_relations


class TestNoSiblings:
    """When the material has no other mappings, both lists are empty."""

    def test_empty_siblings(self):
        parents, children = _classify_hs_relations("2601", [])
        assert parents == []
        assert children == []


class TestParentDetection:
    """A sibling whose prefix is a strict prefix of ours is a PARENT."""

    def test_four_digit_parent_of_six_digit(self):
        # We are 260111 (6-digit); 2601 is our 4-digit parent.
        parents, children = _classify_hs_relations(
            "260111",
            [(10, "2601")],
        )
        assert parents == [10]
        assert children == []

    def test_two_digit_grandparent(self):
        # We are 260111; 26 is the 2-digit chapter — also a parent.
        parents, children = _classify_hs_relations(
            "260111",
            [(20, "26")],
        )
        assert parents == [20]
        assert children == []

    def test_multiple_parents(self):
        # 260111 has both 2-digit ("26") and 4-digit ("2601") parents.
        parents, children = _classify_hs_relations(
            "260111",
            [(30, "26"), (31, "2601")],
        )
        assert sorted(parents) == [30, 31]
        assert children == []


class TestChildDetection:
    """A sibling whose prefix starts with ours and is longer is a CHILD."""

    def test_six_digit_child_of_four_digit(self):
        # We are 2601 (4-digit); 260111 is our 6-digit child.
        parents, children = _classify_hs_relations(
            "2601",
            [(40, "260111")],
        )
        assert parents == []
        assert children == [40]

    def test_multiple_children(self):
        # 2601 (iron ore chapter): 260111 (non-agglomerated fines),
        # 260112 (agglomerated pellets), 260120 (roasted pyrites).
        parents, children = _classify_hs_relations(
            "2601",
            [(50, "260111"), (51, "260112"), (52, "260120")],
        )
        assert parents == []
        assert sorted(children) == [50, 51, 52]

    def test_ten_digit_descendant(self):
        # We are 850760 (6-digit); 8507600020 is a US HTS 10-digit
        # specifier under it — also a child.
        parents, children = _classify_hs_relations(
            "850760",
            [(60, "8507600020")],
        )
        assert parents == []
        assert children == [60]


class TestMixedRelations:
    """Realistic case: ancestors AND descendants both present."""

    def test_four_digit_node_with_parent_and_children(self):
        # We are 2601; we have 26 as grandparent AND 260111/260112 as children.
        parents, children = _classify_hs_relations(
            "2601",
            [(70, "26"), (71, "260111"), (72, "260112")],
        )
        assert parents == [70]
        assert sorted(children) == [71, 72]

    def test_six_digit_node_with_parents_and_grandchild(self):
        # 260111 has 26 + 2601 parents; if a hypothetical 10-digit
        # 2601110000 child existed, both relations should be detected.
        parents, children = _classify_hs_relations(
            "260111",
            [(80, "26"), (81, "2601"), (82, "2601110000")],
        )
        assert sorted(parents) == [80, 81]
        assert children == [82]


class TestUnrelatedSiblings:
    """Sibling prefixes that are neither parent nor child are skipped."""

    def test_disjoint_chapter(self):
        # We are 2601 (iron ore); 7601 (aluminum unwrought) is unrelated.
        parents, children = _classify_hs_relations(
            "2601",
            [(90, "7601")],
        )
        assert parents == []
        assert children == []

    def test_sibling_under_same_chapter(self):
        # We are 260111 (non-agglomerated iron ore fines); 260112
        # (agglomerated pellets) is a SIBLING at the same level — NOT a
        # parent or child of 260111.  Should be skipped.
        parents, children = _classify_hs_relations(
            "260111",
            [(100, "260112")],
        )
        assert parents == []
        assert children == []

    def test_different_6digit_under_same_4digit(self):
        # Sibling case: 260111 and 260112 share parent 2601 but neither
        # is a parent/child of the other.
        parents, children = _classify_hs_relations(
            "260111",
            [(110, "260112"), (111, "260120")],
        )
        assert parents == []
        assert children == []


class TestEqualPrefixSkipped:
    """A sibling with the exact same prefix is a data duplicate; skipped."""

    def test_self_prefix_skipped(self):
        # In practice the unique constraint on hs_code_material_mappings
        # prevents this, but defensively the function ignores equal-prefix
        # siblings rather than classifying them as 'both parent and child'.
        parents, children = _classify_hs_relations(
            "2601",
            [(120, "2601")],
        )
        assert parents == []
        assert children == []


class TestRealisticBatteryScenarios:
    """End-to-end scenarios that mirror real seeded data shapes."""

    def test_lithium_battery_grade_with_ore_and_refined_siblings(self):
        # Real lithium HS seed: 2530.90 (ore), 282520 (refined Li metal),
        # 283691 (battery-grade Li2CO3).  When we score the battery-grade
        # 283691 node, none of the others are its parent/child — they
        # share no prefix.  This validates that intra-material mappings
        # at DIFFERENT 4-digit chapters do NOT propagate.
        parents, children = _classify_hs_relations(
            "283691",
            [(200, "253090"), (201, "282520")],
        )
        assert parents == []
        assert children == []

    def test_iron_ore_4digit_with_three_6digit_children(self):
        # Real iron ore HS seed: 2601 (4-digit), 260111 (non-agglom),
        # 260112 (agglomerated), 260120 (roasted).  Scoring the 4-digit
        # node should propagate events from all three 6-digit children.
        parents, children = _classify_hs_relations(
            "2601",
            [(300, "260111"), (301, "260112"), (302, "260120")],
        )
        assert parents == []
        assert sorted(children) == [300, 301, 302]

    def test_iron_ore_6digit_with_4digit_parent(self):
        # The reverse: scoring 260111 should see events tagged to 2601
        # propagate down.  This is the canonical "FR tariff on iron ore
        # chapter lifts non-agglomerated fines scoring" scenario.
        parents, children = _classify_hs_relations(
            "260111",
            [(400, "2601"), (401, "260112"), (402, "260120")],
        )
        assert parents == [400]
        # 260112 and 260120 are SIBLINGS (same parent, neither child of us)
        assert children == []
