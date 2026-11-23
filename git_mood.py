#!/usr/bin/env python3
"""git-mood - a terminal mood chart for a git repository.

One file, standard library only. One `git log` call in, four panels out.
Stat functions take commits and return numbers; render functions take those
numbers and return strings. Every threshold is decided in mood(), before a
single string is built.
"""

# signal/sys first and out of alphabetical order: a SIGINT can only be caught
# once they are in hand, and every import before the disposition is set is
# uncatchable. A measured Ctrl-C landing 10-20 ms in (inside the slow
# `unicodedata`/`subprocess` imports) printed a raw traceback and exited -2.
# The rest of the imports follow below the handler.
import signal
import sys

# CLI exit-code contract: 0 a chart or a clean "nothing to chart",
# 1 environment, 2 usage, 130 Ctrl-C. 130 holds only from when the handler
# below is installed to just before the process ends. Outside that window -
# interpreter startup, this file's compile, `import signal`, and interpreter
# finalization after the run (a Ctrl-C can then leave -2 with the whole chart
# already on stdout) - Python answers the signal itself and leaves -2.
# Measured, not assumed.
EXIT_ENV = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130


def interrupted(_signum, _frame):
    """Leave with 130 from wherever the signal landed."""
    sys.exit(EXIT_INTERRUPT)


# Installed only as the program: `import git_mood` must not replace an
# importer's own SIGINT disposition. Off the main thread signal.signal()
# raises ValueError, which no import should fail over.
#
# main() hands the signal back as its first act, because a Python handler left
# in place runs a Python frame at an arbitrary bytecode boundary, and one
# landing inside subprocess.Popen can leave `_waitpid_lock` held by the thread
# that then blocks on it - a wedge with no exit at all. This covers the import
# window only.
if __name__ == "__main__":
    try:
        signal.signal(signal.SIGINT, interrupted)
    except ValueError:
        pass

import math                                                   # noqa: E402
import os                                                     # noqa: E402
import subprocess                                             # noqa: E402
import unicodedata                                            # noqa: E402
from collections import namedtuple                            # noqa: E402
from datetime import date, timedelta                          # noqa: E402

PROG = "git-mood"
VERSION = "1.0"

HELP = """git-mood - a terminal mood chart for a git repository

usage: git-mood [path] [options]

  path              a git repository, or any directory inside one
                    (default: the current directory)
  --                end the options; everything after it is the path,
                    even if it begins with a dash

options:
  -w, --weeks N     how many weeks back to read (default: 26, max: 520)
  -a, --all         read the current branch's entire history; wins over --weeks
      --author STR  keep commits whose "Name <email>" contains STR
                    (case-insensitive substring, not a regex; STR may
                    span the brackets: --author "lace <ada")
      --ascii       draw with plain ASCII instead of block characters;
                    printable text above U+007F is escaped, not dropped
      --color       emit ANSI color even where it would be off by default,
                    so `git-mood --color | less -R` keeps its color
      --no-color    never emit ANSI color; wins if --color is given too
  -h, --help        show this and exit; wins over -V if both are given
  -V, --version     show the version and exit

notes:
  Times are each commit's own author clock; nothing is converted.
  "N authors" counts distinct lower-cased author emails; --author matches
  the composed "Name <email>", so the two can disagree.
  Built for 80 columns; COLUMNS is not read and the layout does not adapt.
  Color order and tag table: https://github.com/yinggarykairui/git-mood#readme
"""

# HELP is the one string that never goes through the --ascii ramp, so it must
# be ASCII on its own: a non-ASCII character printed as "?" on a stream that
# could not encode it, mid-sentence.
assert HELP.encode("ascii")

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
    # The ramp climbs low-ink to high so it is not drawn upside down, and must
    # stay exactly as long as GLYPHS["spark"]: ramp_glyph() picks a step by
    # index, so a different length has --ascii and the default disagree.
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
Options = namedtuple("Options", "path weeks whole author ascii_ color given")


class Usage(Exception):
    """Bad command line. Exit 2, with a pointer at --help.

    Three separate fields so only `echo` holds user text and only it needs a
    width budget: `message` says what is wrong, `echo` is the token the user
    typed, `advice` is the fix when one exists.

    `tip` is the "try: git-mood --help" tail; the one message that refuses to
    print the help the user just asked for turns it off.
    """

    def __init__(self, message, echo=None, advice=None, tip=True):
        Exception.__init__(self, message)
        self.echo = echo
        self.advice = advice
        self.tip = tip


class EnvProblem(Exception):
    """No git, no directory, no repo, or git itself failed. Exit 1."""


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

# tame() below marks Unicode general category Cf ("format") - codepoints that
# draw nothing but reorder the characters around them. They are not control
# characters, so the `ch < " "` test misses them, and one like U+202E in a
# repo name or --author value flips the visual order of the rest of the header
# line (the same hijack an embedded ESC performs). Cf is a strict superset of
# the hand-written bidi/BOM ranges it replaced.


# --ascii is a promise about every byte written. Escaping non-ASCII (rather
# than replacing with "?") keeps an error line's datum recoverable: the path a
# reader has to go fix is exactly the part that is not ASCII. tame() does it
# before oneline()/fit()/display_width() run, so an escape that costs six cells
# is budgeted as six and an 80-column line stays one; escaping after the fit
# would overhang.
ASCII_OUT = False


def ascii_escape(ch):
    point = ord(ch)
    return "\\u%04x" % point if point <= 0xFFFF else "\\U%08x" % point


def tame(text):
    """Control characters out (an embedded ESC in a repo name would otherwise
    recolor the caller's terminal from our header); they become "?".

    Under --ascii, everything else above U+007F is escaped rather than dropped,
    so a path or author name stays recoverable. The backslash is doubled so the
    escaped form is unambiguous - a directory literally named `\\u4e2d` and one
    named with the character do not print the same.
    """
    out = []
    for ch in str(text):
        if ch < " " or "\x7f" <= ch <= "\x9f" \
                or unicodedata.category(ch) == "Cf":
            out.append("?")
        elif not ASCII_OUT:
            out.append(ch)
        elif ch == "\\":
            out.append("\\\\")
        elif ch < "\x7f":
            out.append(ch)
        else:
            out.append(ascii_escape(ch))
    return "".join(out)


def whole_escapes(text):
    """`text` with a trailing half-written escape removed.

    Under --ascii the per-character trims cut the six chars of a `\\u4e2d`; a
    budget that runs out mid-token left a dangling `\\u4e2`, not a prefix of
    the name shown, off a value whose whole job is to be checkable.
    """
    if not ASCII_OUT:
        return text
    kept, i, end = 0, 0, len(text)
    while i < end:
        if text[i] != "\\":
            i += 1
        else:
            nxt = text[i + 1:i + 2]
            if nxt == "\\":
                i += 2
            elif nxt == "u":
                i += 6
            elif nxt == "U":
                i += 10
            else:
                # tame() writes a backslash only to begin one of the three
                # tokens above, so a backslash followed by anything else is
                # one of them with its tail already cut.
                break
        if i > end:
            break
        kept = i
    return text[:kept]


