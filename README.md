# git-mood

Reads a git repository's history and draws a mood chart in your terminal: tempo, a punch-card clock, streaks, and a verdict that shows its arithmetic.

![git-mood run with --weeks 52 against a clone of simonw/llm](screenshot.png)

*Captured from this build: `git-mood --weeks 52` against a `--filter=blob:none` clone of [simonw/llm](https://github.com/simonw/llm).*

## What it does

One `git log` call becomes four panels: a sparkline of commits per week, a 7×24 punch card for every hour of the week, the longest and current daily streaks, and up to three "mood" tags. Every tag prints the measured number and the threshold it crossed, so you can disagree with it — a tag that cannot show its arithmetic is not in the table. Times are the author's own local clock exactly as recorded in each commit; nothing is converted to your timezone. The default window is the last 26 weeks, and every number on screen describes that same set of commits.

The whole table of tags, so you can check one against the output:

| tag | fires when |
|---|---|
| `nocturnal` | ≥20% of commits between 00:00 and 05:59 |
| `weekend-coded` | ≥25% of commits on a Saturday or Sunday |
| `nine-to-five` | ≥60% of commits Mon–Fri, 09:00–17:59 |
| `burst-driven` | ≥4 weeks with commits, and the busiest is ≥3× their median |
| `metronomic` | a window of ≥4 weeks, ≥60% of them with a commit, and the busiest <2× the median |
| `on a tear` | a current streak of ≥5 days |
| `dormant` | nothing committed in 21 days |
| `unremarkable` | nothing above fired |

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
python3 git_mood.py --all                # the entire history
python3 git_mood.py --author ada         # only commits whose author matches
python3 git_mood.py --ascii --no-color   # plain ASCII, no ANSI
python3 git_mood.py --help
```

To use it as a git subcommand, put the directory on your `PATH`:

```sh
PATH="$PWD:$PATH" git mood
```

## Why it exists

A repository already knows what hours you keep; it just never says so out loud. This asks it, and prints the number next to every answer so the joke stays checkable.

---

*Day 012 of an autonomous build factory — [factory-hub](https://github.com/yinggarykairui/factory-hub)*
