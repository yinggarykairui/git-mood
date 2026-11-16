#!/usr/bin/env python3
"""Width-bound and truncation-marker regression harness (#134 item 12).

git-mood's summary line is sized by hand: the fixed fields plus one elastic
field (the `--author` filter) must come to 80 cells or fewer, and `fit()` must
never hand back a string wider than its budget or one wearing a cut marker it
did not pay for. Three prior builds (days 051-053, #70/#132/#133/#134) chased
regressions in exactly this arithmetic, and the day-053 sign-off recorded that
its 19,500-case width proof ran *out of tree* -- so "the next word added to that
format string overflows silently" (#134 item 12). This file is that proof,
committed: stdlib `unittest`, zero dependencies, run with

    python3 -m unittest discover -s tests

The invariants, not the current outputs, are what is pinned here. A rung that
loses the noun naming the author filter (#134 item 1) or a `fit()` that returns
a two-dot stub (#134 item 5) fails a test below; the negative controls prove the
tests can see those failures rather than passing vacuously.
"""

import os
import sys
import types
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import git_mood as gm  # noqa: E402


def width(s):
    return gm.display_width(s)


def lone_dot_stub(s):
    """True if `s` ends in one or two dots but not a full three-dot marker --
    the `中..` shape fit() must never return (#134 item 5)."""
    trimmed = s.rstrip(".")
    return (len(s) - len(trimmed)) in (1, 2)


class FakeCommits(object):
    """render_summary() touches `commits` only as `len(commits)` on the
    `--author` branch, so a cheap object with __len__ drives `room` down to any
    scale without building millions of objects. (The author=None branch counts
    distinct emails and so iterates; it is tested separately with a real list.)"""

    def __init__(self, n):
        self._n = n

    def __len__(self):
        return self._n


def summary_room(ncommits, nweeks, start, today, g):
    """The budget render_summary() leaves the elastic filter field -- recomputed
    here so a test can assert a noun is present only where one can fit."""
    span = "%s %s %s" % (start.isoformat(), g["arrow"], today.isoformat())
    rest = [gm.count(ncommits, "commit"), gm.count(nweeks, "week"), span]
    return max(0, 80 - width(g["sep"].join(rest)) - width(g["sep"]))


def render(ncommits, nweeks, author, g, ascii_out):
    opts = types.SimpleNamespace(author=author)
    saved = gm.ASCII_OUT
    gm.ASCII_OUT = ascii_out
    try:
        return gm.render_summary(FakeCommits(ncommits), opts, nweeks,
                                 date(2005, 4, 11), date(2026, 8, 23), g)
    finally:
        gm.ASCII_OUT = saved


# A spread of scales, chosen to push `room` from comfortable down to the
# single digits the largest real repositories reach (torvalds/linux under
# `--all --ascii --author=` sits at room 19).
SCALES = [
    (1, 1), (148, 16), (5_000, 52), (90_000, 300),
    (1_300_000, 1_110), (100_000_000, 5_200), (10 ** 15, 52_000),
]
AUTHORS = ["", "ada", " ada ", "alexandra-barnes", "中" * 20, "日本語の名前",
           "a" * 60, "éxample", "\x07\x1f"]
MODES = [(gm.GLYPHS, False), (gm.ASCII_GLYPHS, True)]


class SummaryWidthBound(unittest.TestCase):
    def test_line_never_exceeds_80_cells(self):
        for g, ascii_out in MODES:
            for nc, nw in SCALES:
                for author in AUTHORS:
                    line = render(nc, nw, author, g, ascii_out)
                    self.assertLessEqual(
                        width(line), 80,
                        "line %r is %d cells (nc=%d nw=%d ascii=%s)"
                        % (line, width(line), nc, nw, ascii_out))

    def test_author_count_line_within_80_cells(self):
        # The author=None branch prints a distinct-author *count*, which iterates
        # commits; a modest real list exercises it (the 80-cell breach needs ten
        # million distinct authors, out of reach and documented as such).
        g = gm.GLYPHS
        commits = [types.SimpleNamespace(email="dev%d@example.com" % i)
                   for i in range(500)]
        opts = types.SimpleNamespace(author=None)
        line = gm.render_summary(commits, opts, 52, date(2005, 4, 11),
                                 date(2026, 8, 23), g)
        self.assertLessEqual(width(line), 80)
        self.assertIn("500 authors", line)

    def test_empty_author_filter_field_always_names_itself(self):
        # #134 item 1: for the *empty* filter there is no value to show, so when
        # the frame cannot be afforded the field still owes a noun saying it is
        # the author filter -- never a bare `"" (matches all)`. (A non-empty
        # author shows the quoted value, which is self-evidently the filter.)
        # The floor rungs below room 6 are a cut of the word "author" itself and
        # are out of reach of any repository, so this is scoped to room>=6.
        for g, ascii_out in MODES:
            for nc, nw in SCALES:
                room = summary_room(nc, nw, date(2005, 4, 11),
                                    date(2026, 8, 23), g)
                if room < 6:
                    continue
                line = render(nc, nw, "", g, ascii_out)
                who = line.split(g["sep"])[1]
                self.assertTrue(
                    who.startswith("author") or who.startswith("filter"),
                    "empty filter field %r carries no noun at room %d "
                    "(nc=%d ascii=%s)" % (who, room, nc, ascii_out))

    def test_empty_author_is_marked_as_matching_all(self):
        # The empty filter stops nothing; every rung must say so (the clarifier
        # is never spent) so a reader is not left hunting for removed commits.
        for g, ascii_out in MODES:
            for nc, nw in SCALES:
                room = summary_room(nc, nw, date(2005, 4, 11),
                                    date(2026, 8, 23), g)
                if room < 11:  # "author (all)" is 11 cells; below it, the floor
                    continue
                line = render(nc, nw, "", g, ascii_out)
                who = line.split(g["sep"])[1]
                self.assertIn("all", who,
                              "empty filter %r does not say it matches all"
                              % who)


