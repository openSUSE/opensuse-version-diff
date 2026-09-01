# osdiff — openSUSE source package version diff

### ➜ **[View the live version diff](https://opensuse.github.io/opensuse-version-diff/)**

Compares source package versions between openSUSE distributions (by default
Tumbleweed vs Leap 16.1) using the `ARCHIVES.gz` indexes published in every
repository, and adds maintainer information from PackageHub.

Further distributions can ride along as read-only columns — the published page
shows **Tumbleweed · Leap 16.1 · Leap 16.0**, newest first, and picks up a
future Leap on its own (see [Extra columns](#extra-columns)).

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
| Leap 16.0 oss | `https://download.opensuse.org/distribution/leap/16.0/repo/oss/ARCHIVES.gz` | `ARCHIVES_160` |
| Leap 16.0 non-oss | `https://download.opensuse.org/distribution/leap/16.0/repo/non-oss/ARCHIVES.gz` | `ARCHIVES_160_nonoss` |
| Maintainers | `https://src.opensuse.org/products/PackageHub.git` (`leap-16.1`) | `.packagehub/_maintainership.json` |

Only the two compared distributions are downloaded; Leap 16.0 costs nothing
until you ask for it with `--extra leap160`.

Missing inputs are fetched automatically: the ARCHIVES files over HTTP (and
decompressed), the maintainership data via a shallow sparse `git clone` — git
avoids the bot check that guards the src.opensuse.org web interface. Everything
is reused on later runs, so re-runs take about two seconds.

Local copies are never re-downloaded unless you ask for it. `--refresh` issues
one HEAD request per repository and only pulls a file whose `Last-Modified` or
size actually changed, which keeps a scheduled rebuild at zero mirror traffic on
the days nothing moved. Requests carry a `User-Agent` naming this project so
download.opensuse.org admins can attribute (and complain about) the traffic.

non-oss contributes 31 source packages (`discord`, `opera`, `steam`, `unrar`,
`wine-mono`, …); the JSON export records per package which repository each side
came from, in `left_repos` / `right_repos` (and `extras[].repos`).

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
| `Only-in-TW` | not in the compared Leap |
| `Only-in-Leap` | not in Tumbleweed |

Extra columns do not get statuses of their own — one per side beats one per
release, and the version columns already say which Leap has what. So
`Only-in-Leap` covers a package any Leap column has, and `Only-in-TW` a package
the compared Leap lost even if an older one still ships it (33 packages are in
Tumbleweed and Leap 16.0 but were dropped in 16.1). Missing versions read `—`.

Only the **upstream version** is compared, not the release — Leap and
Tumbleweed use unrelated release schemes (`bp161.1.2` vs `1.2`), so comparing
them would mark everything as different. Use `--with-release` to include it.
Comparison itself goes through rpm's own `labelCompare` when `python3-rpm` is
installed, with a built-in `rpmvercmp` as fallback.

A handful of `Newer-in-Leap` hits are version-scheme artifacts rather than real
regressions — Tumbleweed's normalized perl versions (`1.291.100` vs `1.2911`)
or a stray `v` prefix compare that way under rpm rules.

## Extra columns

`--extra DISTRO` (repeatable) adds a distribution as a further version column
to the right of `--right`, newest first — an older Leap is the least
interesting column, so it lands furthest from the versions the diff is about:

```sh
./osdiff.py --extra leap160                    # Tumbleweed | Leap 16.1 | Leap 16.0
./osdiff.py --extra leap160 --format html -o diff.html
```

Extra columns are read-only annotations — only `--left` and `--right` are
compared. They do widen the row set, though, so a package that only survives in
an extra column is not silently lost: it shows up as `Only-in-Leap` with an
empty 16.1 cell (79 packages exist in Leap 16.0 but in neither Tumbleweed nor
Leap 16.1).

`--discover` HEAD-probes `download.opensuse.org` for the Leap releases this
script has no entry for (16.2 … 16.9) and adds every one that is already
published as an extra column. An unreleased version costs a single 404, so the
CI job runs with `--extra leap160 --discover` and the page will grow a Leap
16.2 column on the day that repository appears — no commit needed here.

## Options

```
--left/--right DISTRO   which distros to compare (tumbleweed, leap161, leap160)
--extra DISTRO          add a distro as an extra version column (repeatable)
--discover              probe for Leap releases not known yet and add them too
--format FMT            table (default), md, csv, json, html
--only STATUS           filter by status substring, e.g. --only older (repeatable)
--grep REGEX            filter source package names
--maintainers           add a maintainers column to table/md/csv
--maintainer NAME       only packages maintained by NAME
--no-maintainers        skip loading maintainer data entirely
--with-release          compare the rpm release too
--refresh               re-check upstream for newer ARCHIVES indexes
-o FILE                 write output to FILE
-q                      no progress/summary on stderr
```

## Examples

```sh
# Everything you maintain, with the maintainer column shown
./osdiff.py --maintainer lkocman --maintainers

# ... or only the packages a maintainer has fallen behind on
./osdiff.py --maintainer lkocman --only older

# Python stack, as markdown for a report
./osdiff.py --only older --grep '^python-' --format md -o python.md

# Full dataset for further processing
./osdiff.py --format json -o diff.json
```

## Current numbers (Tumbleweed vs Leap 16.1, oss + non-oss, x86_64 + noarch)

Run with `--extra leap160`: 17532 source packages in total — 17143 in
Tumbleweed, 10574 in Leap 16.1 and 10551 in Leap 16.0, of which 10264 exist in
both compared distros:

| Status | Count |
| --- | --- |
| Older-in-Leap | 5027 |
| Newer-in-Leap | 26 |
| Same | 5211 |
| Only-in-TW | 6879 |
| Only-in-Leap | 389 |

The JSON export carries the same figures under `totals` and `summary`.

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

The ARCHIVES indexes are never committed — `.gitignore` keeps them out. CI
caches the compressed ones between runs and passes `--refresh`, so a scheduled
rebuild normally costs six HEAD requests for the indexes plus eight for
`--discover`, and downloads only what changed upstream. A cold cache pulls
~360 MB.