def marked(prefix):
    """`prefix` with the "..." cut marker on it, and never a fourth dot.

    A prefix already ending in "." would join as `name....`, which reads as a
    longer marker or as part of the name. Stripping the dots off the prefix
    (not the marker) keeps the result no longer than prefix + 3, so every
    budget written against that sum holds; an all-dots prefix stays inside the
    limit either way.
    """
    return prefix.rstrip(".") + "..."


def flatten(text):
    """Whitespace folded and control characters tamed, with no length budget:
    the display-safe form oneline() and fit() both start from.

    Separate from oneline() so fit() can flatten *without* oneline's marking
    step and apply its own cell-measured marker once - marking here at the
    character count left wide-char cuts like `fit("中"*30, 4)` -> `中..` a
    two-dot stub that looks marked and is not (#134 item 5).
    """
    # Fold on Python's full whitespace set MINUS the C0 separators \x1c-\x1f:
    # str.split() counts those as whitespace and drops them before tame() sees
    # them, so a lone \x1f came out empty instead of as "?". Keep the rest of
    # the set, or U+0085/U+00A0/U+2028/U+2029 reach stdout raw.
    seps = "\x1c\x1d\x1e\x1f"
    spaced = "".join(" " if ch.isspace() and ch not in seps else ch
                     for ch in str(text))
    return tame(" ".join(part for part in spaced.split(" ") if part))


def oneline(text, limit=60):
    """Errors are one line, so user data never breaks the format.

    Below four characters there is no room for the ellipsis, and adding one
    anyway returned a string longer than the limit; under that floor the text
    is simply cut.
    """
    flat = flatten(text)
    if len(flat) <= limit:
        return flat
    if limit < 4:
        return whole_escapes(flat[:max(limit, 0)])
    return marked(whole_escapes(flat[:limit - 3]))


# Below four cells fit() stops paying for its "..." and cuts silently, so a
# budget under this floor buys a *wrong* string, not a short one. fit() and
# render_summary (which sizes a budget down toward nothing) both read it.
MARKED_FLOOR = 4


