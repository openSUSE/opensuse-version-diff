# osdiff — openSUSE source package version diff

Compares source package versions between openSUSE distributions (by default
Tumbleweed vs Leap 16.1) using the `ARCHIVES.gz` indexes published in every
repository, and adds maintainer information from PackageHub.

Only **x86_64 and noarch** packages are considered. Tumbleweed's ARCHIVES index
covers no other arch, so counting Leap's `aarch64`/`ppc64le`/`s390x` packages
would only invent rows that look Leap-only.

```
./osdiff.py                      # full table
./osdiff.py | grep Older         # what Leap is behind on
./osdiff.py --only older         # same, done properly
./osdiff.py --format html -o diff.html   # interactive, local-repology-ish view
```

## Data sources

Both the **oss and non-oss** repositories of each distribution are read and
merged:

| What | Where | Local file |
| --- | --- | --- |
| Tumbleweed oss | `https://download.opensuse.org/tumbleweed/repo/oss/ARCHIVES.gz` | `ARCHIVES_TW` |
| Tumbleweed non-oss | `https://download.opensuse.org/tumbleweed/repo/non-oss/ARCHIVES.gz` | `ARCHIVES_TW_nonoss` |
| Leap 16.1 oss | `https://download.opensuse.org/distribution/leap/16.1/repo/oss/ARCHIVES.gz` | `ARCHIVES_161` |
| Leap 16.1 non-oss | `https://download.opensuse.org/distribution/leap/16.1/repo/non-oss/ARCHIVES.gz` | `ARCHIVES_161_nonoss` |
| Maintainers | `https://src.opensuse.org/products/PackageHub.git` (`leap-16.1`) | `.packagehub/_maintainership.json` |

Missing inputs are fetched automatically: the ARCHIVES files over HTTP (and
decompressed), the maintainership data via a shallow sparse `git clone` — git
avoids the bot check that guards the src.opensuse.org web interface. Everything
is reused on later runs, so re-runs take about two seconds.

non-oss contributes 31 source packages (`discord`, `opera`, `steam`, `unrar`,
`wine-mono`, …); the JSON export records per package which repository each side
came from, in `left_repos` / `right_repos`.

Maintainers are only known for the ~5.3k packages Leap gets from PackageHub;
the core packages inherited from SLES/Factory (release `160099.*`) and the
non-oss packages are not listed there and show up empty.

## Status column

Every row carries one greppable status. The first word is always the same, so
`grep Older` works no matter which distros are compared:

| Status | Meaning |
| --- | --- |
| `Older-in-Leap` | Leap is behind Tumbleweed |
| `Newer-in-Leap` | Leap is ahead |
| `Same` | same upstream version |
| `Only-in-TW` | not in Leap at all |
| `Only-in-Leap` | not in Tumbleweed |

Only the **upstream version** is compared, not the release — Leap and
Tumbleweed use unrelated release schemes (`bp161.1.2` vs `1.2`), so comparing
them would mark everything as different. Use `--with-release` to include it.
Comparison itself goes through rpm's own `labelCompare` when `python3-rpm` is
installed, with a built-in `rpmvercmp` as fallback.

A handful of `Newer-in-Leap` hits are version-scheme artifacts rather than real
regressions — Tumbleweed's normalized perl versions (`1.291.100` vs `1.2911`)
or a stray `v` prefix compare that way under rpm rules.

## Options

```
--left/--right DISTRO   which distros to compare (tumbleweed, leap161, leap160)
--format FMT            table (default), md, csv, json, html
--only STATUS           filter by status substring, e.g. --only older (repeatable)
--grep REGEX            filter source package names
--maintainers           add a maintainers column to table/md/csv
--maintainer NAME       only packages maintained by NAME
--no-maintainers        skip loading maintainer data entirely
--with-release          compare the rpm release too
-o FILE                 write output to FILE
-q                      no progress/summary on stderr
```

## Examples

```sh
# Everything a given maintainer has fallen behind on
./osdiff.py --maintainer mnhauke --only older

# Python stack, as markdown for a report
./osdiff.py --only older --grep '^python-' --format md -o python.md

# Full dataset for further processing
./osdiff.py --format json -o diff.json
```

## Current numbers (Tumbleweed vs Leap 16.1, oss + non-oss, x86_64 + noarch)

17453 source packages: 5027 Older-in-Leap, 26 Newer-in-Leap, 5211 Same,
6879 Only-in-TW, 310 Only-in-Leap.

## Publishing to GitHub Pages

`.github/workflows/pages.yml` regenerates the site and deploys it every morning
(05:17 UTC), on every push to `main`, and on demand via *Run workflow*. It needs
no secrets and no dependencies — the runner has no `python3-rpm`, so osdiff uses
its built-in `rpmvercmp`, which is verified to agree with rpm's `labelCompare`
on every package in the current data set.

One-time setup after pushing: **Settings → Pages → Source: GitHub Actions**.

The deployed site contains:

| Path | What |
| --- | --- |
| `index.html` | the interactive table (self-contained, ~2 MB) |
| `diff.json` / `diff.json.gz` | full data set including per-package repos and arches |
| `diff.csv` | flat table with maintainers |

The ARCHIVES indexes are never committed — `.gitignore` keeps them out, and CI
re-downloads them (~230 MB) on each run.
