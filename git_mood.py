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
      --no-color    never emit ANSI color; a non-empty NO_COLOR, TERM=dumb
                    and a non-tty stdout do the same
  -h, --help        show this and exit; wins over -V if both are given
  -V, --version     show the version and exit

Times are the author's own local clock, exactly as recorded in each commit.
Nothing is converted to your timezone. When color is on, the punch card
tints any commits it holds in the 00:00-05:59 hours; the caption says so
when there are any to tint.

"N authors" counts distinct author email addresses, lower-cased. --author
matches the composed "Name <email>" instead, so the two can disagree.

The layout is built for 80 columns; COLUMNS is not read and the layout
does not adapt.

The mood tags are nicknames for numbers, not psychology. Every threshold
tag it prints carries the measured number and the line it crossed, so you
can disagree with it. The three tags about a window of the week also
print the share an evenly spread history would put in that window,
because a line below that share would fire on no pattern at all.
"unremarkable" is the one tag with no arithmetic to show, because
nothing crossed anything. Tags are tested in a fixed order (on a tear,
dormant, nocturnal, weekend-coded, nine-to-five, burst-driven,
metronomic) and at most three print; when more fired than that, the mood
line ends with "+N more" for the count it cut. The cut tags are not
named - a tag is only worth reading with its arithmetic under it, and
there is room for three of those.
"""

# HELP is the one string that never goes through the --ascii ramp, so it
# has to be ASCII on its own: an em dash in it printed as "?" on a stream
# that could not encode one, mid-sentence, in the text explaining the tags.
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
    # The ramp climbs like the Unicode one: a dot for a week with nothing in
    # it (the same low-ink mark the punch card uses for an empty cell), then
    # a baseline stroke for the shortest bar. The old set had these two the
    # other way round, so an --ascii sparkline drew dead weeks as solid bars
    # and busy weeks as gaps - the picture upside down.
    #
    # Above the first step the order is convention - the ramp people already
    # read in ASCII art - and not measured ink. This comment used to claim
    # that "%" carries less ink than "#" "in every monospace face", which is
    # a promise about every font in the world that nothing here checked. A
    # survey of rendered coverage across eleven monospace faces says the
    # opposite of monotone: ":" comes out heavier than "-" and "=" heavier
    # than "+" in all eleven, "+" heavier than "*" in seven, and the last
    # step, "%" to "#", runs backwards in Liberation Mono. Ten of eleven
    # faces do put "#" on top, which is why it is the top step, but that is
    # a majority and not a law.
    #
    # No number on screen depends on any of it. ramp_glyph() picks the step
    # from the value and the panel maximum, and this string is exactly as
    # long as the Unicode one, so --ascii and the default choose the same
    # index for the same week; only the shape drawn there differs.
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

    Three parts on three lines, because only one of them is elastic: the
    message says what is wrong and never holds user text, `echo` is the
    token the user typed, and `advice` is the fix when one exists. Folding
    the token into the message meant every message had its own budget for
    it, and a 40-character argument pushed the one line to 155 cells.

    `tip` is the "try: git-mood --help" tail. One message turns it off: the
    one that refuses to print the help the user just asked for.
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

# Codepoints that draw nothing but change how the characters around them
# are laid out. They are not control characters by category, so the
# `ch < " "` test below never saw them, and a repo name or an --author value
# holding U+202E flipped the visual order of the rest of the header line -
# the same hijack an embedded ESC performs, with no escape byte in sight.
#
# The set is Unicode's own: general category Cf, "format". Hand-written
# ranges (ZWSP..RLM, LRE..RLO, LRI..PDI, the BOM) covered the block a
# reviewer happened to think of and left the rest of the same class through
# - U+061C ARABIC LETTER MARK is a Bidi_Control and was not marked, and
# neither were U+00AD, U+180E, U+206A..U+206F or the interlinear annotation
# marks U+FFF9..U+FFFB. Cf is exactly the class the comment above describes,
# and it is a strict superset of the ranges it replaces, so nothing that
# used to be marked stopped being marked.


# --ascii is a promise about the bytes this program writes, and write()
# used to keep it with encode("ascii", "replace") - one "?" per codepoint,
# applied after every width was measured. On the chart that is only ugly;
# on an error line it destroys the datum the line exists to carry, because
# the path the reader has to go fix is exactly the part that is not ASCII:
#     git-mood --ascii /tmp/<four CJK chars>  ->  not a git repository: /tmp/????
# Escaping instead of replacing keeps the path recoverable, and doing it
# here rather than in write() keeps it measurable: tame() runs before
# oneline(), fit() and display_width(), so an escape that costs six cells
# is budgeted as six cells and an 80-column line stays an 80-column line.
# Escaping after the fit is what would overhang.
ASCII_OUT = False


def ascii_escape(ch):
    point = ord(ch)
    return "\\u%04x" % point if point <= 0xFFFF else "\\U%08x" % point


def tame(text):
    """Control characters out. A repo directory named with an embedded ESC
    would otherwise recolor the caller's terminal from our own header.

    Under --ascii, everything else above U+007F is escaped rather than
    dropped: a control character carries no datum worth keeping and stays a
    "?", while a path or an author name does. The backslash is doubled so
    the escaped form is unambiguous - a directory literally named `\\u4e2d`
    and one named with the character do not print the same.
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

    The trims below cut one character at a time, and under --ascii the
    characters being cut are the six of a `\\u4e2d`. A budget that runs out
    mid-token printed `"\\u4e2d\\u4e2`, which is not a prefix of the name
    the reader is being shown: it is a backslash-u meaning nothing, dangling
    off the end of a value whose whole job is to be checkable.
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
                # tame() writes a backslash only as the first character of
                # one of the three tokens above, so a backslash followed by
                # anything else is one of them with its tail already cut.
                break
        if i > end:
            break
        kept = i
    return text[:kept]


