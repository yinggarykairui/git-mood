#!/usr/bin/env python3
"""git-mood - a terminal mood chart for a git repository.

One file, standard library only. One `git log` call in, four panels out.
Stat functions take commits and return numbers; render functions take numbers
and return strings. Nothing computes and renders at the same time.
"""

import math
import os
import signal
import subprocess
import sys
import unicodedata
from collections import namedtuple
from datetime import date, timedelta

PROG = "git-mood"
VERSION = "1.0"

# Exit codes are part of the CLI contract: 0 a chart or a clean "nothing to
# chart", 1 environment, 2 usage, 130 Ctrl-C.
EXIT_ENV = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130

HELP = """git-mood — a terminal mood chart for a git repository

usage: git-mood [path] [options]

  path              a git repository, or any directory inside one
                    (default: the current directory)

options:
  -w, --weeks N     how many weeks back to read (default: 26, max: 520)
  -a, --all         read the current branch's entire history; wins over --weeks
      --author STR  keep commits whose "Name <email>" contains STR
                    (case-insensitive substring, not a regex; STR may
                    span the brackets: --author "lace <ada")
      --ascii       draw with plain ASCII instead of block characters
      --no-color    never emit ANSI color (also honors NO_COLOR)
  -h, --help        show this and exit
  -V, --version     show the version and exit

Times are the author's own local clock, exactly as recorded in each commit.
Nothing is converted to your timezone.

The mood tags are nicknames for numbers, not psychology. Every tag that
fires on a threshold prints the measured number and the line it crossed,
so you can disagree with it. "unremarkable" is the one tag with no
arithmetic to show, because nothing crossed anything.
"""

MAX_WEEKS = 520
GUTTER = 8           # width of the "tempo   " / "clock   " label column
INDENT = " " * GUTTER
SPARK_COLS = 52       # sparkline never grows past this; it buckets instead
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RULER = "0  3  6  9  12 15 18 21 "   # exactly 24 columns, one per hour

GLYPHS = {
    "spark": "▁▂▃▄▅▆▇█",
    "spark_zero": "·",
    "grid_zero": "·",
    "grid": "░▒▓█",
    "rule": "─",
    "sep": " · ",
    "arrow": "→",
    "dash": "—",
}
ASCII_GLYPHS = {
    # The ramp climbs like the Unicode one: a dot for a week with nothing in
    # it (the same low-ink mark the punch card uses for an empty cell), then
    # a baseline stroke for the shortest bar. The old set had these two the
    # other way round, so an --ascii sparkline drew dead weeks as solid bars
    # and busy weeks as gaps - the picture upside down. The top of the ramp
    # was inverted too: "%" carries less ink than "#" in every monospace
    # face, so the tallest bar - the one the caption points at - printed
    # lighter than the bar beside it. "#" is the top step.
    "spark": "_:-=+*%#",
    "spark_zero": ".",
    "grid_zero": ".",
    "grid": "-=+#",
    "rule": "-",
    "sep": " | ",
    "arrow": "->",
    "dash": "-",
}

Commit = namedtuple("Commit", "date hour weekday name email")
Options = namedtuple("Options", "path weeks whole author ascii_ color")


class Usage(Exception):
    """Bad command line. Exit 2, with a pointer at --help."""


class EnvProblem(Exception):
    """No git, no directory, no repo, or git itself failed. Exit 1."""


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def tame(text):
    """Control characters out. A repo directory named with an embedded ESC
    would otherwise recolor the caller's terminal from our own header."""
    return "".join("?" if ch < " " or ch == "\x7f" else ch for ch in str(text))


def oneline(text, limit=60):
    """Errors are one line, so user data never breaks the format.

    Below four characters there is no room for the ellipsis, and adding one
    anyway returned a string longer than the limit asked for - oneline("abcdef",
    2) came back as "abcde...". Under that floor the text is simply cut.
    """
    flat = tame(" ".join(str(text).split()))
    if len(flat) <= limit:
        return flat
    if limit < 4:
        return flat[:max(limit, 0)]
    return flat[:limit - 3] + "..."


