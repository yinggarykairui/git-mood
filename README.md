# git-mood

Reads a git repository's history and draws a mood chart in your terminal: tempo, a punch-card clock, streaks, and a verdict that shows the arithmetic behind every tag that fired on a threshold.

![git-mood run with --weeks 26 against a clone of simonw/llm](screenshot.png)

*Captured from this build: `git-mood --weeks 26` — the default window — against a `--filter=blob:none` clone of [simonw/llm](https://github.com/simonw/llm).*

## What it does

One `git log` call becomes four panels: a sparkline of commits per week, a 7×24 punch card for every hour of the week, the longest and current daily streaks, and up to three mood tags. Every tag that fires on a threshold prints the number it measured and the line it crossed, so you can disagree with it. Times are the author's own local clock exactly as recorded in each commit; nothing is converted to your timezone. The default window is the last 26 weeks of the current branch, and every number on screen describes that same set of commits.

## The tags

Tested in this order; at most three print, and if more fired the tag line ends `+N more` for the count it cut, without naming them.

A tag about a window of the week has to clear more than the window's own size, or it fires on the absence of a pattern: 6 of 24 hours is a quarter of an evenly spread day, so the old `nocturnal` line of 20% fired on no night habit at all. Each of the three now sits at roughly twice the share an evenly spread history would put in its window, and prints that share beside its line as `chance`.

| tag | fires when | chance |
|---|---|---|
| `on a tear` | a current streak of ≥5 days | — |
| `dormant` | nothing committed for ≥21 days | — |
| `nocturnal` | ≥50% of commits between 00:00 and 05:59 | 25% |
| `weekend-coded` | ≥57% of commits on a Saturday or Sunday | 29% |
| `nine-to-five` | ≥60% of commits Mon–Fri, 09:00–17:59 | 27% |
| `burst-driven` | ≥4 weeks with commits, and the busiest is ≥3× their median | — |
| `metronomic` | a window of ≥4 weeks, ≥60% of them with a commit, and the busiest <2× the median | — |
| `unremarkable` | nothing above fired | — |

Three things the numbers do not say:

- **The newest column is short.** It runs to today, so it is short of its full span unless today is a Sunday. The caption says how many of its days have elapsed; the `commits/week` rate divides by whole weeks anyway.
- **`N authors` counts addresses.** Distinct author emails, lower-cased, so two people sharing one count once. `--author` matches the composed `Name <email>` instead.
- **The width is fixed.** The layout is built for 80 columns and never exceeds them; `COLUMNS` is not read, and the header rule grows from 60 toward that 80 only to cover its own contents.

## How to run

Python 3.8 or newer and `git` on your `PATH`. Nothing to install.

```sh
git clone https://github.com/yinggarykairui/git-mood.git
cd git-mood
python3 git_mood.py                      # the repo you are standing in
./git-mood ~/code/some-other-repo        # or any directory inside one
```

Options:

```sh
python3 git_mood.py --weeks 8            # a shorter window (1-520, default 26)
python3 git_mood.py --all                # the current branch's entire history
python3 git_mood.py --author ada         # substring of "Name <email>"
python3 git_mood.py --ascii --no-color   # plain ASCII, no ANSI
python3 git_mood.py -- --weird-dir-name  # end the options; the rest is the path
python3 git_mood.py --help
```

To use it as a git subcommand, put the directory on your `PATH`:

```sh
PATH="$PWD:$PATH" git mood
```

## Why it exists

Seeded: it was idea [#4](https://github.com/yinggarykairui/factory-hub/issues/4) in the factory's warm-start queue. A repository already knows what hours you keep; it just never says so out loud.

---

*Day 012 of an autonomous build factory — [factory-hub](https://github.com/yinggarykairui/factory-hub)*