def oneline(text, limit=60):
    """Errors are one line, so user data never breaks the format.

    Below four characters there is no room for the ellipsis, and adding one
    anyway returned a string longer than the limit asked for - oneline("abcdef",
    2) came back as "abcde...". Under that floor the text is simply cut.
    """
    # str.split() with no argument folds on Unicode whitespace, which counts
    # the C0 separators \x1c-\x1f as spaces and drops them before tame() ever
    # sees them: an author string of one \x1f came out empty rather than as
    # the "?" every other control character gets. So the fold is Python's own
    # whitespace set minus those four. Narrowing it to six ASCII characters
    # instead fixed the separators and let everything else str.split() used
    # to catch - U+0085, U+00A0, U+2028, U+2029 - reach stdout raw.
    seps = "\x1c\x1d\x1e\x1f"
    spaced = "".join(" " if ch.isspace() and ch not in seps else ch
                     for ch in str(text))
    flat = tame(" ".join(part for part in spaced.split(" ") if part))
    if len(flat) <= limit:
        return flat
    if limit < 4:
        return whole_escapes(flat[:max(limit, 0)])
    return whole_escapes(flat[:limit - 3]) + "..."


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
    for tail in ("..." if cells >= 4 else "", ""):
        room = cells - len(tail)
        cut = flat
        while cut and display_width(cut) > room:
            cut = cut[:-1]
        cut = whole_escapes(cut)
        if cut:
            return cut + tail
    return ""