def display_width(text):
    """Terminal cells, not codepoints. A rule sized in codepoints came up
    short under any name holding wide characters: five party poppers are five
    codepoints and ten cells, so the rule drew 68 under a 73-cell line."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text, cells):
    """oneline(), measured in cells, so the result really is that wide.

    The trim loop used to stop at three characters while the test it was
    trying to satisfy counted cells, so any text of three wide characters or
    fewer was never trimmed at all: fit("XXX", 4) handed back six cells. Now
    the loop runs on the same measure as the test, and the ellipsis is dropped
    rather than paid for when the budget cannot hold both.
    """
    flat = oneline(text, cells)
    if display_width(flat) <= cells:
        return flat
    tail = "..." if cells >= 4 else ""
    room = cells - len(tail)
    while flat and display_width(flat) > room:
        flat = flat[:-1]
    return flat + tail


def flag_shaped(arg):
    """`--all` and `-a` are flags; `-3` and `-` are values."""
    return len(arg) > 1 and arg[0] == "-" and (arg[1] == "-" or arg[1].isalpha())


def take_value(flag, inline, argv, i, literal=""):
    """Return (value, next index) for both `--flag=v` and `--flag v`.

    `flag` is the token the user actually typed, so `-w` never reports itself
    as `--weeks`. A following flag is refused rather than eaten: swallowing it
    used to produce advice ("try --all") naming the flag it had just consumed.

    `literal` is what the flag would do with a value shaped like a flag, if
    anything. `--weeks --all` used to advise `--weeks=--all`, which fails on
    the next line with `got "--all"`; advice that does not work is worse than
    none, so only the flags that can take such a value offer it.
    """
    if inline is not None:
        return inline, i
    if i >= len(argv):
        raise Usage("%s needs a value" % flag)
    if flag_shaped(argv[i]):
        seen = oneline(argv[i], 24)
        note = "%s needs a value; %s is a flag" % (flag, seen)
        if literal:
            note += " (use %s=%s to %s)" % (flag, seen, literal)
        raise Usage(note)
    return argv[i], i + 1


def parse_weeks(raw):
    try:
        n = int(raw)
    except ValueError:
        raise Usage('--weeks needs an integer from 1 to %d, got "%s"'
                    % (MAX_WEEKS, oneline(raw, 24)))
    if not 1 <= n <= MAX_WEEKS:
        raise Usage('--weeks must be from 1 to %d, got "%s"'
                    % (MAX_WEEKS, oneline(raw, 24)))
    return n


def parse_args(argv):
    """Hand-rolled so --help is a verbatim string and every input is spec'd.

    Clustering (`-aw 4`) is deliberately not split; it reports as an unknown
    option rather than guessing.
    """
    path, weeks, whole, author, ascii_, color = None, 26, False, None, False, True
    i, only_paths = 0, False
    while i < len(argv):
        arg = argv[i]
        i += 1
        flag, eq, inline = arg.partition("=")
        inline = inline if eq else None
        if only_paths:
            if path is not None:
                raise Usage("unexpected argument: %s" % oneline(arg, 24))
            path = arg
        elif arg == "--":
            only_paths = True          # everything after is a path, even `-x`
        elif arg in ("-h", "--help"):
            emit(HELP)
            raise SystemExit(0)
        elif arg in ("-V", "--version"):
            emit("%s %s\n" % (PROG, VERSION))
            raise SystemExit(0)
        elif arg in ("-a", "--all"):
            whole = True
        elif arg == "--ascii":
            ascii_ = True
        elif arg == "--no-color":
            color = False
        elif flag in ("-w", "--weeks"):
            raw, i = take_value(flag, inline, argv, i)
            weeks = parse_weeks(raw)
        elif flag == "--author":
            author, i = take_value(flag, inline, argv, i,
                                   "search for it as text")
        elif arg.startswith("-") and arg != "-":
            raise Usage("unknown option: %s" % oneline(arg, 24))
        elif path is None:
            path = arg
        else:
            raise Usage("unexpected argument: %s" % oneline(arg, 24))
    return Options(path or ".", weeks, whole, author, ascii_, color)


# --------------------------------------------------------------------------
# reading git
# --------------------------------------------------------------------------

def run_git(args, timeout=120):
    env = dict(os.environ)
    # As the `git mood` subcommand git exports these; left in place they make
    # the child read the caller's repo instead of the path argument.
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_PREFIX"):
        env.pop(var, None)
    cmd = ["git", "-c", "log.showSignature=false", "-c", "color.ui=false"] + args
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=timeout)
    except FileNotFoundError:
        raise EnvProblem("git not found on PATH")
    except subprocess.TimeoutExpired:
        raise EnvProblem("git timed out after %d seconds" % timeout)
    except OSError as exc:
        raise EnvProblem("could not run git: %s" % oneline(exc))


def git_says(done, limit=120):
    """git's own first line of complaint, or "" when it said nothing.

    Only the first line: the rest of a git error is usually a worked example
    indented under it, and this program's errors are one line each.
    """
    for line in done.stderr.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            return oneline(line, limit)
    return ""


def check_directory(path):
    """Say which of the three ways a path can fail actually happened."""
    try:
        os.stat(path)
    except FileNotFoundError:
        raise EnvProblem("no such directory: %s" % oneline(path))
    except NotADirectoryError:
        raise EnvProblem("not a directory: %s" % oneline(path))
    except PermissionError:
        raise EnvProblem("permission denied: %s" % oneline(path))
    except OSError as exc:
        raise EnvProblem("cannot read %s: %s" % (oneline(path, 30),
                                                 oneline(exc.strerror, 30)))
    if not os.path.isdir(path):
        raise EnvProblem("not a directory: %s" % oneline(path))
    if not os.access(path, os.R_OK | os.X_OK):
        raise EnvProblem("permission denied: %s" % oneline(path))


def resolve_repo(path):
    """Return the directory whose basename names the repo in the header.

    A bare repo has no work tree, so --show-toplevel fails there; fall back to
    the git directory itself rather than calling it "not a repository".
    """
    check_directory(path)
    done = run_git(["-C", path, "rev-parse", "--show-toplevel"])
    if done.returncode == 0:
        top = done.stdout.decode("utf-8", "replace").strip()
        if top:
            return top
    done = run_git(["-C", path, "rev-parse", "--git-dir"])
    if done.returncode != 0:
        # Relabelling every nonzero exit "not a git repository" told the
        # owner of a repo they cannot read that their repo is not a repo.
        # git's own sentence carries the reason and, for the everyday case
        # of a directory owned by somebody else, names the fix.
        reason = git_says(done)
        if reason and "not a git repository" not in reason.lower():
            raise EnvProblem("git says: %s" % reason)
        raise EnvProblem("not a git repository: %s" % oneline(path))
    git_dir = done.stdout.decode("utf-8", "replace").strip()
    top = os.path.abspath(os.path.join(path, git_dir))
    # `.../repo/.git` names the repo `repo`; `.../repo.git` names itself.
    return os.path.dirname(top) if os.path.basename(top) == ".git" else top


def has_commits(top):
    """A fresh `git init` is not an error, so ask before running the log."""
    return run_git(["-C", top, "rev-parse", "--quiet", "--verify",
                    "HEAD"]).returncode == 0


def commits_elsewhere(top):
    """Is anything committed on some other ref while HEAD points nowhere?

    `git switch -c` before the first commit leaves HEAD unborn on the new
    branch while main still holds the history, and calling that repository
    "no commits yet" contradicts its own `git log main`.
    """
    done = run_git(["-C", top, "rev-list", "-n", "1", "--all"])
    return done.returncode == 0 and bool(done.stdout.strip())


def read_commits(top):
    """The one `git log` call. Bytes in, (Commit list, undateable count) out.

    `-z` makes git separate the records with NUL. git refuses to write a
    commit object holding a NUL in the author ident (`fsck` calls it
    nulInHeader, and fast-import stops at the first one), so a record boundary
    is the one thing in this stream an author name cannot counterfeit. The
    field separators inside a record still can, which is why the name is
    reassembled from the middle fields rather than trusted to be one.

    No `--since` prefilter: it cuts on *committer* date, so a rebased or
    grafted commit whose author date is inside the window would vanish from a
    windowed run but show up under --all. The author-date filter in build() is
    the only authority on membership, so every panel counts the same commits.
    """
    args = ["-C", top, "log", "-z", "--pretty=format:%aI%x1f%aN%x1f%aE"]
    done = run_git(args)
    if done.returncode != 0:
        raise EnvProblem("git log failed: %s"
                         % (git_says(done) or "no reason given"))
    return parse_log(done.stdout.decode("utf-8", "replace"))


def stamp_parts(stamp):
    """(date, hour) from an %aI string, or None if it is not one."""
    try:
        day = date.fromisoformat(stamp[:10])
        hour = int(stamp[11:13])
    except (ValueError, IndexError):
        return None
    if not 0 <= hour <= 23:
        return None
    return day, hour


def parse_log(text):
    """One NUL-separated record in, one Commit out. No rejoining.

    Returns (commits, undated), where `undated` holds the raw author stamp of
    every record that could not be placed on a calendar. There are two ways
    in, and they are different facts about the repository, so the strings are
    kept rather than tallied: git leaves its own placeholder "%aI" in place
    when it cannot read the author timestamp at all (a negative one, from
    before 1970, does this), and an author timestamp far in the future renders
    fine as "3170843-11-07T09:46:39+00:00", a year no calendar covers. Either
    way the record belongs to no week and no hour, so it is set aside and the
    caller says so; dropping it in silence once turned a repo into "no commits
    yet".

    Because the record boundary is a NUL (see read_commits) every record is
    exactly one commit, so field 0 is always git's own %aI and no author name
    can invent a commit or erase one. A name holding \\x1f still splits into
    extra fields; they are joined back into the name by taking the first field
    as the stamp and the last as the email, so at worst an absurd name blurs
    into the email it is printed beside.

    A newline is treated as a record boundary too. That is what git uses when
    `-z` is not honored, and git refuses an author ident containing one
    (`missingEmail` from fsck, "Missing < in ident string" from fast-import),
    so it can only ever be git's own separator and never part of a name.
    """
    commits, undated = [], []
    for record in text.replace("\n", "\x00").split("\x00"):
        if not record:
            continue
        fields = record.split("\x1f")
        when = stamp_parts(fields[0]) if len(fields) >= 3 else None
        if when is None:
            undated.append(fields[0])
            continue
        name, email = "\x1f".join(fields[1:-1]), fields[-1]
        commits.append(Commit(when[0], when[1], when[0].weekday(), name, email))
    return commits, undated


def undated_notes(undated):
    """One line per reason a commit could not be dated, naming the real one.

    The old single line said the dates were ones "git could not render",
    which is wrong twice over: git renders %aI for an author timestamp of
    99999999999999 quite happily (it prints the year 3170843) and it is this
    program that refuses it, and "render" reads as "draw" to anyone who has
    not read the source. Each cause now says what actually happened.
    """
    unread = sum(1 for stamp in undated if stamp.strip() == "%aI")
    unreal = len(undated) - unread
    notes = []
    if unreal:
        notes.append("skipped %s whose author date is not a real calendar date"
                     % count(unreal, "commit"))
    if unread:
        notes.append("skipped %s whose author date git itself could not read"
                     % count(unread, "commit"))
    return ["%s: %s\n" % (PROG, note) for note in notes]


# --------------------------------------------------------------------------
# stats: commits in, numbers out
# --------------------------------------------------------------------------

def window_start(weeks, whole, days, today):
    """Monday-aligned, so 'the week of <date>' always names a Monday.

    Returns (start Monday, number of weeks). The window ends today; commits
    dated in the future clamp into the newest bucket.
    """
    monday = today - timedelta(days=today.weekday())
    if whole:
        first = min(days) if days else today
        start = min(first - timedelta(days=first.weekday()), monday)
        return start, (monday - start).days // 7 + 1
    return monday - timedelta(days=7 * (weeks - 1)), weeks


def weekly_counts(commits, start, nweeks):
    weekly = [0] * nweeks
    for commit in commits:
        index = (commit.date - start).days // 7
        weekly[min(max(index, 0), nweeks - 1)] += 1
    return weekly


def punch_card(commits):
    grid = [[0] * 24 for _ in range(7)]
    for commit in commits:
        grid[commit.weekday][commit.hour] += 1
    return grid


def streaks(days, today):
    """Return (longest, its start, its end, current, current end)."""
    ordered = sorted(days)
    best, best_start, best_end = 0, None, None
    run, run_start, prev = 0, None, None
    for day in ordered:
        if prev is not None and (day - prev).days == 1:
            run += 1
        else:
            run, run_start = 1, day
        if run > best:
            best, best_start, best_end = run, run_start, day
        prev = day
    anchor = None
    for candidate in (today, today - timedelta(days=1)):
        if candidate in days:
            anchor = candidate
            break
    current, cursor = 0, anchor
    while cursor is not None and cursor in days:
        current += 1
        cursor -= timedelta(days=1)
    return best, best_start, best_end, current, anchor


def share(part, total):
    """The exact percentage. Thresholds are tested against this, not a
    rounded copy of it, so a tag can never fire below its own line."""
    return part * 100.0 / total if total else 0.0


def pct(value, line):
    """A whole percent for reading that never contradicts the chart.

    Rounding alone printed "100%" for 200 of 201 commits while the punch card
    three lines above showed the odd one lit, so the two ends are held back:
    100 is reserved for "all of them" and 0 for "none of them". Between those,
    the nearest whole percent. `line` is the threshold the tag quoted; the tag
    itself fires on the exact `value`, and for every integer line the rounded
    number already lands on or above it, so nothing has to be pushed up.
    """
    shown = int(round(value))
    if shown >= 100 and value < 100:
        shown = 99
    if shown <= 0 and value > 0:
        shown = 1
    return shown


def floor1(value):
    """One decimal, rounded down: the printed number is never larger than the
    measured one, so `2.0x` can never appear under a `< 2x` rule."""
    return "%.1f" % (math.floor(value * 10) / 10.0)


def median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mood(commits, weekly, nweeks, current, last_day, today):
    """Up to three tags, each with exactly one evidence line.

    Every threshold is tested against the exact measurement and only then
    formatted for print, so no tag fires below the line it quotes and no
    evidence line contradicts its own rule.
    """
    tags, evidence = [], []
    total = len(commits)
    nonempty = [w for w in weekly if w > 0]
    mid = median(nonempty)
    peak = max(weekly) if weekly else 0
    ratio = peak / mid if mid else 0.0

    night = share(sum(1 for c in commits if c.hour < 6), total)
    weekend = share(sum(1 for c in commits if c.weekday >= 5), total)
    office = share(sum(1 for c in commits
                       if c.weekday < 5 and 9 <= c.hour < 18), total)
    covered = share(len(nonempty), nweeks)
    idle = (today - last_day).days

    candidates = [
        (night >= 20, "nocturnal",
         "%d%% of commits land between 00:00 and 05:59 (line: 20%%)"
         % pct(night, 20)),
        (weekend >= 25, "weekend-coded",
         "%d%% of commits land on a Saturday or Sunday (line: 25%%)"
         % pct(weekend, 25)),
        (office >= 60, "nine-to-five",
         "%d%% of commits land Mon-Fri, 09:00-17:59 (line: 60%%)"
         % pct(office, 60)),
        (len(nonempty) >= 4 and ratio >= 3.0, "burst-driven",
         "the busiest week holds %sx the median week (line: 3x)"
         % floor1(ratio)),
        # Two thresholds, one of them a `<` bound, and the only line in the
        # program that ever passed 80 columns - at --weeks 520 the old
        # wording reached 83 and dropped a lone ")" at column 0. "lines"
        # plural, and "under 2x" says which way the second one points.
        (nweeks >= 4 and covered >= 60 and ratio < 2.0, "metronomic",
         "%d of %d weeks busy, peak %sx the median (lines: 60%%, under 2x)"
         % (len(nonempty), nweeks, floor1(ratio))),
        (current >= 5, "on a tear",
         "%d days in a row with at least one commit (line: 5)" % current),
        (idle >= 21, "dormant",
         "nothing committed in %d days (line: 21)" % idle),
    ]
    for fired, tag, line in candidates:
        if fired:
            tags.append(tag)
            evidence.append(line)
        if len(tags) == 3:
            break
    if not tags:
        tags.append("unremarkable")
        evidence.append("nothing in these numbers crosses a line")
    return tags, evidence


# --------------------------------------------------------------------------
# rendering: numbers in, strings out
# --------------------------------------------------------------------------

class Ink(object):
    """Three uses of color, or none at all."""

    def __init__(self, enabled):
        self.enabled = enabled

    def _wrap(self, code, text):
        return "\x1b[%sm%s\x1b[0m" % (code, text) if self.enabled else text

    def dim(self, text):
        return self._wrap("2", text)

    def bold(self, text):
        return self._wrap("1", text)

    def accent(self, text):
        return self._wrap("36", text)


def color_enabled(opts):
    if not opts.color:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def ascii_only(opts, glyphs):
    if opts.ascii_:
        return True
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(glyphs.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def count(n, word):
    return "%s %s" % ("{:,}".format(n), word if n == 1 else word + "s")


def ramp_glyph(value, top, ramp):
    """Quantize against the panel's own maximum. Zero never reaches here:
    both panels have a zero glyph of their own."""
    if value <= 0 or top <= 0:
        return ramp[0]
    index = int(math.ceil(value * len(ramp) / float(top)))
    return ramp[min(max(index, 1), len(ramp)) - 1]


def cell_glyph(value, top, g):
    if value <= 0:
        return g["grid_zero"]
    return ramp_glyph(value, top, g["grid"])


def spark_glyph(value, top, g):
    """A week with no commits gets its own low-ink glyph, not the shortest bar.

    Otherwise a repo three days old reads as six months of steady work.
    """
    if value <= 0:
        return g["spark_zero"]
    return ramp_glyph(value, top, g["spark"])


def gutter(label):
    return label.ljust(GUTTER)


def render_head(name, summary, g):
    """Name, rule, counts line. The rule is 60 wide but never shorter than
    the line it underlines, which `--all` can push past 60.

    The name is cut to fit that 60, since a repo directory is free to be 120
    characters long and was hanging that far past its own rule.
    """
    title = "%s  %s" % (PROG, fit(name, 60 - len(PROG) - 2))
    lines = [title, g["rule"] * max(60, display_width(summary))]
    return lines + [summary] if summary else lines


def render_summary(commits, opts, nweeks, start, today, g):
    """One line, and never wider than 80 cells: the rule is drawn to match it.

    Only the filter text is elastic, so it is the one that gives way. Cutting
    it to a flat 30 characters was not enough - the rest of the line is 49
    cells before the author has said a word, and a 30-character name pushed
    the whole thing, and the rule under it, to 93.
    """
    span = "%s %s %s" % (start.isoformat(), g["arrow"], today.isoformat())
    rest = [count(len(commits), "commit"), count(nweeks, "week"), span]
    if opts.author is None:
        who = count(len(set(c.email.lower() for c in commits)), "author")
    else:
        # "was the flag given", not "is the value truthy": --author= is a
        # filter to the empty string, not an absent filter.
        room = 80 - display_width(g["sep"].join(rest)) - display_width(g["sep"])
        if opts.author == "":
            # The empty string is a substring of every ident, so this filter
            # is applied and stops nothing. Announcing it without saying so
            # left the reader hunting for the commits it had removed.
            who = 'filtered to "" (matches all)'
            if display_width(who) > room:
                who = 'filtered to ""'
        else:
            who = 'filtered to "%s"' % fit(opts.author,
                                           min(30, max(4, room - 14)))
    return g["sep"].join([rest[0], who] + rest[1:])


def bucket_columns(weekly, size):
    """[(first week index, weeks in the column, commits)], oldest first.

    A window that does not divide evenly leaves one short column; it is put at
    the *oldest* end, so the newest bar is never a partial bucket masquerading
    as a decline.
    """
    lead = len(weekly) % size
    edges = ([(0, lead)] if lead else []) + [(i, size) for i in
                                             range(lead, len(weekly), size)]
    return [(i, n, sum(weekly[i:i + n])) for i, n in edges]


def per_week(total, nweeks):
    """One decimal, always, so the number reads the same at both scales.

    Switching to two significant figures under 0.1 printed "0.038 commits/
    week" next to "8.0 commits/week" elsewhere - two different rules on the
    same caption line. The reason for the switch stands, though: a repo that
    really has commits must never print 0.0. So the low end says "<0.1",
    which is one decimal and still cannot be read as none.
    """
    average = total / float(nweeks) if nweeks else 0.0
    if 0 < average < 0.05:
        return "<0.1"
    return "%.1f" % average


def render_tempo(weekly, start, clamped, ink, g):
    nweeks = len(weekly)
    size = int(math.ceil(nweeks / float(SPARK_COLS)))
    columns = bucket_columns(weekly, size)
    top = max(c[2] for c in columns)
    bar = "".join(spark_glyph(c[2], top, g) for c in columns)

    # Caption the peak *column*, the bar a reader can actually point at -
    # but only when there is one to point at. On a flat repo every week held
    # the same count and max() picked the oldest, so a chart with no shape at
    # all read as "something happened in February".
    peak = top
    tied = [c for c in columns if c[2] == peak]
    unit = "week" if size == 1 else "column"
    if len(tied) == 1:
        first, span = tied[0][0], tied[0][1]
        when = (start + timedelta(days=7 * first)).isoformat()
        where = "the week of %s" % when if span == 1 else \
                "the %s from %s" % (count(span, "week"), when)
        peak_line = "peak %d in %s" % (peak, where)
    elif len(tied) == len(columns):
        peak_line = "every %s holds %d" % (unit, peak)
    else:
        peak_line = "peak %d, tied across %s" % (peak, count(len(tied), unit))
    # bucket_columns leaves the short column, if there is one, at the oldest
    # end. Saying only "one column = 16 weeks" made that 9-week bar read as a
    # lull, so the odd column is named whenever it exists.
    width = count(size, "week")
    if columns and columns[0][1] != size:
        width += " (the oldest holds %d)" % columns[0][1]
    lines = [
        ink.dim(gutter("tempo")) + bar,
        ink.dim(INDENT + "one column = %s%s%s commits/week"
                % (width, g["sep"], per_week(sum(weekly), nweeks))),
        ink.dim(INDENT + peak_line),
    ]
    if clamped:
        # Future-dated commits clamp into the newest bucket, which is rarely
        # the peak column. Hung off the peak caption, the note claimed they
        # were in a column that does not hold them; it gets its own line and
        # names the column that does.
        lines.append(ink.dim(INDENT + "%s dated after today, counted in the "
                             "newest column" % count(clamped, "commit")))
    return lines


def render_clock(grid, ink, g):
    top = max(max(row) for row in grid)
    lines = [ink.dim((gutter("clock") + "     " + RULER).rstrip())]
    for index, day in enumerate(DAYS):
        # Hours 00:00-05:59 carry the one accent color, so a late-night spike
        # is visible without reading the ruler. Only cells that hold commits
        # are tinted; tinting the empty ones paints a cyan rectangle that
        # reads as a rendering artifact rather than an accent.
        cells = []
        for hour, value in enumerate(grid[index]):
            glyph = cell_glyph(value, top, g)
            cells.append(ink.accent(glyph) if hour < 6 and value else glyph)
        lines.append(ink.dim(INDENT + day + "  ") + "".join(cells))
    lines.append(ink.dim(INDENT + "one cell per hour of the week" + g["sep"]
                         + "darkest = %s" % count(top, "commit")))
    lines.append(ink.dim(INDENT + "author-local time, exactly as recorded "
                                  "in each commit"))
    return lines


def render_streaks(best, best_start, best_end, current, anchor, last_day,
                   clamped, ink, g):
    """Dates here are the commits' own, never clamped into the window.

    The header says the window ends today and tempo folds future-dated
    commits into the newest column, so a streak reported in September beside
    a header ending in August looked like three panels disagreeing. It is the
    same disclosure tempo makes, in the panel that needs it.
    """
    if best_start == best_end:
        longest = "longest %s, %s" % (count(best, "day"),
                                      best_start.isoformat())
    else:
        longest = "longest %s, %s %s %s" % (count(best, "day"),
                                            best_start.isoformat(),
                                            g["arrow"], best_end.isoformat())
    if current:
        now = "current %s, through %s" % (count(current, "day"),
                                          anchor.isoformat())
    else:
        now = "current none, last commit %s" % last_day.isoformat()
    lines = [ink.dim(gutter("streaks")) + longest, INDENT + now]
    if clamped:
        lines.append(ink.dim(INDENT + "%s dated after today, so these dates "
                             "can run ahead of it" % count(clamped, "commit")))
    return lines


def render_mood(tags, evidence, ink, g):
    lines = [ink.dim(gutter("mood"))
             + g["sep"].join(ink.bold(tag) for tag in tags)]
    lines.extend(INDENT + line for line in evidence)
    return lines


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def write(stream, text, ascii_):
    """A repo or author name can hold characters the stream cannot encode.

    Replace them instead of raising; under --ascii force plain ASCII so the
    whole output stays below U+0080 whatever the repo is called. A stream that
    is closed (`git-mood >&-` leaves sys.stdout as None) is not a crash.
    """
    if stream is None:
        return False
    encoding = "ascii" if ascii_ else (getattr(stream, "encoding", None) or "utf-8")
    try:
        text = text.encode(encoding, "replace").decode(encoding, "replace")
    except LookupError:
        text = text.encode("ascii", "replace").decode("ascii")
    try:
        stream.write(text)
    except (ValueError, OSError):
        return False
    return True


def emit(text, ascii_=False):
    """Print to stdout, or say so on stderr if stdout is gone. Never raise."""
    if not write(sys.stdout, text, ascii_):
        raise EnvProblem("could not write to stdout")


def window_words(opts, nweeks):
    """Name the window the numbers describe, so a dead end says which one."""
    if opts.whole:
        return "the whole history", ""
    return "the last %s" % count(nweeks, "week"), " try --all."


def build(opts, today, g, ink):
    top = resolve_repo(opts.path)
    name = os.path.basename(top.rstrip(os.sep)) or top
    if name.endswith(".git") and len(name) > len(".git"):
        # A bare repo lives in `name.git`; the repository is still `name`.
        name = name[:-len(".git")]
    name = tame(name)

    def page(summary, message):
        """Header, then exactly one line. Never a half-drawn chart."""
        head = render_head(name, summary, g)
        return head + ["", message] if summary else head + [message]

    nothing = "no commits yet %s nothing to chart." % g["dash"]
    if not has_commits(top):
        if commits_elsewhere(top):
            return page("", "no commits on the current branch %s nothing "
                        "to chart." % g["dash"])
        return page("", nothing)

    commits, undated = read_commits(top)
    for note in undated_notes(undated):
        # These belong to no week and no hour, so they cannot go on a panel.
        # The note goes to stderr: it survives `| head -1`, and it cannot
        # change what any number already on the chart means.
        write(sys.stderr, note, True)
    if not commits:
        if undated:
            return page("", "%s here, none with a real calendar date %s "
                        "nothing to chart."
                        % (count(len(undated), "commit"), g["dash"]))
        return page("", nothing)

    start, nweeks = window_start(opts.weeks, opts.whole,
                                 [c.date for c in commits], today)
    commits = [c for c in commits if c.date >= start]
    where, advice = window_words(opts, nweeks)
    if not commits:
        return page(render_summary([], opts, nweeks, start, today, g),
                    "no commits in %s.%s" % (where, advice))
    if opts.author is not None:
        # The needle is tested against the whole `Name <email>` ident, the
        # same string git log prints, so `--author "lace <ada"` can match
        # across the boundary. --help says so rather than promising a
        # name-or-email match this does not perform.
        needle = opts.author.lower()
        commits = [c for c in commits
                   if needle in ("%s <%s>" % (c.name, c.email)).lower()]
        if not commits:
            # A needle that matched nobody at all is not a window problem,
            # so "try --all." on its own was pointing at the wrong knob.
            fix = " try --all, or a shorter --author." if advice \
                  else " try a shorter --author."
            shape = 'no commits by "%s" in %s.%s'
            room = 80 - display_width(shape % ("", where, fix))
            return page(render_summary([], opts, nweeks, start, today, g),
                        shape % (fit(opts.author, max(4, min(30, room))),
                                 where, fix))

    weekly = weekly_counts(commits, start, nweeks)
    # Against `today`, not against the Sunday that ends today's week: a commit
    # dated tomorrow is as much in the future as one dated next month, and
    # author-local dates routinely run a day ahead across timezones.
    clamped = sum(1 for c in commits if c.date > today)
    days = set(c.date for c in commits)
    best, best_start, best_end, current, anchor = streaks(days, today)
    tags, evidence = mood(commits, weekly, nweeks, current, max(days), today)

    return (render_head(name, render_summary(commits, opts, nweeks, start,
                                             today, g), g)
            + [""]
            + render_tempo(weekly, start, clamped, ink, g) + [""]
            + render_clock(punch_card(commits), ink, g) + [""]
            + render_streaks(best, best_start, best_end, current, anchor,
                             max(days), clamped, ink, g) + [""]
            + render_mood(tags, evidence, ink, g))


def main(argv):
    # Without this, `git-mood | head -3` raises BrokenPipeError on exit.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        opts = parse_args(argv)
        plain = ascii_only(opts, GLYPHS)
        lines = build(opts, date.today(),
                      ASCII_GLYPHS if plain else GLYPHS,
                      Ink(color_enabled(opts)))
        emit("\n".join(lines) + "\n", opts.ascii_)
    except Usage as exc:
        write(sys.stderr, "%s: %s; try: %s --help\n" % (PROG, exc, PROG), True)
        return EXIT_USAGE
    except EnvProblem as exc:
        write(sys.stderr, "%s: %s\n" % (PROG, exc), True)
        return EXIT_ENV
    except KeyboardInterrupt:
        return EXIT_INTERRUPT
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