def display_width(text):
    """Terminal cells, not codepoints: wide (W/F) characters count 2. A rule
    sized in codepoints drew short under any name holding wide characters."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text, cells):
    """oneline(), measured in cells, so the result really is that wide. The
    trim loop and its test both count cells (a char-counted loop never trimmed
    text of <=3 wide chars), and the "..." is dropped rather than paid for when
    the budget cannot hold both.

    flatten(), not oneline(): oneline() marks at the character count, so a wide
    first character under a four-cell budget left `中..` - a two-dot stub that
    looks marked and is not (#134 item 5). fit() flattens unmarked and applies
    its own cell-measured marker once, below.
    """
    flat = flatten(text)
    if display_width(flat) <= cells:
        return flat
    for tail in ("..." if cells >= MARKED_FLOOR else "", ""):
        room = cells - len(tail)
        cut = flat
        while cut and display_width(cut) > room:
            cut = cut[:-1]
        cut = whole_escapes(cut)
        if cut:
            return marked(cut) if tail else cut
    return ""


def env_line(prefix, tail, tail2=None, joiner=": "):
    """`prefix` + user text, cut so `git-mood: ` plus all of it fits 80 cells.

    Every environment message prints as `git-mood: <message>`, so the elastic
    tail's budget is 80 cells less PROG, ": " and the prefix. fit() and not
    oneline() because the budget is in cells: oneline()'s char limit let forty
    CJK chars render a 114-cell line. `tail2` is the one message with two
    elastic parts; it gets whatever the first one leaves.
    """
    room = 80 - len(PROG) - len(": ") - display_width(prefix)
    if tail2 is None:
        return prefix + fit(tail, max(room, 0))
    room -= display_width(joiner)
    head = fit(tail, max(room // 2, 0))
    return prefix + head + joiner + fit(tail2,
                                        max(room - display_width(head), 0))


# Everything a POSIX shell reads as more than text. Advice that pastes a value
# holding one is a line the reader cannot paste back: `--author=--a$(id)` runs
# `id`.
SHELL_META = frozenset("$`\"'\\;&|<>(){}[]*?~!#")


def flag_shaped(arg):
    """`--all` and `-a` are flags; `-3` and `-` are values."""
    return len(arg) > 1 and arg[0] == "-" and (arg[1] == "-" or arg[1].isalpha())


def take_value(flag, inline, argv, i, literal=""):
    """Return (value, next index) for both `--flag=v` and `--flag v`.

    `flag` is the token the user actually typed, so `-w` never reports itself
    as `--weeks`. A following flag is refused rather than eaten (swallowing it
    produced advice naming the flag it had just consumed).

    `literal` is what the flag would do with a flag-shaped value, if anything;
    only flags that can take one offer the `--flag=value` advice, since
    `--weeks=--all` is advice that fails on the next line.

    The advice quotes the value whole, but a value that will not fit or that
    needs quoting to survive a shell is named VALUE rather than pasted in: a
    cut value advises a different command, and "needs quoting" is SHELL_META
    (testing only spaces let `--author=--a$(id)` out as a pasteable line).
    """
    if inline is not None:
        return inline, i
    if i >= len(argv):
        raise Usage("%s needs a value" % flag)
    if flag_shaped(argv[i]):
        value = argv[i]
        advice = "use %s=%s to %s" % (flag, value, literal)
        quotable = any(ch.isspace() or ch in SHELL_META for ch in value)
        if not (tame(value) == value and not quotable
                and len(PROG) + 2 + display_width(advice) <= 80):
            # Still say which form works; only the value is withheld.
            advice = "use the %s=VALUE form to %s" % (flag, literal)
        # The shape was detected, not the identity: `-a b` and `-中中中` are
        # refused for beginning with `-`. The message says why it was not
        # taken; the echo line below shows the value.
        raise Usage("%s needs a value; that one begins with -" % flag,
                    echo=value, advice=advice if literal else None)
    return argv[i], i + 1


# int() accepts more than "an integer from 1 to 520" describes: `1_0`, Unicode
# digits, and surrounding whitespace all pass, so the CLI reads only the ASCII
# digits it documents.
DIGITS = frozenset("0123456789")


def integer_shaped(raw):
    body = raw[1:] if raw[:1] in ("+", "-") else raw
    return bool(body) and all(ch in DIGITS for ch in body)


def parse_weeks(raw, flag="--weeks"):
    """`flag` is the token the user typed, so `-w` is never reported as
    `--weeks`."""
    try:
        if not integer_shaped(raw):
            raise ValueError(raw)
        n = int(raw)
    except ValueError:
        raise Usage("%s needs an integer from 1 to %d" % (flag, MAX_WEEKS),
                    echo=raw)
    if not 1 <= n <= MAX_WEEKS:
        raise Usage("%s must be from 1 to %d" % (flag, MAX_WEEKS), echo=raw)
    return n


SHORT_FLAGS = "ahV"          # short options that take no value
SHORT_VALUED = "w"           # short options that take one

# Every long option the scan accepts, spelled as typed. Must match the scan:
# here but not in the scan is suggested and then rejected, and vice versa.
LONG_OPTIONS = ("--weeks", "--all", "--author", "--ascii", "--color",
                "--no-color", "--help", "--version")


def nearest_option(flag):
    """The long option `flag` is a near miss for, or None.

    Two stages. Prefix test first: a typed stem that is a non-empty prefix of
    exactly one option stem is that option (so `--w`, `--we`, `--co` are
    advised); an ambiguous prefix (`--a`: all, author, ascii) falls through,
    "report rather than guess" like a difflib tie below.

    Then difflib decides, at n=2 and cutoff=0.6. This function does not
    second-guess that ratio - it asks difflib for the same candidates' scores
    back, so there is one similarity rule, not two.

    The typed stem is case-folded first (--WEEKS scored 0.0 against `weeks`
    where --Weeks scored 0.8); the suggestion keeps its canonical case, read
    from LONG_OPTIONS. The leading `--` is dropped on both sides before
    scoring: it is shared by every pair, and leaving it in paid a ratio with
    nothing to do with the word (`--since` scored 0.625 against `--version`),
    offering a confident wrong answer in place of the `--help` tail.

    Two candidates tied at the top score get no answer: naming one of two
    equally close options is a guess.

    difflib is imported past the prefix test, not at module level: it pulls in
    `re` (~7 ms of a ~26 ms cold start) on a path almost no run takes, and
    every module-level import widens the window in which a Ctrl-C is answered
    by the interpreter, not this program.
    """
    stems = [option[2:] for option in LONG_OPTIONS]
    stem = flag[2:].lower()
    if stem:
        starts = [opt for opt, s in zip(LONG_OPTIONS, stems)
                  if s.startswith(stem)]
        if len(starts) == 1:
            return starts[0]
    import difflib
    near = difflib.get_close_matches(stem, stems, 2, 0.6)
    if not near:
        return None
    if len(near) == 2:
        # Candidate as seq1, typed as seq2, matching get_close_matches; the
        # other order is a different number.
        scorer = difflib.SequenceMatcher(b=stem)
        scorer.set_seq1(near[0])
        best = scorer.ratio()
        scorer.set_seq1(near[1])
        if scorer.ratio() == best:
            return None
    return LONG_OPTIONS[stems.index(near[0])]


def short_option_problem(arg):
    """(message, kwargs) for a `-...` token no branch in the scan claimed.

    Clustering is tested first: every char of `-wa` is a flag this program
    has, so it is named a cluster, not an attached value. `-w8` reaches the
    second test because `8` is no flag.
    """
    body = arg[1:]
    if arg[1] == "-" or "=" in arg:
        return "unknown option", {"echo": arg}
    if all(ch in SHORT_FLAGS + SHORT_VALUED for ch in body):
        # The advice is a command line, so it has to work: `-wa` split in
        # order is `-w -a`, where `-w` eats and refuses `-a`. Valued options
        # go last instead, each shown with the value it needs.
        rewrite = " ".join(["-" + ch for ch in body if ch in SHORT_FLAGS]
                           + ["-%s N" % ch for ch in body
                              if ch in SHORT_VALUED])
        advice = "write them separately: %s" % rewrite
        # Past the budget the enumeration is dropped whole, not cut: a cut
        # command line is not the one being advised (`-aaaa...` listed `-a`
        # thirty-eight times, 146 cells).
        if len(PROG) + 2 + display_width(advice) > 80:
            # The fallback still has to be runnable: "one dash each" alone is
            # not enough for a cluster holding -w, which needs its value.
            advice = ("write them separately, one dash each, each -%s with "
                      "its own N" % SHORT_VALUED[0]
                      if any(ch in SHORT_VALUED for ch in body)
                      else "write them separately, one dash each")
        return ("short options cannot be combined",
                {"echo": arg, "advice": advice})
    if body[0] in SHORT_VALUED:
        # `-w=8` is accepted; what `-w8` is missing is the = or the space,
        # which the two forms below show.
        return ("-%s does not take an attached value" % body[0],
                {"echo": arg,
                 "advice": "write it as -%s N or -%s=N" % (body[0], body[0])})
    return "unknown option", {"echo": arg}


def parse_args(argv):
    """Hand-rolled so --help is a verbatim string and every input is spec'd.

    Clustering (`-aw 4`) is deliberately not split; it reports as an unknown
    option rather than guessing.

    --help and --version are recorded and acted on after the scan, not on
    sight (which made `git-mood --help nonsense` exit 0 with the help text).
    Only a path argument then makes it a usage error: `--no-color --help` and
    `--ascii --help` are fine, since those flags say how to print and the help
    is printed. An unknown option still raises inside the scan, so `--help
    --nope` is exit 2.
    """
    path, weeks, whole, author, ascii_ = None, 26, False, None, False
    # Three states, not two: "auto" is the default and the only one that
    # consults the terminal; "off"/"on" are decisions the caller already made.
    color = "auto"
    i, only_paths = 0, False
    want, want_flag = None, None
    # Parsed and thrown away, keyed by option, valued by the first spelling:
    # a flag written twice is named once.
    discarded = {}
    while i < len(argv):
        arg = argv[i]
        i += 1
        flag, eq, inline = arg.partition("=")
        inline = inline if eq else None
        if only_paths:
            if path is not None:
                raise Usage("unexpected argument", echo=arg)
            path = arg
        elif arg == "--":
            only_paths = True          # everything after is a path, even `-x`
        elif arg in ("-h", "--help"):
            # --help wins over -V in either order, so this is not guarded.
            want, want_flag = HELP, arg
        elif arg in ("-V", "--version"):
            if want is None:
                want, want_flag = "%s %s\n" % (PROG, VERSION), arg
        elif arg in ("-a", "--all"):
            whole = True
            discarded.setdefault("all", arg)
        elif arg == "--ascii":
            ascii_ = True
        elif arg == "--no-color":
            # Not last-wins: of `--color --no-color`, only one answer can be
            # wrong in a way that costs the reader (ANSI into a file or pipe),
            # so refusing wins in either order - hence this is unguarded and
            # `--color` below is guarded.
            color = "off"
        elif arg == "--color":
            if color == "auto":
                color = "on"
        elif flag in ("-w", "--weeks"):
            raw, i = take_value(flag, inline, argv, i)
            weeks = parse_weeks(raw, flag)
            # `flag`, not `arg`: `--weeks=8` discards the option, not the 8.
            discarded.setdefault("weeks", flag)
        elif flag == "--author":
            author, i = take_value(flag, inline, argv, i,
                                   "search for it as text")
            discarded.setdefault("author", flag)
        elif arg.startswith("-") and arg != "-":
            # Clusters and attached values below are made of real flags, so
            # short_option_problem() names the rule they broke rather than
            # calling them unknown (the docstring's "not split" stands).
            message, extra = short_option_problem(arg)
            # nearest_option only for long options, and `flag` stops at the
            # "=" so `--wekes=4` scores as `--wekes` while the echo shows the
            # whole token. `near != flag` because `--ascii=1` scores 1.0
            # against --ascii: offering the token already typed is a no-op
            # that evicts the "; try: git-mood --help" tail.
            if arg.startswith("--"):
                near = nearest_option(flag)
                if near and near != flag:
                    # Advice drops main()'s "; try: git-mood --help" tail.
                    extra = dict(extra, advice="did you mean %s?" % near)
            raise Usage(message, **extra)
        elif path is None:
            path = arg
        else:
            raise Usage("unexpected argument", echo=arg)
    if want is not None:
        if path is not None:
            # No "try: --help" tail: it would advise asking for the help this
            # line is refusing to print. The path goes on the echo line.
            raise Usage("%s takes no path argument" % want_flag,
                        echo=path, tip=False)
        emit(want)
        if discarded:
            # The note goes to stderr (stdout stays byte-identical to a plain
            # --help, exit stays 0) and after emit(), so on an 80x24 terminal
            # it lands on the last row rather than scrolling off above the
            # help - the one case it exists for (`--help --weeks 8`). Named in
            # a canonical order, not argv order, so one intent prints one
            # diffable line.
            names = [discarded[key] for key in ("weeks", "all", "author")
                     if key in discarded]
            write(sys.stderr, "%s: %s ignores %s\n"
                  % (PROG, want_flag, ", ".join(names)), ascii_)
        raise SystemExit(0)
    # Not `path or "."`: that collapses "no argument" and "the empty string".
    # Only the first defaults to "."; the empty string is a bad path and gets
    # the exit-1 treatment (what a script passing an unset "$VAR" hits), rather
    # than silently charting the cwd. `given` carries the distinction.
    return Options("." if path is None else path, weeks, whole, author,
                   ascii_, color, path is not None)


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
        raise EnvProblem(env_line("could not run git: ", str(exc)))


def git_says(done):
    """git's own first line of complaint, or "" when it said nothing.

    Only the first line, since this program's errors are one line each. Raw,
    not tamed or trimmed: the caller wraps it in env_line() where flatten,
    tame and trim happen once against the real budget. Taming here too
    double-escaped under --ascii (`\u00e9` -> `\\u00e9`).
    """
    for line in done.stderr.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def check_directory(path, label=None):
    """Say which of the three ways a path can fail actually happened.

    `label` is how the path is named in the message; it differs from `path`
    only when no path argument was given, where quoting "." back at a reader
    who typed nothing would read as an echo of their own text.
    """
    if label is None:
        label = path
    if path == "":
        raise EnvProblem('no such directory: ""')
    try:
        os.stat(path)
    except FileNotFoundError:
        raise EnvProblem(env_line("no such directory: ", label))
    except NotADirectoryError:
        raise EnvProblem(env_line("not a directory: ", label))
    except PermissionError:
        raise EnvProblem(env_line("permission denied: ", label))
    except OSError as exc:
        raise EnvProblem(env_line("cannot read ", label,
                                  exc.strerror or str(exc)))
    if not os.path.isdir(path):
        raise EnvProblem(env_line("not a directory: ", label))
    if not os.access(path, os.R_OK | os.X_OK):
        raise EnvProblem(env_line("permission denied: ", label))


def git_path(done):
    """git's stdout as a path, or "" - never something exec() will reject.

    A NUL byte in git's answer, handed to the next subprocess call, surfaced
    as `ValueError: embedded null byte` and a traceback; a shimmed or wrapped
    git is a documented failure surface and owes a sentence, not a stack.
    """
    text = done.stdout.decode("utf-8", "replace").strip()
    if "\x00" in text:
        raise EnvProblem("git answered with a NUL byte in the path")
    return text


def resolve_repo(path, label=None):
    """Return (directory whose basename names the repo, is it the git dir).

    A bare repo has no work tree, so --show-toplevel fails there; fall back to
    the git directory itself rather than calling it "not a repository". The
    second element is True only in that fallback - the only case where a
    trailing `.git` is plumbing rather than part of a chosen directory name.
    """
    if label is None:
        label = path
    check_directory(path, label)
    done = run_git(["-C", path, "rev-parse", "--show-toplevel"])
    if done.returncode == 0:
        top = git_path(done)
        if top:
            return top, False
    done = run_git(["-C", path, "rev-parse", "--git-dir"])
    if done.returncode != 0:
        # git's own sentence carries the reason (a directory someone else
        # owns, a $GIT_DIR pointing at nothing), so it is passed through. Only
        # when git itself says "not a git repository" does this program say so
        # in its own voice and name the path.
        reason = git_says(done)
        if reason and "not a git repository" not in reason.lower():
            raise EnvProblem(env_line("git says: ", reason))
        if not reason:
            # git failing silently is not evidence about the directory (a
            # shim exiting 77 reported "not a git repository" against one that
            # is), so the exit code, the only fact in hand, is what prints.
            raise EnvProblem("git rev-parse exited %d and said nothing"
                             % done.returncode)
        raise EnvProblem(env_line("not a git repository: ", label))
    git_dir = git_path(done)
    top = os.path.abspath(os.path.join(path, git_dir))
    # `.../repo/.git` names the repo `repo`; `.../repo.git` names itself.
    if os.path.basename(top) == ".git":
        return os.path.dirname(top), False
    return top, True


def has_commits(top):
    """A fresh `git init` is not an error, so ask before running the log."""
    return run_git(["-C", top, "rev-parse", "--quiet", "--verify",
                    "HEAD"]).returncode == 0


def commits_elsewhere(top):
    """Is anything committed on some other ref while HEAD points nowhere?

    `git switch -c` before the first commit leaves HEAD unborn while main
    still holds history; calling that "no commits yet" contradicts `git log
    main`.
    """
    done = run_git(["-C", top, "rev-list", "-n", "1", "--all"])
    return done.returncode == 0 and bool(done.stdout.strip())


def read_commits(top):
    """The one `git log` call. Bytes in, (Commit list, undateable count) out.

    `-z` separates records with NUL, and git refuses a NUL in the author ident
    (fsck's nulInHeader), so a record boundary is the one thing an author name
    cannot counterfeit; the field separators inside a record still can, so the
    name is reassembled from the middle fields.

    No `--since` prefilter: it cuts on *committer* date, so an author-dated-
    in-window commit could vanish from a windowed run but show under --all.
    The author-date filter in build() is the only authority on membership.
    """
    args = ["-C", top, "log", "-z", "--pretty=format:%aI%x1f%aN%x1f%aE"]
    done = run_git(args)
    if done.returncode != 0:
        raise EnvProblem(env_line("git log failed: ",
                                  git_says(done) or "no reason given"))
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
    every record that could not be placed on a calendar. The strings are kept,
    not tallied, because there are two distinct causes: git's placeholder
    "%aI" (an author timestamp it could not read, e.g. a pre-1970 negative
    one) and a far-future timestamp that renders to a year no calendar covers.
    Either belongs to no week or hour; dropping it silently once turned a repo
    into "no commits yet".

    Because the record boundary is a NUL (see read_commits), field 0 is always
    git's own %aI and no author name can invent or erase a commit. A name
    holding \\x1f splits into extra fields; they rejoin by taking field 0 as
    the stamp and the last as the email, so at worst a name blurs into its
    email. A newline is a record boundary too: git uses it when `-z` is not
    honored and refuses it inside an author ident, so it is never part of a
    name.
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
    """One line per reason a commit could not be dated, naming the real one:
    a timestamp git itself could not read, versus one git read fine but no
    calendar covers (this program, not git, refuses it).
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


def undated_reason(undated):
    """Why there is nothing to chart, in the same terms as those notes, so
    stdout and the stderr note above do not name different causes.
    """
    unread = sum(1 for stamp in undated if stamp.strip() == "%aI")
    here = "%s here, " % count(len(undated), "commit")
    if not unread:
        return here + "none with a real calendar date"
    if unread == len(undated):
        return here + "none with a date git could read"
    return here + "none with a date this could use"


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


def nearer(new, old, today):
    """Is `new` the better date to report a tied streak at?

    Both are the authors' own commit dates and can run past today. The
    tie-break: a date that has happened beats one that has not; later beats
    earlier among dates that have; among all-future dates the earliest (least
    far past the window the header printed) wins.
    """
    if old is None:
        return True
    if (new > today) != (old > today):
        return old > today
    return new < old if new > today else new > old


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
        # On scattered single days every streak ties at one; a tie moves the
        # report forward (keeping the first put two panels years apart at the
        # same length). Not simply to the last, though - a plain `>=` handed
        # the title to a 2030 commit against a window ending today, so
        # nearer() prefers a date that has happened.
        if run > best or (run == best and nearer(day, best_end, today)):
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


def pct(value):
    """A whole percent for reading that never contradicts the chart.

    100 is reserved for "all of them" and 0 for "none of them" (rounding alone
    printed "100%" for 200 of 201 while the punch card showed the odd one
    lit); between them, the nearest whole percent. Tags fire on the exact
    measurement, never on this number, so nothing has to be nudged.
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


def mood(commits, weekly, nweeks, current, last_day, ahead, today):
    """Up to three tags, each with exactly one evidence line, and a count
    of the tags that fired past the cap.

    Every threshold is tested against the exact measurement and only then
    formatted for print, so no tag fires below the line it quotes and no
    evidence line contradicts its own rule.
    """
    total = len(commits)
    nonempty = [w for w in weekly if w > 0]
    mid = median(nonempty)
    peak = max(weekly) if weekly else 0
    ratio = peak / mid if mid else 0.0

    night = share(sum(1 for c in commits if c.hour < 6), total)
    weekend = share(sum(1 for c in commits if c.weekday >= 5), total)
    office = share(sum(1 for c in commits
                       if c.weekday < 5 and 9 <= c.hour < 18), total)
    evening = share(sum(1 for c in commits if 18 <= c.hour < 24), total)

    # What an evenly spread history already puts in each window - the number
    # a window tag's line must beat. Derived from the window's own size
    # through the same share()/pct() pair, so a printed baseline cannot drift
    # from the hours it counts.
    night_baseline = pct(share(6, 24))            # 6 of 24 hours -> 25.0
    evening_baseline = pct(share(6, 24))          # 6 of 24 hours -> 25.0
    weekend_baseline = pct(share(2, 7))           # 2 of 7 days   -> 28.6
    office_baseline = pct(share(9 * 5, 24 * 7))   # 45 of 168 h   -> 26.8
    covered = share(len(nonempty), nweeks)
    idle = (today - last_day).days

    # A distribution over too few commits is noise (at n=2 "nocturnal" fires
    # 44% of the time on random commits). The four hour/day window tags need a
    # minimum commit count, set where each one's false-positive rate under the
    # even-spread null stays below 1%. The binding pair are the two 50%-line
    # tags, nocturnal and after-hours (p=0.25): 21; weekend-coded (16) and
    # nine-to-five (12) reach 1% sooner and sit safely inside one gate.
    WINDOW_MIN = 21
    enough = total >= WINDOW_MIN

    # `last_day` is the newest non-future commit, so when `ahead` commits are
    # dated later this gap is measured from an older date than streaks prints;
    # the "ignoring N future dates" clause discloses that. Both numbers are
    # grouped, and the short form is the fallback for when the long one will
    # not fit (a repo idle for decades with a pile of future dates).
    idle_days = "%s days" % "{:,}".format(idle)
    quiet = "nothing committed in " + idle_days
    if ahead:
        quiet += ", ignoring %s" % count(ahead, "future date")
        if GUTTER + display_width(quiet) + len(" (line: 21 days)") > 80:
            quiet = ("idle %s, ignoring %s"
                     % (idle_days, count(ahead, "future date")))

    # Tested in this order, and at most three print, so order decides how a
    # repo that fires several is described. The two "is it being worked on"
    # tags - on a tear, dormant - go first, before any tag about which hours
    # the work keeps. The rest keep the order they had.
    candidates = [
        # These two carry "days" in their line, so at the top of the order
        # they sit above the percent lines below where a bare "(line: 5)"
        # would read as a percent.
        (current >= 5, "on a tear",
         "%d days in a row with at least one commit (line: 5 days)"
         % current),
        (idle >= 21, "dormant", "%s (line: 21 days)" % quiet),
        # Each of these four prints its baseline (else the tag reads as the
        # absence of a pattern) and carries the `enough` guard (below
        # WINDOW_MIN the share is noise). The windows are disjoint: nocturnal
        # and after-hours can only both clear 50% at an exact 50/50 split,
        # which the `evening > night` tie-break then gives to nocturnal alone,
        # and nine-to-five is ruled out against either by 60 + 50 > 100 - so
        # at most one of the three hour tags fires.
        (enough and night >= 50, "nocturnal",
         "%d%% of commits land between 00:00 and 05:59 "
         "(line: 50%%, baseline: %d%%)" % (pct(night), night_baseline)),
        (enough and evening >= 50 and evening > night, "after-hours",
         "%d%% of commits land between 18:00 and 23:59 "
         "(line: 50%%, baseline: %d%%)" % (pct(evening), evening_baseline)),
        (enough and weekend >= 57, "weekend-coded",
         "%d%% of commits land on a Saturday or Sunday "
         "(line: 57%%, baseline: %d%%)" % (pct(weekend), weekend_baseline)),
        (enough and office >= 60, "nine-to-five",
         "%d%% of commits land Mon-Fri, 09:00-17:59 "
         "(line: 60%%, baseline: %d%%)" % (pct(office), office_baseline)),
        # "busy week" (both rhythm tags' only denominator) is the median of
        # weeks that have commits, not of all weeks on the chart - whose
        # median is 0 on a mostly-empty repo and lands on the wrong side of
        # the "<2x" bound. Both lines name the weeks they measure.
        (len(nonempty) >= 4 and ratio >= 3.0, "burst-driven",
         "the busiest week holds %sx the median busy week (line: 3x)"
         % floor1(ratio)),
        # At its limit: 76 columns worst case, nothing elastic. "the median
        # busy week" names the same denominator burst-driven uses two rows up.
        (nweeks >= 4 and covered >= 60 and ratio < 2.0, "metronomic",
         "%d%% of weeks busy, peak %sx the median busy week "
         "(lines: 60%%, <2x)" % (pct(covered), floor1(ratio))),
    ]
    fired = [(tag, line) for ok, tag, line in candidates if ok]
    # `cut` is what the three-tag cap dropped, held to 0 or 1 by the
    # exclusions: tear/dormant is one pair, at most one hour tag fires,
    # weekend-coded can ride an hour tag but not nine-to-five (57 + 60 > 100),
    # and burst/metronomic is one pair - so at most four fire and the last in
    # this order (a rhythm tag) is the one dropped. max() keeps it honest if
    # the thresholds move.
    cut = max(0, len(fired) - 3)
    tags = [tag for tag, _ in fired[:3]]
    evidence = [line for _, line in fired[:3]]
    if not tags:
        tags.append("unremarkable")
        evidence.append("nothing in these numbers crosses a line")
    return tags, evidence, cut


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
    """First match wins; README.md prints this list in the same order: command
    line, then environment, then the terminal's own account, then stdout.
    """
    if opts.color != "auto":
        return opts.color == "on"
    # no-color.org: disable "when present and not an empty string", so the
    # truthiness test, not `is not None` - a shell exports `NO_COLOR=` for a
    # variable it was told to leave unset, and that must not suppress.
    if os.environ.get("NO_COLOR"):
        return False
    # The same emptiness rule in reverse. NO_COLOR is tested first because the
    # off switch wins. CLICOLOR_FORCE is the older name for FORCE_COLOR.
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    # Below the variables: TERM=dumb is the terminal describing itself, and a
    # caller who set FORCE_COLOR after reading that has overruled it.
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
    """Name, rule, counts line. The rule is 60 wide but never shorter than the
    line it underlines (which `--all` can push past 60). The name is cut to fit
    that 60, since a repo directory can be far longer.
    """
    # Exact match only: `git-mood-fork` and `Git-Mood` are other repos.
    if name == PROG:
        title = PROG
    else:
        # The separator's cost is cells, not characters; --ascii swaps it.
        # PROG is fixed ASCII, so len() is the same number there.
        room = 60 - len(PROG) - display_width(g["sep"])
        title = "%s%s%s" % (PROG, g["sep"], fit(name, room))
    lines = [title, g["rule"] * max(60, display_width(summary))]
    return lines + [summary] if summary else lines


def elastic(forms, room):
    """The first form that fits `room`, else the last one cut to fit.

    `forms` is in the caller's preference order, not width order: the first
    that fits wins, and every form states the same truth, trading only how
    much of it it says. The trailing fit() is a floor for when even the last
    form overflows (a count no repository reaches) - a cut string beats a
    renderer that raises.
    """
    for form in forms:
        if display_width(form) <= room:
            return form
    return fit(forms[-1], room)


def render_summary(commits, opts, nweeks, start, today, g):
    """One line; the rule under it is drawn to match, so width is the whole
    concern here.

    Only the filter text is elastic, so it is the one that gives way (a flat
    30-char cut was not enough: the rest of the line is 49 cells). With
    `--author`, 80 cells is an enforced bound - every elastic form is measured
    against it. Without it the line is a count of authors with nothing elastic,
    which only passes 80 at a scale no repository reaches, so it is described
    rather than guarded.
    """
    span = "%s %s %s" % (start.isoformat(), g["arrow"], today.isoformat())
    rest = [count(len(commits), "commit"), count(nweeks, "week"), span]
    if opts.author is None:
        # Nothing elastic: a count of authors is a number.
        who = count(len(set(c.email.lower() for c in commits)), "author")
    else:
        # "was the flag given", not "is the value truthy": --author= is a
        # filter to the empty string, not an absent filter. Each ladder below
        # is in preference order; elastic() takes the first rung that fits,
        # and every rung states the same true thing.
        room = max(0, 80 - display_width(g["sep"].join(rest))
                   - display_width(g["sep"]))
        frame = 'filtered to "%s"'
        empty = frame % ""
        if opts.author == "":
            # The empty string matches every ident, so the filter is applied
            # and stops nothing. The invariant every rung keeps (#134 item 1):
            # the field always owes the reader **a noun naming it as the author
            # filter** and **a sign the empty filter matches everything**. A
            # narrower budget spends the verbose frame and the literal empty
            # value, never the noun or the clarifier - which is why the noun-
            # carrying `author "" (all)` takes over when the frame is too wide.
            #
            # Five rungs, narrowing: full frame, frame + short clarifier, the
            # compact noun `author` with value + clarifier, the noun + clarifier
            # alone, then the bare noun as a floor. The floor is `author`, not a
            # trailing-space `""`. Every rung holds past any real repository.
            who = elastic((empty + " (matches all)", empty + " (all)",
                           'author "" (all)', "author (all)", "author"), room)
        else:
            # Rung one (always produced): the name cut to what the frame
            # leaves, floored at MARKED_FLOOR so fit() can afford its "...".
            # That floor must be measured against its own budget, or it put
            # the line at 81 (#71). Rung two hands the name the frame's twelve
            # cells, so a cell less of budget can show more of the name.
            forms = (frame % fit(opts.author,
                                 min(30, max(MARKED_FLOOR,
                                             room - display_width(empty)))),
                     '"%s"' % fit(opts.author, max(room - 2, MARKED_FLOOR)))
            who = elastic(forms, room)
    return g["sep"].join([rest[0], who] + rest[1:])


def bucket_columns(weekly, size):
    """[(first week index, weeks in the column, commits)], oldest first.

    A window that does not divide evenly leaves one short column, put at the
    *oldest* end so the newest bar is never a partial bucket read as a decline.
    """
    lead = len(weekly) % size
    edges = ([(0, lead)] if lead else []) + [(i, size) for i in
                                             range(lead, len(weekly), size)]
    return [(i, n, sum(weekly[i:i + n])) for i, n in edges]


def per_week(total, nweeks):
    """One decimal, always, so the number reads the same at both scales. But
    a repo that really has commits must never print 0.0, so the low end says
    "<0.1" - one decimal, and still not readable as none.
    """
    average = total / float(nweeks) if nweeks else 0.0
    if 0 < average < 0.05:
        return "<0.1"
    return "%.1f" % average


def render_tempo(weekly, start, clamped, today, ink, g):
    nweeks = len(weekly)
    size = int(math.ceil(nweeks / float(SPARK_COLS)))
    columns = bucket_columns(weekly, size)
    top = max(c[2] for c in columns)
    bar = "".join(spark_glyph(c[2], top, g) for c in columns)

    # Caption the peak *column* a reader can point at, but only when there is
    # one: on a flat repo every column ties and max() would pick the oldest,
    # reading as "something happened in February". The count is grouped, like
    # every other four-digit number on the page.
    peak = "{:,}".format(top)
    tied = [c for c in columns if c[2] == top]
    unit = "week" if size == 1 else "column"
    if len(tied) == 1:
        first, span = tied[0][0], tied[0][1]
        when = (start + timedelta(days=7 * first)).isoformat()
        where = "the week of %s" % when if span == 1 else \
                "the %s from %s" % (count(span, "week"), when)
        peak_line = "peak %s in %s" % (peak, where)
    elif len(tied) == len(columns):
        peak_line = "every %s holds %s" % (unit, peak)
    else:
        peak_line = "peak %s, tied across %s" % (peak, count(len(tied), unit))
    # The short column (at the oldest end) is named whenever it exists, or
    # "one column = 16 weeks" makes a 9-week bar read as a lull. Named without
    # "holds", which everywhere else counts commits, so it is not read as a
    # commit count; "leftmost (oldest)" also states which end is old. Worst
    # case 77 columns, at ten weeks a column and a five-digit rate.
    width = count(size, "week")
    if columns and columns[0][1] != size:
        width += ", the leftmost (oldest) %d" % columns[0][1]
    lines = [
        ink.dim(gutter("tempo")) + bar,
        ink.dim(INDENT + "one column = %s%s%s commits/week"
                % (width, g["sep"], per_week(sum(weekly), nweeks))),
        ink.dim(INDENT + peak_line),
    ]
    # The newest bar is drawn at full height from a span that has not
    # finished, so the caption says how much has elapsed - measured on the
    # column, not the week. The "rate divides by whole weeks" clause is the
    # one part that gives way when day counts push the line past 80.
    newest = columns[-1]
    first_day = start + timedelta(days=7 * newest[0])
    span_days, elapsed = 7 * newest[1], (today - first_day).days + 1
    if 0 < elapsed < span_days:
        # One "N of M days old" shape at both scales: the bare "6 days old"
        # week form was ambiguous (reads just as easily as "over already").
        aged = ("the newest %s is %d of %d days old"
                % (unit, elapsed, span_days))
        whole = aged + "; the rate divides by whole weeks"
        lines.append(ink.dim(INDENT + (whole if GUTTER + display_width(whole)
                                       <= 80 else aged)))
    if clamped:
        # Future-dated commits clamp into the newest bucket, rarely the peak
        # column, so this gets its own line naming the column that holds them.
        # "with a future date" (not "dated after today") so it does not echo
        # the streaks panel's wording; it matches the dormant evidence line.
        lines.append(ink.dim(INDENT + "%s with a future date, counted in the "
                             "newest column" % count(clamped, "commit")))
    return lines


def render_clock(grid, ink, g):
    top = max(max(row) for row in grid)
    lines = [ink.dim((gutter("clock") + "     " + RULER).rstrip())]
    for index, day in enumerate(DAYS):
        # Hours 00:00-05:59 carry the accent color. Only cells that hold
        # commits are tinted; tinting empty ones paints a cyan rectangle that
        # reads as a rendering artifact.
        cells = []
        for hour, value in enumerate(grid[index]):
            glyph = cell_glyph(value, top, g)
            cells.append(ink.accent(glyph) if hour < 6 and value else glyph)
        lines.append(ink.dim(INDENT + day + "  ") + "".join(cells))
    # The "teal = 00-05" clause prints only when something is actually tinted
    # (color emitted is necessary but not sufficient), so `tinted` tests the
    # same condition the loop tints on. The clause costs 15 cells and drops
    # whole rather than running the line - whose width grows with the count's
    # digits - past 80.
    key = (INDENT + "one cell per hour of the week" + g["sep"]
           + "darkest = %s" % count(top, "commit"))
    clause = g["sep"] + "teal = 00-05"
    tinted = any(row[hour] for row in grid for hour in range(6))
    if ink.enabled and tinted and display_width(key + clause) <= 80:
        key += clause
    lines.append(ink.dim(key))
    lines.append(ink.dim(INDENT + "author-local time, exactly as recorded "
                                  "in each commit"))
    return lines


def render_streaks(best, best_start, best_end, current, anchor, last_day,
                   clamped, today, ink, g):
    """Dates here are the commits' own, never clamped into the window; when
    one runs past today the panel discloses it, as tempo does for its column.
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
    # The test is whether a date actually printed on these two lines is after
    # today, not just whether the panel is clamped: a future commit outside
    # every streak shown here is tempo's disclosure to make, not this one's.
    shown = [d for d in (best_start, best_end,
                         anchor if current else last_day) if d is not None]
    if clamped and any(d > today for d in shown):
        # The date is spelled out, not called "it": a pronoun would attach to
        # "today" and say dates after today can run ahead of today.
        lines.append(ink.dim(INDENT + "%s dated after today; these dates can "
                             "pass %s" % (count(clamped, "commit"),
                                          today.isoformat())))
    return lines


def render_mood(tags, evidence, cut, ink, g):
    """The tag line, then one evidence line per printed tag - never more
    than three of either, whatever `cut` says was left off."""
    named = g["sep"].join(ink.bold(tag) for tag in tags)
    if cut:
        # A plain space, not the tag separator (which would read as a fourth
        # tag), and brackets so that under --no-color "(+1 more)" still parses
        # as a note about the list, not a member of it.
        named += " " + ink.dim("(+%d more)" % cut)
    lines = [ink.dim(gutter("mood")) + named]
    lines.extend(INDENT + line for line in evidence)
    return lines


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def write(stream, text, ascii_=False):
    """Encode-unrepresentable characters in a repo or author name are replaced,
    not raised; under --ascii, forced to plain ASCII. A closed stream
    (`git-mood >&-` leaves sys.stdout as None) is not a crash.

    `ascii_` is the --ascii flag and only that flag, on both streams alike:
    hard-coding True for stderr made the two streams disagree about the same
    string on a run that never asked for ASCII (the "you typed:" echo printed
    "?" for text stdout printed as typed).
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
    except UnicodeError:
        # The stream reported an encoding it will not actually write. An
        # error message is not worth a traceback, so it goes out in ASCII.
        try:
            stream.write(text.encode("ascii", "replace").decode("ascii"))
        except (ValueError, OSError, UnicodeError):
            return False
    except (ValueError, OSError):
        return False
    return True


def emit(text, ascii_=False):
    """Print to stdout, or say so on stderr if stdout is gone. Never raise."""
    if not write(sys.stdout, text, ascii_):
        raise EnvProblem("could not write to stdout")


def window_words(opts, nweeks):
    """Name the window the numbers describe, so a dead end says which one.

    --all reads one branch, not the repository, so it is named "the current
    branch's whole history" - "the whole history" sent readers hunting for a
    commit on another branch that was never counted.
    """
    if opts.whole:
        return "the current branch's whole history", ""
    return "the last %s" % count(nweeks, "week"), " try --all."


def build(opts, today, g, ink):
    top, is_git_dir = resolve_repo(
        opts.path, opts.path if opts.given else "the current directory")
    name = os.path.basename(top.rstrip(os.sep)) or top
    if is_git_dir and name.endswith(".git") and len(name) > len(".git"):
        # A bare repo lives in `name.git`; the repository is still `name`.
        # Only when is_git_dir, though: an ordinary work tree called
        # `notbare.git` keeps its name.
        name = name[:-len(".git")]

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
        # These belong to no week or hour, so they go to stderr, where they
        # survive `| head -1` and change no number on the chart.
        write(sys.stderr, note, opts.ascii_)
    if not commits:
        if undated:
            return page("", "%s %s nothing to chart."
                        % (undated_reason(undated), g["dash"]))
        return page("", nothing)

    start, nweeks = window_start(opts.weeks, opts.whole,
                                 [c.date for c in commits], today)
    commits = [c for c in commits if c.date >= start]
    where, advice = window_words(opts, nweeks)
    if not commits:
        return page(render_summary([], opts, nweeks, start, today, g),
                    "no commits in %s.%s" % (where, advice))
    if opts.author is not None:
        # Tested against the whole `Name <email>` ident git log prints, so
        # `--author "lace <ada"` can match across the boundary (--help says so
        # rather than promising a name-or-email match this does not perform).
        needle = opts.author.lower()
        commits = [c for c in commits
                   if needle in ("%s <%s>" % (c.name, c.email)).lower()]
        if not commits:
            # A needle that matched nobody is not purely a window problem, so
            # "try --all." alone pointed at the wrong knob. The needle is not
            # echoed again: the header above prints it, and two echoes cut to
            # different budgets read as two different searches.
            fix = " try --all, or a shorter --author." if advice \
                  else " try a shorter --author."
            return page(render_summary([], opts, nweeks, start, today, g),
                        "nothing matched in %s.%s" % (where, fix))

    weekly = weekly_counts(commits, start, nweeks)
    # Against `today`, not the Sunday ending today's week: a commit dated
    # tomorrow is as future as one next month, and author-local dates run a
    # day ahead across timezones.
    clamped = sum(1 for c in commits if c.date > today)
    days = set(c.date for c in commits)
    best, best_start, best_end, current, anchor = streaks(days, today)
    # tempo shows future-dated commits clamped into the newest column; the
    # rhythm tags must not, because clamping piles them into one week and
    # manufactures a peak (four next-month commits turned a `metronomic` repo
    # into `burst-driven`). So burst-driven/metronomic measure `weekly_real`,
    # with the future left out; tempo keeps the clamped `weekly`, so no commit
    # vanishes from the chart, only from the verdict about weeks that occurred.
    weekly_real = weekly_counts([c for c in commits if c.date <= today],
                                start, nweeks)
    # dormant measures the gap since the last commit, so mood() gets the
    # newest non-future date: a lone next-month commit would otherwise make
    # `idle` negative and silence the tag on a repo quiet for a year. streaks
    # below still gets the commits' own maximum and says those dates run ahead.
    tags, evidence, cut = mood(
        commits, weekly_real, nweeks, current,
        max([d for d in days if d <= today] or [max(days)]), clamped, today)

    return (render_head(name, render_summary(commits, opts, nweeks, start,
                                             today, g), g)
            + [""]
            + render_tempo(weekly, start, clamped, today, ink, g) + [""]
            + render_clock(punch_card(commits), ink, g) + [""]
            + render_streaks(best, best_start, best_end, current, anchor,
                             max(days), clamped, today, ink, g) + [""]
            + render_mood(tags, evidence, cut, ink, g))


def ascii_requested(argv):
    """Is --ascii on this command line, as a flag and not as a path?

    Read before parse_args(), since --ascii governs even parse_args()'s own
    stderr errors (reading it during the scan would honour it only when it came
    before the mistake). Stops at `--`, after which `--ascii` is a path.
    """
    for arg in argv:
        if arg == "--":
            return False
        if arg == "--ascii":
            return True
    return False


def restore_signal(sig, handler):
    """Hand a saved disposition back, tolerating the two values that cannot be.

    getsignal() returns None for a handler installed from C; signal() cannot
    take None, so there is nothing to restore. Off the main thread signal()
    raises ValueError - the same guard main() uses to install them.
    """
    if handler is None:
        return
    try:
        signal.signal(sig, handler)
    except (ValueError, OSError):
        pass


def main(argv):
    ascii_ = ascii_requested(argv)
    # Before parse_args(), whose own errors are output too.
    globals()["ASCII_OUT"] = ascii_
    # Hand SIGINT back to Python for the run itself (the module-level handler
    # covered startup only): default_int_handler raises KeyboardInterrupt,
    # which the guard below turns into 130. Not SIG_DFL, which would terminate
    # on the spot and leave -2 - and a Python handler firing inside
    # subprocess.Popen can wedge the process (see the handler comment up top).
    #
    # main() is a public entry point, so these are the caller's dispositions:
    # save them now and restore in the finally, or an import that runs the
    # program once leaves default_int_handler where the caller's SIGINT was and
    # SIG_DFL where Python's SIGPIPE-ignore was (which turns the caller's next
    # broken pipe into a killed process). getsignal() before signal(), so we
    # save the caller's and not the one we install.
    saved_sigint = signal.getsignal(signal.SIGINT)
    saved_sigpipe = (signal.getsignal(signal.SIGPIPE)
                     if hasattr(signal, "SIGPIPE") else None)
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except ValueError:
        pass
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
        # The generic "try: --help" tip steps aside for specific advice when
        # there is some. One decision, read twice: computed once here (and
        # nulled if too wide to print) so the tip and the advice line below
        # cannot both drop, leaving the reader no next step.
        advice = exc.advice if exc.advice and len(PROG) + 2 \
            + display_width(exc.advice) <= 80 else None
        tail = "; try: %s --help" % PROG if exc.tip and not advice else ""
        write(sys.stderr, "%s: %s%s\n" % (PROG, exc, tail), ascii_)
        if exc.echo is not None:
            # Quoted, because an empty value is a thing the user typed too:
            # unquoted, `--weeks=` showed "you typed:" and a trailing space.
            write(sys.stderr, '%s: you typed: "%s"\n'
                  % (PROG, fit(exc.echo, 55)), ascii_)
        if advice:
            write(sys.stderr, "%s: %s\n" % (PROG, advice), ascii_)
        return EXIT_USAGE
    except EnvProblem as exc:
        write(sys.stderr, "%s: %s\n" % (PROG, exc), ascii_)
        return EXIT_ENV
    except KeyboardInterrupt:
        return EXIT_INTERRUPT
    finally:
        # Put the caller's dispositions back, whichever way we leave - a
        # chart, a usage error, a Ctrl-C, or an exception on its way up.
        restore_signal(signal.SIGINT, saved_sigint)
        if hasattr(signal, "SIGPIPE"):
            restore_signal(signal.SIGPIPE, saved_sigpipe)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