def env_line(prefix, tail, tail2=None, joiner=": "):
    """`prefix` + user text, cut so `git-mood: ` plus all of it fits 80 cells.

    Every environment message is printed as `git-mood: <message>`, so the
    budget for the message is 80 cells less that prefix, and the budget for
    the elastic tail is whatever the fixed words leave of it.

    It has to be fit() and not oneline(): oneline()'s limit counts
    characters, and forty CJK characters are forty characters and eighty
    cells, so a path of them rendered a 114-cell line under a rule that
    believed it had already trimmed. `tail2` is for the one message with two
    elastic parts; it gets whatever the first one leaves.
    """
    room = 80 - len(PROG) - len(": ") - display_width(prefix)
    if tail2 is None:
        return prefix + fit(tail, max(room, 0))
    room -= display_width(joiner)
    head = fit(tail, max(room // 2, 0))
    return prefix + head + joiner + fit(tail2,
                                        max(room - display_width(head), 0))


# Everything a POSIX shell reads as more than text. Advice that pastes a
# value holding one of these is a command line the reader cannot paste back:
# `--author=--a$(id)` looks like a search and runs `id`.
SHELL_META = frozenset("$`\"'\\;&|<>(){}[]*?~!#")


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

    The advice quotes the value whole or falls back to the generic form. Cut
    to fit, it printed a command line that is not the one it was advising -
    `--author=--xxx...` searches for a literal ellipsis - so a value that
    will not fit, or that needs quoting to survive a shell, is named as
    VALUE rather than pasted in.

    "Needs quoting" is SHELL_META, not whitespace. Testing only for spaces
    let `--author=--a$(id)` out as a copy-pasteable line, and pasting it
    runs `id` - the exact hazard the quote-it-whole rule was written for.
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
        # What was detected is the shape, not the identity: `-a b` and
        # `-中中中` are refused for beginning with `-`, and calling either
        # of them "a flag" told the user their own value was something it
        # is not. The echo line below shows it; this line says why it was
        # not taken.
        raise Usage("%s needs a value; that one begins with -" % flag,
                    echo=value, advice=advice if literal else None)
    return argv[i], i + 1


# int() accepts more than "an integer from 1 to 520" describes: `1_0` is
# ten, an Arabic-Indic four is four, a fullwidth two-six is twenty-six, and
# surrounding whitespace is skipped - so command lines nobody would call
# the same charted the same window, while the plainly-numeric `3.0` was
# refused. The CLI's contract is the digits it prints, so that is what it
# reads.
DIGITS = frozenset("0123456789")


def integer_shaped(raw):
    body = raw[1:] if raw[:1] in ("+", "-") else raw
    return bool(body) and all(ch in DIGITS for ch in body)


def parse_weeks(raw, flag="--weeks"):
    """`flag` is the token the user typed. take_value() already refuses to
    report `-w` as `--weeks`, and these two messages were the place the
    same flag still named itself two different ways."""
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


def short_option_problem(arg):
    """(message, kwargs) for a `-...` token no branch in the scan claimed.

    Clustering is tested first: every character of `-wa` is a flag this
    program has, and calling that an attached value because it starts with
    the one short option that takes one answers a question nobody asked.
    `-w8` reaches the second test because `8` is no flag.
    """
    body = arg[1:]
    if arg[1] == "-" or "=" in arg:
        return "unknown option", {"echo": arg}
    if all(ch in SHORT_FLAGS + SHORT_VALUED for ch in body):
        # The advice is a command line, so it has to be one that works.
        # `-wa` split left to right is `-w -a`, and `-w` then eats the flag
        # and refuses it - the "advice that does not work is worse than
        # none" the take_value() docstring rules out. The valued options go
        # last instead, each shown with the value it needs.
        rewrite = " ".join(["-" + ch for ch in body if ch in SHORT_FLAGS]
                           + ["-%s N" % ch for ch in body
                              if ch in SHORT_VALUED])
        advice = "write them separately: %s" % rewrite
        # The enumeration stops earning its place long before it overhangs:
        # `-aaaa...` printed a 146-cell line listing `-a` thirty-eight
        # times. Cutting it would print a command line that is not the one
        # being advised, so past the budget it is dropped whole.
        if len(PROG) + 2 + display_width(advice) > 80:
            # The fallback is held to the same test as the form it
            # replaces: "one dash each" alone is not a runnable
            # instruction for a cluster holding -w, which still needs its
            # value.
            advice = ("write them separately, one dash each, each -%s with "
                      "its own N" % SHORT_VALUED[0]
                      if any(ch in SHORT_VALUED for ch in body)
                      else "write them separately, one dash each")
        return ("short options cannot be combined",
                {"echo": arg, "advice": advice})
    if body[0] in SHORT_VALUED:
        # Not "takes its value as a separate argument": `-w=8` is accepted
        # and charts eight weeks. What `-w8` is missing is the = or the
        # space, which is what the two forms below show.
        return ("-%s does not take an attached value" % body[0],
                {"echo": arg,
                 "advice": "write it as -%s N or -%s=N" % (body[0], body[0])})
    return "unknown option", {"echo": arg}


def parse_args(argv):
    """Hand-rolled so --help is a verbatim string and every input is spec'd.

    Clustering (`-aw 4`) is deliberately not split; it reports as an unknown
    option rather than guessing.

    --help and --version are recorded and acted on after the scan, not in the
    middle of it. Printing on sight made `git-mood --help nonsense` exit 0
    with the help text, which reads as "that command line was fine".

    Only a path argument makes that a usage error, though. Refusing every
    other token counted `--no-color --help` and `--ascii --help` as mistakes,
    which is nobody's mistake: those flags say how to print, and the help is
    printed. An unknown option still raises inside the scan, so `--help
    --nope` is exit 2 the way it always was.
    """
    path, weeks, whole, author, ascii_, color = None, 26, False, None, False, True
    i, only_paths = 0, False
    want, want_flag = None, None
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
            # --help wins when both are given: it is the larger answer, and
            # it names --version anyway. It wins whichever order they come
            # in, so this assignment is not guarded.
            want, want_flag = HELP, arg
        elif arg in ("-V", "--version"):
            if want is None:
                want, want_flag = "%s %s\n" % (PROG, VERSION), arg
        elif arg in ("-a", "--all"):
            whole = True
        elif arg == "--ascii":
            ascii_ = True
        elif arg == "--no-color":
            color = False
        elif flag in ("-w", "--weeks"):
            raw, i = take_value(flag, inline, argv, i)
            weeks = parse_weeks(raw, flag)
        elif flag == "--author":
            author, i = take_value(flag, inline, argv, i,
                                   "search for it as text")
        elif arg.startswith("-") and arg != "-":
            # "unknown option" sent the reader hunting for a flag named
            # `aw`. Both shapes below are made of flags --help does list,
            # so the answer is how to write them, not that they do not
            # exist. The docstring's "deliberately not split" stands: this
            # names the rule instead of guessing at the intent.
            message, extra = short_option_problem(arg)
            raise Usage(message, **extra)
        elif path is None:
            path = arg
        else:
            raise Usage("unexpected argument", echo=arg)
    if want is not None:
        if path is not None:
            # No "try: --help" tail here: it would advise the user to ask for
            # the help this very line is refusing to print. The path they
            # typed goes on the echo line instead, like every other usage
            # error.
            raise Usage("%s takes no path argument" % want_flag,
                        echo=path, tip=False)
        emit(want)
        raise SystemExit(0)
    # `path or "."` collapsed two different command lines into one: no
    # argument at all, and an argument that is the empty string. The first
    # should not be quoted back at the reader as a path they never typed;
    # the second is a bad path like any other and gets the exit-1 treatment
    # every other bad path gets, rather than silently charting the cwd -
    # which is what a script passing an unset "$VAR" hits.
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

    Only the first line: the rest of a git error is usually a worked example
    indented under it, and this program's errors are one line each.

    Raw. It used to trim to 120 characters, which is both too long for the
    line it lands on and measured in the wrong unit; the caller wraps it in
    env_line() with the prefix it is about to print, so the flatten, the
    tame and the trim all happen once, there, against the real budget.

    Taming it here as well was harmless while a second pass only replaced
    "?" with "?". It stopped being harmless when --ascii began escaping:
    the first pass wrote `\u00e9` and the second escaped its backslash, so
    git's own sentence reached the reader as `\\u00e9` - the escape that
    means a literal six-character name.
    """
    for line in done.stderr.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def check_directory(path, label=None):
    """Say which of the three ways a path can fail actually happened.

    `label` is how the path is named in the message. It differs from `path`
    in exactly one case: no path argument was given, and quoting "." back at
    a reader who typed nothing reads as an echo of their own text when every
    other message on this stream is one.
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

    A `git` on PATH that answers `rev-parse --show-toplevel` with a NUL byte
    in it got that byte handed straight back to the next subprocess call,
    where it surfaced as `ValueError: embedded null byte` and a traceback.
    --help names `git` on PATH as a requirement, so a shimmed or wrapped git
    is a documented failure surface and owes a sentence, not a stack.
    """
    text = done.stdout.decode("utf-8", "replace").strip()
    if "\x00" in text:
        raise EnvProblem("git answered with a NUL byte in the path")
    return text


def resolve_repo(path, label=None):
    """Return (directory whose basename names the repo, is it the git dir).

    A bare repo has no work tree, so --show-toplevel fails there; fall back to
    the git directory itself rather than calling it "not a repository". The
    second element is True only in that fallback, because it is the only case
    where a trailing `.git` is part of the repository's plumbing rather than
    part of a directory name the user chose.
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
        # git's own sentence carries the reason - a directory somebody else
        # owns, a $GIT_DIR pointing at nothing - so it is passed through
        # instead of being relabelled. When git's own verdict already is
        # "not a git repository" there is nothing to add, so this program
        # says that in its own voice and names the path it was handed.
        reason = git_says(done)
        if reason and "not a git repository" not in reason.lower():
            raise EnvProblem(env_line("git says: ", reason))
        if not reason:
            # git failing without a word is not evidence about the
            # directory. A `git` shim exiting 77 in silence was reported as
            # `not a git repository` against a directory that is one, which
            # sends the reader to fix the wrong thing; the exit code is the
            # only fact in hand, so it is the one printed.
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


def undated_reason(undated):
    """Why there is nothing to chart, in the same terms as those notes.

    One sentence used to cover both causes: "none with a real calendar date"
    was printed even when every one of them had been skipped because git
    itself could not read the timestamp - the stderr note two lines above
    naming one cause while stdout named the other.
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

    Both are commit dates, which are the authors' own and can run past
    today; the panel says so when they do. Between two ties, the one
    nearest what the rest of the page is talking about wins: a date that
    has happened beats one that has not, later beats earlier among dates
    that have, and among dates that have all not happened yet the earliest
    is the least far past the window the header printed. Preferring the
    later one there sent a repo whose only two commits are dated next week
    and next month to the month, which is the disagreement this function
    exists to shrink.
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
        # On a repo of scattered single days every streak ties at one, and
        # keeping the first put `longest 1 day, 2007-04-14` directly above
        # `current 1 day, through <today>` - two panels reporting the same
        # length at dates years apart, which reads as a data error rather
        # than as the tie it is. So a tie moves the report forward.
        #
        # Not simply to the last one, though: a plain `>=` handed the title
        # to a commit dated 2030 on a repo whose header two panels up says
        # the window ends today, which is a worse disagreement than the one
        # it was fixing. nearer() prefers a date that has happened.
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

    Rounding alone printed "100%" for 200 of 201 commits while the punch card
    three lines above showed the odd one lit, so the two ends are held back:
    100 is reserved for "all of them" and 0 for "none of them". Between those,
    the nearest whole percent. A tag fires on the exact measurement and never
    on this number, and for every threshold in this program the rounded value
    still lands on the firing side of the line, so nothing has to be nudged.
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

    # What an evenly spread history would already put in each window: the
    # number every window tag's line has to beat to mean anything. It is a
    # constant of the window and not of the data, so it is derived from the
    # window's own size through the same share()/pct() pair the measurements
    # go through - a printed "chance" cannot drift from the hours it counts.
    night_chance = pct(share(6, 24))            # 6 of 24 hours -> 25.0
    weekend_chance = pct(share(2, 7))           # 2 of 7 days   -> 28.6
    office_chance = pct(share(9 * 5, 24 * 7))   # 45 of 168 h   -> 26.8
    covered = share(len(nonempty), nweeks)
    idle = (today - last_day).days

    # `last_day` is the newest commit that is not in the future, so when
    # `ahead` commits are dated later this gap was measured from an older
    # one than the streaks panel prints. Unsaid, the two panels contradicted
    # each other four lines apart: "last commit 2036-01-05" above "nothing
    # committed in 141 days". The shorter lead is there for the arithmetic
    # that will not fit the long one - a repo idle for decades with a
    # double-figure pile of future dates - and says the same thing.
    # Both numbers on this line are grouped. The short form used to print
    # "idle 739000 days, ignoring 9,999,999 future dates" - the same kind of
    # count, one row, two spellings - and the metronomic line two rows below
    # already argues that a number should not read one way here and another
    # way somewhere else on the page.
    idle_days = "%s days" % "{:,}".format(idle)
    quiet = "nothing committed in " + idle_days
    if ahead:
        quiet += ", ignoring %s" % count(ahead, "future date")
        if GUTTER + display_width(quiet) + len(" (line: 21 days)") > 80:
            quiet = ("idle %s, ignoring %s"
                     % (idle_days, count(ahead, "future date")))

    # Tested in this order, and at most three print, so the order decides
    # what a repo that fires five is described as. The two tags about
    # *whether* the repo is being worked on go first: "on a tear" and
    # "dormant" answer that before any tag about which hours the work keeps,
    # and a dormant repo whose old commits happened to be nocturnal was
    # printing "nocturnal - weekend-coded - burst-driven" and never getting
    # to the one fact a reader wants first. The rest keep the order they had.
    candidates = [
        # Both lines carry the unit their threshold is measured in. Moved
        # to the top of the order they sit above "(line: 50%)" and
        # "(line: 57%)", where a bare "(line: 5)" reads as another percent.
        (current >= 5, "on a tear",
         "%d days in a row with at least one commit (line: 5 days)"
         % current),
        (idle >= 21, "dormant", "%s (line: 21 days)" % quiet),
        # A window tag's line has to sit above the share an evenly spread
        # history already puts in that window, or the tag fires on the
        # *absence* of a pattern. These two did not: 6 of 24 hours is 25% of
        # an even day and the line was 20%, 2 of 7 days is 28.6% of an even
        # week and the line was 25%. A repo of 168 commits, one per
        # hour-of-week slot - as featureless as a repo can be - printed
        # "dormant - nocturnal - weekend-coded". Both lines are now twice
        # their own baseline. nine-to-five below is left alone: 60% against
        # the 26.8% that 45 of 168 hours gives is already 2.24x, which is
        # why it is the one window tag that never misfired.
        #
        # All three quote their chance next to their line, because the line
        # alone does not say whether it is a pattern: "25% of commits land
        # between 00:00 and 05:59" was a true sentence about a repo with no
        # night habit at all, and only the baseline beside it tells a reader
        # which of the two they are looking at.
        (night >= 50, "nocturnal",
         "%d%% of commits land between 00:00 and 05:59 "
         "(line: 50%%, chance: %d%%)" % (pct(night), night_chance)),
        (weekend >= 57, "weekend-coded",
         "%d%% of commits land on a Saturday or Sunday "
         "(line: 57%%, chance: %d%%)" % (pct(weekend), weekend_chance)),
        (office >= 60, "nine-to-five",
         "%d%% of commits land Mon-Fri, 09:00-17:59 "
         "(line: 60%%, chance: %d%%)" % (pct(office), office_chance)),
        (len(nonempty) >= 4 and ratio >= 3.0, "burst-driven",
         "the busiest week holds %sx the median week (line: 3x)"
         % floor1(ratio)),
        # Two thresholds, one of them a `<` bound, and the only line in the
        # program that ever passed 80 columns - at --weeks 520 an older
        # wording reached 83 and dropped a lone ")" at column 0. "lines"
        # stays plural and "<2x" keeps showing that the second bound points
        # the other way from the first; spelling it "under 2x" cost four
        # columns this line does not have.
        #
        # The coverage is a percentage because the line it quotes is one:
        # "26 of 26 weeks busy ... (lines: 60%, ...)" measured in weeks and
        # quoted in percent, leaving the reader to divide. "the median week"
        # matches burst-driven two rows up, which measures the same ratio
        # against the same denominator and names it.
        #
        # Worst case 77 columns. nweeks tops out in the thousands: --weeks
        # caps at 520, and under --all an author date before 1970 comes out
        # of git as the literal "%aI" and is set aside rather than charted,
        # so the oldest week this line can count from is 1970's. It is
        # written with the thousands separator count() gives the header, so
        # the same number does not read as "1,000 weeks" up there and
        # "1000 weeks" down here.
        (nweeks >= 4 and covered >= 60 and ratio < 2.0, "metronomic",
         "%d%% of %s weeks busy, peak %sx the median week (lines: 60%%, <2x)"
         % (pct(covered), "{:,}".format(nweeks), floor1(ratio))),
    ]
    fired = [(tag, line) for ok, tag, line in candidates if ok]
    # The cap has always been three; until now it was silent about it. The
    # thresholds are published, so a reader who correctly works out a fourth
    # firing tag from their own numbers had no way to tell whether it failed
    # to fire or was simply cut. The count says which. The tags themselves
    # are not named: each one is only worth printing with the arithmetic
    # under it, and the cap is exactly the rule that there are three of
    # those. `cut` is however many were dropped - the order holds seven
    # candidates and says nothing about how many of them can hold at once.
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
    if not opts.color:
        return False
    # no-color.org: the variable disables color "when present and not an
    # empty string". `is not None` honoured `NO_COLOR=` as well, which is
    # how a shell exports a variable it was told to leave unset, and the
    # convention says that case must not suppress.
    if os.environ.get("NO_COLOR"):
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
            # Under width pressure the clarifier used to be the thing that
            # gave way, which left exactly the bare line it was added to
            # prevent. It shortens instead of vanishing.
            who = 'filtered to "" (matches all)'
            if display_width(who) > room:
                who = 'filtered to "" (all)'
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


def render_tempo(weekly, start, clamped, today, ink, g):
    nweeks = len(weekly)
    size = int(math.ceil(nweeks / float(SPARK_COLS)))
    columns = bucket_columns(weekly, size)
    top = max(c[2] for c in columns)
    bar = "".join(spark_glyph(c[2], top, g) for c in columns)

    # Caption the peak *column*, the bar a reader can actually point at -
    # but only when there is one to point at. On a flat repo every week held
    # the same count and max() picked the oldest, so a chart with no shape at
    # all read as "something happened in February".
    # The count is grouped, like every other four-digit number on the page:
    # the header says "1,200 commits" and the clock says "darkest = 1,200
    # commits", and "peak 1200" two rows between them read as a third,
    # different number.
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
    # bucket_columns leaves the short column, if there is one, at the oldest
    # end. Saying only "one column = 16 weeks" made that 9-week bar read as a
    # lull, so the odd column is named whenever it exists.
    #
    # It is named without the verb "holds", which everywhere else in this
    # program counts commits - "every week holds 3", "the busiest week holds
    # 8.3x the median week", and "peak 4" on the very next line - so "the
    # oldest holds 1" read as one commit under a bar that was clearly taller
    # than that. "the leftmost (oldest)" also states, in the only place the
    # panel had room for it, which end of the chart is the old end.
    # Worst case 77 columns, at ten weeks a column and a five-digit rate.
    width = count(size, "week")
    if columns and columns[0][1] != size:
        width += ", the leftmost (oldest) %d" % columns[0][1]
    lines = [
        ink.dim(gutter("tempo")) + bar,
        ink.dim(INDENT + "one column = %s%s%s commits/week"
                % (width, g["sep"], per_week(sum(weekly), nweeks))),
        ink.dim(INDENT + peak_line),
    ]
    # The window is Monday-aligned and ends today, so the newest bar is drawn
    # from a span that has not finished, at full height beside bars that
    # have. A one-commit Monday next to a five-commit week reads as a
    # collapse rather than as a week that is one day old, so the caption
    # says how much of the newest bar's span has actually elapsed.
    #
    # Measured on the column, not on the week. Firing on today.weekday()
    # alone and then saying "6 days into the week" described a week nobody
    # drew: at `one column = 10 weeks` the newest column is 69 days of 70,
    # and calling that a fragment is a plain falsehood.
    #
    # The consequence rides on the same line, because the reconciliation -
    # that the rate above divides by whole weeks including this part-week -
    # was only ever written down in the README, which is not on screen. It
    # is dropped, and only it, when the two day counts run to three digits
    # and the line would pass 80; the arithmetic is still printed, and a
    # column that is 391 days of 392 is not a fragment worth a caveat.
    newest = columns[-1]
    first_day = start + timedelta(days=7 * newest[0])
    span_days, elapsed = 7 * newest[1], (today - first_day).days + 1
    if 0 < elapsed < span_days:
        # One shape at both scales. The bucketed form said "69 of 70 days
        # old" and the week form said "6 days old", which is the same fact
        # in two spellings and the shorter one is ambiguous: a week whose
        # Monday was six days ago has six of its seven days behind it, and
        # "6 days old" reads just as easily as "over already".
        aged = ("the newest %s is %d of %d days old"
                % (unit, elapsed, span_days))
        whole = aged + "; the rate divides by whole weeks"
        lines.append(ink.dim(INDENT + (whole if GUTTER + display_width(whole)
                                       <= 80 else aged)))
    if clamped:
        # Future-dated commits clamp into the newest bucket, which is rarely
        # the peak column. Hung off the peak caption, the note claimed they
        # were in a column that does not hold them; it gets its own line and
        # names the column that does.
        # "with a future date" and not "dated after today", because the
        # streaks panel six rows down discloses the same commits and the
        # page was opening two lines with the same five words. Both still
        # disclose. It is not led with "the newest column" either: the
        # caption two rows up now begins "the newest week/column is", and
        # trading one repetition for another three rows closer is not a fix.
        # "future date" is the phrase the dormant evidence line already uses
        # for the same commits.
        lines.append(ink.dim(INDENT + "%s with a future date, counted in the "
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
    # Shade already has a key on this line; hue had none anywhere, on screen
    # or in --help, while the accent was the one splash of color in the whole
    # program and the first thing a stranger sees in the screenshot. It is
    # printed only when color is actually being emitted: under --no-color,
    # NO_COLOR, TERM=dumb or a pipe there is nothing teal on the page and a
    # key to it would name a color the reader cannot see. Emitting color is
    # necessary and not sufficient: only cells that hold commits are tinted,
    # so a repo whose commits all land in daylight draws nothing teal on a
    # tty either, and the key named a color that was not on the page. The
    # `tinted` test is the same condition the loop above tints on.
    #
    # One wording, not a long one that shortens under pressure: the hours are
    # written the way the ruler two rows up writes them, and a key that said
    # "00:00-05:59" on a small repo and "00-05" on a large one would be the
    # same fact in two shapes. It costs 15 cells, and this line runs to 65
    # before it - so it fits unless one hour of one weekday holds a million
    # commits, at which point the key drops rather than the line running wide.
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
        # The date is spelled out rather than called "it". The nearest thing
        # for a pronoun to attach to was "today", which made the sentence say
        # that dates after today can run ahead of today; the date it actually
        # means is the one ending the header, five lines up and never named.
        lines.append(ink.dim(INDENT + "%s dated after today; these dates can "
                             "pass %s" % (count(clamped, "commit"),
                                          today.isoformat())))
    return lines


def render_mood(tags, evidence, cut, ink, g):
    """The tag line, then one evidence line per printed tag - never more
    than three of either, whatever `cut` says was left off."""
    named = g["sep"].join(ink.bold(tag) for tag in tags)
    if cut:
        # A plain space, not the tag separator: " . +2 more" sitting in the
        # same slot the separator marks would read as a fourth tag, which is
        # the confusion this suffix exists to end. Dim for the same reason -
        # it is a note about the list, not a member of it.
        named += ink.dim(" +%d more" % cut)
    lines = [ink.dim(gutter("mood")) + named]
    lines.extend(INDENT + line for line in evidence)
    return lines


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def write(stream, text, ascii_=False):
    """A repo or author name can hold characters the stream cannot encode.

    Replace them instead of raising; under --ascii force plain ASCII so the
    whole output stays below U+0080 whatever the repo is called. A stream that
    is closed (`git-mood >&-` leaves sys.stdout as None) is not a crash.

    `ascii_` is the --ascii flag and only that flag, on both streams alike.
    It used to be hard-coded True for every stderr write, which made the two
    streams disagree about the same string on a run that never asked for
    ASCII: stdout printed an author of 漢字テスト as typed while the "you
    typed:" echo, whose whole job is to show the user their own text, printed
    a row of "?". Reading the flag instead of assuming it keeps that fixed and
    still lets --ascii cover stderr, which is where a path or an author value
    the terminal cannot draw is most likely to end up.
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

    --all reads one branch, not the repository: "the whole history" promised
    the reader that a commit on another branch would have been counted, and
    sent them looking for a bug when it was not.
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
        # Only there, though: a perfectly ordinary work tree may be called
        # `notbare.git`, and stripping the suffix off that one named a
        # directory nobody has.
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
        # These belong to no week and no hour, so they cannot go on a panel.
        # The note goes to stderr: it survives `| head -1`, and it cannot
        # change what any number already on the chart means.
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
            #
            # The needle is not echoed a second time. The header three lines
            # up already prints it, and the two echoes were cut to different
            # budgets: `filtered to "zzzznobody"` above `no commits by
            # "zzzzn..."` read as two different searches, and under --all the
            # second one printed in full so the pair disagreed by invocation.
            fix = " try --all, or a shorter --author." if advice \
                  else " try a shorter --author."
            return page(render_summary([], opts, nweeks, start, today, g),
                        "nothing matched in %s.%s" % (where, fix))

    weekly = weekly_counts(commits, start, nweeks)
    # Against `today`, not against the Sunday that ends today's week: a commit
    # dated tomorrow is as much in the future as one dated next month, and
    # author-local dates routinely run a day ahead across timezones.
    clamped = sum(1 for c in commits if c.date > today)
    days = set(c.date for c in commits)
    best, best_start, best_end, current, anchor = streaks(days, today)
    # The dormant tag measures the gap since the last commit, so it reads the
    # newest date that is not in the future: a single commit dated next month
    # otherwise sets `idle` negative and silences the tag on a repo that has
    # in fact been quiet for a year. The streaks panel below still gets the
    # commits' own maximum - it prints those dates and says they run ahead.
    tags, evidence, cut = mood(
        commits, weekly, nweeks, current,
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

    Read before parse_args(), because parse_args()'s own errors go to stderr
    and --ascii is a promise about everything this program writes, not only
    about the chart. Reading it during the scan would have honoured the flag
    only when it came before the mistake. The scan stops at `--` for the same
    reason parse_args() does: after it, `--ascii` is a directory name.
    """
    for arg in argv:
        if arg == "--":
            return False
        if arg == "--ascii":
            return True
    return False


def main(argv):
    # Without this, `git-mood | head -3` raises BrokenPipeError on exit.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ascii_ = ascii_requested(argv)
    # Before parse_args(), for the same reason ascii_requested() is read
    # there: parse_args()'s own errors are output too.
    globals()["ASCII_OUT"] = ascii_
    try:
        opts = parse_args(argv)
        plain = ascii_only(opts, GLYPHS)
        lines = build(opts, date.today(),
                      ASCII_GLYPHS if plain else GLYPHS,
                      Ink(color_enabled(opts)))
        emit("\n".join(lines) + "\n", opts.ascii_)
    except Usage as exc:
        # The generic pointer steps aside for a specific one. `--author -x`
        # printed "try: git-mood --help" as a clause on line 1 and the
        # sentence that actually fixes the command on line 3, so the reader
        # met the weaker answer first and the stronger one after two lines
        # of looking. When there is no advice the tip is all there is.
        # One decision, read twice. Testing `exc.advice` here and its
        # width below would drop both on an advice too wide to print,
        # leaving the reader a message, their own token, and no next step.
        advice = exc.advice if exc.advice and len(PROG) + 2 \
            + display_width(exc.advice) <= 80 else None
        tail = "; try: %s --help" % PROG if exc.tip and not advice else ""
        write(sys.stderr, "%s: %s%s\n" % (PROG, exc, tail), ascii_)
        if exc.echo is not None:
            # Quoted, because an empty value is a thing the user typed too:
            # `--weeks=` printed "you typed:" and then a trailing space, so
            # the one line meant to show the argument showed nothing at all.
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
