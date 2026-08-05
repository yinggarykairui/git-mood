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
  -a, --all         read the entire history; wins over --weeks
      --author STR  only commits whose author name or email contains STR
                    (case-insensitive substring, not a pattern)
      --ascii       draw with plain ASCII instead of block characters
      --no-color    never emit ANSI color (also honors NO_COLOR)
  -h, --help        show this and exit
  -V, --version     show the version and exit

Times are the author's own local clock, exactly as recorded in each commit.
Nothing is converted to your timezone.

The mood tags are nicknames for numbers, not psychology. Every tag prints
the number and the threshold that produced it, so you can disagree with it.
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
    "spark": ".:-=+*#%",
    "spark_zero": "_",
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

def oneline(text, limit=60):
    """Errors are one line, so user data never breaks the format."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def take_value(flag, inline, argv, i):
    """Return (value, next index) for both `--flag=v` and `--flag v`."""
    if inline is not None:
        return inline, i
    if i >= len(argv):
        raise Usage("%s needs a value" % flag)
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
    i = 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        flag, eq, inline = arg.partition("=")
        inline = inline if eq else None
        if arg in ("-h", "--help"):
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
            raw, i = take_value("--weeks", inline, argv, i)
            weeks = parse_weeks(raw)
        elif flag == "--author":
            author, i = take_value("--author", inline, argv, i)
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


def resolve_repo(path):
    if not os.path.isdir(path):
        raise EnvProblem("no such directory: %s" % oneline(path))
    done = run_git(["-C", path, "rev-parse", "--show-toplevel"])
    if done.returncode != 0:
        raise EnvProblem("not a git repository: %s" % oneline(path))
    top = done.stdout.decode("utf-8", "replace").strip()
    return top or os.path.abspath(path)


def has_commits(top):
    """A fresh `git init` is not an error, so ask before running the log."""
    return run_git(["-C", top, "rev-parse", "--quiet", "--verify",
                    "HEAD"]).returncode == 0


def read_commits(top):
    """The one `git log` call. Bytes in, Commit list out.

    No `--since` prefilter: it cuts on *committer* date, so a rebased or
    grafted commit whose author date is inside the window would vanish from a
    windowed run but show up under --all. The author-date filter in build() is
    the only authority on membership, so every panel counts the same commits.
    """
    args = ["-C", top, "log", "--pretty=format:%aI%x1f%aN%x1f%aE%x1e"]
    done = run_git(args)
    if done.returncode != 0:
        raise EnvProblem("git log failed: %s"
                         % oneline(done.stderr.decode("utf-8", "replace")))
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
    """Split the log into commits without losing any to odd author names.

    An author name may itself contain \\x1f or \\x1e. A record is only complete
    once it holds a parseable timestamp and at least two field separators; a
    short fragment is rejoined with the next one (restoring the \\x1e the split
    ate) instead of being dropped along with its neighbour. Extra \\x1f inside a
    name is kept by taking the first field as the stamp and the last as the
    email.
    """
    commits, held = [], None
    for chunk in text.split("\x1e"):
        fresh = chunk.lstrip("\r\n")
        if held is None or complete(fresh):
            # A held fragment that the next chunk cannot complete was real
            # garbage: drop the fragment, never the record standing behind it.
            record = fresh
        else:
            record = held + "\x1e" + chunk
        held = None
        if not record:
            continue
        if not complete(record):
            held = record
            continue
        held = None
        fields = record.split("\x1f")
        when = stamp_parts(fields[0])
        name, email = "\x1f".join(fields[1:-1]), fields[-1]
        commits.append(Commit(when[0], when[1], when[0].weekday(), name, email))
    return commits


def complete(record):
    """A record git can no longer be adding to: stamp plus both separators."""
    return (record.count("\x1f") >= 2
            and stamp_parts(record.split("\x1f")[0]) is not None)


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
         % int(night)),
        (weekend >= 25, "weekend-coded",
         "%d%% of commits land on a Saturday or Sunday (line: 25%%)"
         % int(weekend)),
        (office >= 60, "nine-to-five",
         "%d%% of commits land Mon-Fri, 09:00-17:59 (line: 60%%)" % int(office)),
        (len(nonempty) >= 4 and ratio >= 3.0, "burst-driven",
         "the busiest week holds %sx the median week (line: 3x)"
         % floor1(ratio)),
        (nweeks >= 4 and covered >= 60 and ratio < 2.0, "metronomic",
         "%d of %d weeks have a commit, top week %sx the median "
         "(line: 60%% and 2x)" % (len(nonempty), nweeks, floor1(ratio))),
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
    """0 falls to the bottom of the ramp; the rest quantize against the max."""
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


def render_head(name, g):
    return ["%s  %s" % (PROG, name), g["rule"] * 60]


def render_summary(commits, opts, nweeks, start, today, g):
    if opts.author:
        who = 'filtered to "%s"' % oneline(opts.author, 30)
    else:
        who = count(len(set(c.email.lower() for c in commits)), "author")
    span = "%s %s %s" % (start.isoformat(), g["arrow"], today.isoformat())
    return g["sep"].join([count(len(commits), "commit"), who,
                          count(nweeks, "week"), span])


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
    """Enough precision that a real commit never prints as 0.0/week."""
    average = total / float(nweeks)
    return "%.1f" % average if average >= 0.1 else "%.2g" % average


def render_tempo(weekly, start, clamped, ink, g):
    nweeks = len(weekly)
    size = int(math.ceil(nweeks / float(SPARK_COLS)))
    columns = bucket_columns(weekly, size)
    top = max(c[2] for c in columns)
    bar = "".join(spark_glyph(c[2], top, g) for c in columns)

    # Caption the peak *column*, the bar a reader can actually point at.
    first, span, peak = max(columns, key=lambda c: c[2])
    when = (start + timedelta(days=7 * first)).isoformat()
    where = "the week of %s" % when if span == 1 else \
            "the %s from %s" % (count(span, "week"), when)
    caption = "peak %d in %s" % (peak, where)
    if clamped:
        caption += "%sincludes %s dated later" % (g["sep"],
                                                  count(clamped, "commit"))
    return [
        ink.dim(gutter("tempo")) + bar,
        ink.dim(INDENT + "one column = %s%s%s commits/week"
                % (count(size, "week"), g["sep"],
                   per_week(sum(weekly), nweeks))),
        ink.dim(INDENT + caption),
    ]


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


def render_streaks(best, best_start, best_end, current, anchor, last_day, ink, g):
    longest = "longest %s, %s %s %s" % (count(best, "day"),
                                        best_start.isoformat(), g["arrow"],
                                        best_end.isoformat())
    if current:
        now = "current %s, through %s" % (count(current, "day"),
                                          anchor.isoformat())
    else:
        now = "current none, last commit %s" % last_day.isoformat()
    return [ink.dim(gutter("streaks")) + longest, INDENT + now]


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


def build(opts, today, g, ink):
    top = resolve_repo(opts.path)
    name = os.path.basename(top.rstrip(os.sep)) or top
    head = render_head(name, g)

    if not has_commits(top):
        return head + ["", "no commits yet %s nothing to chart." % g["dash"]]

    commits = read_commits(top)
    if not commits:
        return head + ["", "no commits yet %s nothing to chart." % g["dash"]]

    start, nweeks = window_start(opts.weeks, opts.whole,
                                 [c.date for c in commits], today)
    commits = [c for c in commits if c.date >= start]
    if not commits:
        return head + ["", "no commits in the last %s. try --all."
                       % count(nweeks, "week")]
    if opts.author:
        needle = opts.author.lower()
        commits = [c for c in commits
                   if needle in ("%s <%s>" % (c.name, c.email)).lower()]
        if not commits:
            return head + ["", 'no commits by "%s" in this window. try --all.'
                           % oneline(opts.author, 30)]

    weekly = weekly_counts(commits, start, nweeks)
    last_week = start + timedelta(days=7 * nweeks - 1)
    clamped = sum(1 for c in commits if c.date > last_week)
    days = set(c.date for c in commits)
    best, best_start, best_end, current, anchor = streaks(days, today)
    tags, evidence = mood(commits, weekly, nweeks, current, max(days), today)

    return (head
            + [render_summary(commits, opts, nweeks, start, today, g), ""]
            + render_tempo(weekly, start, clamped, ink, g) + [""]
            + render_clock(punch_card(commits), ink, g) + [""]
            + render_streaks(best, best_start, best_end, current, anchor,
                             max(days), ink, g) + [""]
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