class FitInvariants(unittest.TestCase):
    SAMPLES = ["", "a", "ab", "abcdef", "中", "中" * 30, "中a中a中",
               "é" * 10, "́́X", "héllo " + "x" * 40,
               "x" * 40, "\x07\x1f中", "a b  c   d", "😀" * 10, "名\\u"]

    def test_width_never_exceeds_budget(self):
        for ascii_out in (False, True):
            gm.ASCII_OUT = ascii_out
            for s in self.SAMPLES:
                for cells in range(0, 20):
                    out = gm.fit(s, cells)
                    self.assertLessEqual(
                        width(out), cells,
                        "fit(%r, %d) = %r is %d cells (ascii=%s)"
                        % (s, cells, out, width(out), ascii_out))
        gm.ASCII_OUT = False

    def test_no_two_dot_stub(self):
        # #134 item 5: a wide first character under a four-cell budget used to
        # return `中..`, a cut that looks marked and is not.
        for ascii_out in (False, True):
            gm.ASCII_OUT = ascii_out
            for s in self.SAMPLES:
                for cells in range(0, 20):
                    out = gm.fit(s, cells)
                    self.assertFalse(
                        lone_dot_stub(out),
                        "fit(%r, %d) = %r ends in a partial-dot stub (ascii=%s)"
                        % (s, cells, out, ascii_out))
        gm.ASCII_OUT = False
        self.assertEqual(gm.fit("中" * 30, 4), "中中")
        self.assertEqual(gm.fit("abcdef", 4), "a...")  # ASCII path unchanged

    def test_marker_is_paid_for(self):
        # A three-dot marker means a real cut was made: the text was wider than
        # the budget and the budget could afford the marker (>= MARKED_FLOOR).
        for ascii_out in (False, True):
            gm.ASCII_OUT = ascii_out
            for s in self.SAMPLES:
                for cells in range(0, 20):
                    out = gm.fit(s, cells)
                    if out.endswith("..."):
                        self.assertGreaterEqual(cells, gm.MARKED_FLOOR)
                        self.assertGreater(width(gm.flatten(s)), cells)
        gm.ASCII_OUT = False

    def test_whole_string_returned_when_it_fits(self):
        # If the flattened text fits the budget in cells, fit() returns it whole
        # and unmarked -- it does not truncate by character count and drop a
        # trailing zero-width mark (the bug sibling to item 5).
        for ascii_out in (False, True):
            gm.ASCII_OUT = ascii_out
            for s in self.SAMPLES:
                flat = gm.flatten(s)
                w = width(flat)
                for cells in range(w, w + 6):
                    self.assertEqual(
                        gm.fit(s, cells), flat,
                        "fit(%r, %d) did not return the whole fitting string"
                        % (s, cells))
        gm.ASCII_OUT = False


class NegativeControls(unittest.TestCase):
    """Prove the invariants above can see the failures they guard against, so a
    green suite means the checks ran rather than that they are vacuous."""

    def test_lone_dot_stub_detector_fires(self):
        self.assertTrue(lone_dot_stub("中.."))
        self.assertTrue(lone_dot_stub("name."))
        self.assertFalse(lone_dot_stub("name..."))
        self.assertFalse(lone_dot_stub("name"))

    def test_noun_check_would_reject_a_nounless_rung(self):
        # The pre-#134 ladder dropped to '"" (matches all)' -- no noun. The
        # assertion used in test_author_filter_field_always_names_itself would
        # reject it.
        nounless = '"" (matches all)'
        self.assertFalse(
            nounless.startswith("author") or nounless.startswith("filter"))

    def test_width_check_would_reject_an_overlong_line(self):
        self.assertGreater(width("x" * 81), 80)


if __name__ == "__main__":
    unittest.main()
