#!/usr/bin/env python3
"""Compare source package versions between openSUSE distributions.

Reads the ARCHIVES(.gz) indexes of the oss and non-oss repositories, groups the
x86_64 and noarch binary packages by their source RPM and prints a table of the
versions found in each distribution.

The status column is meant to be grepped:

    ./osdiff.py | grep Older            # Leap behind Tumbleweed
    ./osdiff.py | grep Only-in-TW       # not in Leap at all

Maintainers are pulled from the PackageHub git repository, so they are only
known for the packages Leap gets from PackageHub (the core packages inherited
from SLES/Factory are not listed there).

Machine readable output (--format json/csv) is there so this can grow into a
local repology-ish index later on.
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import html
import json
import mmap
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Only x86_64 and noarch are compared.  Tumbleweed's ARCHIVES index covers no
# other arch, so pulling in Leap's aarch64/ppc64le/s390x packages would only
# invent packages that look Leap-only.
ARCHES = frozenset({"x86_64", "noarch"})

# Tumbleweed's other architectures live under /ports, one tree per port, and
# they are read as a *lookaside*: consulted only for source packages the
# x86_64/noarch media do not have.  That is what turns "Leap has s390-tools and
# Tumbleweed does not" back into the truth, and it surfaces the arch-specific
# packages (yast2-s390, libica, u-boot, …) that never reach x86_64 at all.
PORTS_BASE = "https://download.opensuse.org/ports"
PORTS_TREES = ("aarch64", "riscv", "zsystems", "ppc", "i586")
# The 32-bit arm ports redirect to the aarch64 tree, so asking for armv7hl is
# the same download as asking for aarch64, not another 151 MB.  The rest are
# the names people actually say, mapped to the directory that serves them.
PORTS_ALIASES = {
    "arm": "aarch64", "armv6hl": "aarch64", "armv7hl": "aarch64",
    "s390x": "zsystems", "s390": "zsystems", "riscv64": "riscv",
    "ppc64": "ppc", "ppc64le": "ppc", "i686": "i586", "x86": "i586",
}
# Directories in a ports tree that hold something other than packages.
NON_ARCH_DIRS = frozenset({"boot", "src", "nosrc", "repodata", "media.1", "EFI"})

# Status values.  Keep the first word greppable and stable: whatever distros are
# compared, `grep Older` always means "the compared distro is behind".
def statuses(left: Distro, right: Distro, upstream: bool = False) -> dict:
    """The status vocabulary for this run.

    `Perfection` only exists when there is an upstream column to earn it
    against; without --repology the set stays at five, rather than offering a
    filter that can never match.
    """
    st = {
        "older": f"Older-in-{right.tag}",
        "newer": f"Newer-in-{right.tag}",
        "same": "Same",
        "only_left": f"Only-in-{left.tag}",
        "only_right": f"Only-in-{right.tag}",
    }
    if upstream:
        # Both sides level *and* nobody ships anything newer.  A subset of
        # Same, split out because "as good as it gets" is the thing worth
        # seeing in a table that is otherwise a list of what is behind.
        st = {"perfect": "Perfection", **st}
    return st


@dataclass
class Repo:
    name: str  # oss / non-oss
    path: str  # local, uncompressed copy
    url: str
    keep_gz: bool = False  # ports: read straight from the gzip, never expand it


@dataclass
class Distro:
    key: str
    label: str
    tag: str
    repos: list


def _repos(base, oss_path, names=("oss", "non-oss")) -> list:
    """One Repo per repository; non-oss reuses the oss naming."""
    return [
        Repo(n, oss_path if n == "oss" else f"{oss_path}_{n.replace('-', '')}",
             f"{base}/{n}/ARCHIVES.gz")
        for n in names
    ]


TW_BASE = "https://download.opensuse.org/tumbleweed/repo"
# --discover probes for Leap releases that do not have an entry below yet, so a
# 16.2 shows up in the published table on the day its repository goes live
# instead of on the day somebody remembers to patch this file.
LEAP_BASE = "https://download.opensuse.org/distribution/leap"
LEAP_PROBE = [f"16.{minor}" for minor in range(0, 10)]

DISTROS = {
    d.key: d
    for d in (
        Distro("tumbleweed", "Tumbleweed", "TW", _repos(TW_BASE, "ARCHIVES_TW")),
        Distro("leap161", "Leap 16.1", "Leap",
               _repos(f"{LEAP_BASE}/16.1/repo", "ARCHIVES_161")),
        Distro("leap160", "Leap 16.0", "Leap160",
               _repos(f"{LEAP_BASE}/16.0/repo", "ARCHIVES_160")),
    )
}


# Sent on every HTTP request so download.opensuse.org admins can attribute the
# traffic (and reach the project) if it ever becomes a nuisance.
PROJECT_URL = "https://github.com/openSUSE/opensuse-version-diff"
USER_AGENT = f"osdiff/1.0 (+{PROJECT_URL})"

# Maintainership comes from the PackageHub product repo.  Cloning it over git
# avoids the bot check that guards the src.opensuse.org web interface.
MAINTAINERS_REPO = "https://src.opensuse.org/products/PackageHub.git"
MAINTAINERS_BRANCH = "leap-16.1"
MAINTAINERS_FILE = "_maintainership.json"
MAINTAINERS_CLONE = ".packagehub"

# Repology supplies the "newest version anyone ships" column.  Only the
# projects where Tumbleweed is *outdated* are downloaded: for the rest Repology
# by definition has nothing newer than Tumbleweed, so its own version is the
# answer.  That is ~17 pages instead of ~86, and the pages are the expensive
# part — each one carries every repository's take on 200 projects.
REPOLOGY_URL = "https://repology.org"
REPOLOGY_API = f"{REPOLOGY_URL}/api/v1/projects/"
REPOLOGY_REPO = "opensuse_tumbleweed"  # the only openSUSE repo Repology tracks
REPOLOGY_CACHE = "repology_newest.json"
REPOLOGY_PAGE = 200  # projects per response, fixed by the API
REPOLOGY_DELAY = 1.0  # seconds between requests, as their docs ask
REPOLOGY_MAX_PAGES = 200  # stop runaway pagination rather than trust the feed


# --------------------------------------------------------------------------
# version comparison
# --------------------------------------------------------------------------

try:  # python3-rpm gives us the real thing
    from rpm import labelCompare as _label_compare  # type: ignore

    HAVE_RPM = True
except ImportError:  # pragma: no cover - fallback for hosts without python3-rpm
    HAVE_RPM = False

    def _rpmvercmp(a: str, b: str) -> int:
        """Pure python port of rpmvercmp() from rpm's rpmvercmp.c.

        Follows the C original closely, including the `~` (sorts before
        everything, used for pre-releases) and `^` (sorts after the base
        version) separators — dropping those flips comparisons like
        `3.0.0` vs `3.0.0~alpha1`.
        """
        if a == b:
            return 0
        i, j = 0, 0
        la, lb = len(a), len(b)
        while i < la or j < lb:
            while i < la and not (a[i].isalnum() or a[i] in "~^"):
                i += 1
            while j < lb and not (b[j].isalnum() or b[j] in "~^"):
                j += 1

            # tilde sorts before everything else
            if (i < la and a[i] == "~") or (j < lb and b[j] == "~"):
                if i >= la or a[i] != "~":
                    return 1
                if j >= lb or b[j] != "~":
                    return -1
                i += 1
                j += 1
                continue

            # caret is like tilde, except that a plain base version is lower
            if (i < la and a[i] == "^") or (j < lb and b[j] == "^"):
                if i >= la:
                    return -1
                if j >= lb:
                    return 1
                if a[i] != "^":
                    return 1
                if b[j] != "^":
                    return -1
                i += 1
                j += 1
                continue

            if i >= la or j >= lb:
                break

            # grab the first completely alpha or completely numeric segment
            si, sj = i, j
            if a[i].isdigit():
                while si < la and a[si].isdigit():
                    si += 1
                while sj < lb and b[sj].isdigit():
                    sj += 1
                isnum = True
            else:
                while si < la and a[si].isalpha():
                    si += 1
                while sj < lb and b[sj].isalpha():
                    sj += 1
                isnum = False
            seg_a, seg_b = a[i:si], b[j:sj]

            # numeric segments are always newer than alpha ones
            if not seg_b:
                return 1 if isnum else -1
            if isnum:
                seg_a = seg_a.lstrip("0")
                seg_b = seg_b.lstrip("0")
                if len(seg_a) != len(seg_b):  # more digits wins
                    return 1 if len(seg_a) > len(seg_b) else -1
            if seg_a != seg_b:
                return 1 if seg_a > seg_b else -1
            i, j = si, sj

        if i >= la and j >= lb:
            return 0
        return -1 if i >= la else 1

    def _label_compare(t1, t2):  # noqa: D103
        for i in (0, 1, 2):
            v1 = t1[i] or ("0" if i == 0 else "")
            v2 = t2[i] or ("0" if i == 0 else "")
            rc = _rpmvercmp(str(v1), str(v2))
            if rc:
                return rc
        return 0


# --------------------------------------------------------------------------
# version normalization
# --------------------------------------------------------------------------
#
# rpmvercmp is a string algorithm: it has no idea what a version *means*.  Three
# scheme mismatches make it answer confidently and wrongly, and all three show
# up between Tumbleweed and Leap.  They are normalized for comparison only —
# what the table prints stays the NVR as packaged, because a mis-versioned
# package is a packaging bug worth seeing, not one to paper over.
#
# Each rule is deliberately narrow.  A rule that fires where it should not is
# worse than one that misses: it invents a difference nobody can explain.

# A stray `v` in front of the number, e.g. dysk's `v3.6.1`.  rpm reads that as
# an alpha segment, and alpha always loses to numeric — so `v3.6.1` < `2.9.1`.
_V_PREFIX_RE = re.compile(r"^[vV](?=\d)")

# A pre-release marker glued to the version without the `~` that tells rpm it
# sorts *before* the release: `4.18.0rc1` should be below `4.18.0`, but rpm puts
# it above.  Anchored between a digit and a boundary so it cannot hit a git
# hash (hex has no `r`/`v`/`t`/`l`/`p`) or the middle of a longer word.
_PRERELEASE_RE = re.compile(
    r"(?<=\d)(rc|alpha|beta|pre|dev)(?=\d|$|[.+~_^-])", re.IGNORECASE
)

# CPAN ships one version in two notations, and openSUSE's perl packages use
# both: the decimal `1.111017` and the v-string `1.111.17` are the same
# release.  perl reads the fraction in groups of three, so the conversion is
# exact — and it is not order-preserving under rpm rules, which is the whole
# problem (`3.24` is perl's 3.240.0, i.e. *newer* than `3.9`'s 3.900.0 is not:
# 3.9 wins, though rpm says 24 > 9).
_PERL_DECIMAL_RE = re.compile(r"^(\d+)\.(\d+)$")


def _perl_decimal(version: str) -> str:
    """`1.111017` -> `1.111.17`, perl's own `version->parse(x)->normal`."""
    m = _PERL_DECIMAL_RE.match(version)
    if not m:
        return version  # already dotted, or not a bare decimal
    whole, frac = m.groups()
    frac = frac.ljust(-(-len(frac) // 3) * 3, "0")  # pad to a multiple of three
    groups = [str(int(frac[i:i + 3])) for i in range(0, len(frac), 3)]
    while len(groups) < 2:  # perl normalizes to three components: 0.69 -> 0.690.0
        groups.append("0")
    return ".".join([whole, *groups])


def normalize_version(name: str, version: str) -> tuple:
    """Return the version to *compare* with, plus the rules that changed it.

    Never used for display.  Scoping matters: the CPAN rule is restricted to
    `perl-*` because two-component versions are ordinary elsewhere — lua's
    `lua53-cliargs` 3.02 is 3.02, not perl's 3.20.0.
    """
    rules = []
    v = version
    if _V_PREFIX_RE.match(v):
        v = v[1:]
        rules.append("v-prefix")
    if name.startswith("perl-"):
        pv = _perl_decimal(v)
        if pv != v:
            v = pv
            rules.append("cpan-decimal")
    pv = _PRERELEASE_RE.sub(lambda m: "~" + m.group(0), v)
    if pv != v:
        v = pv
        rules.append("pre-release")
    return v, tuple(rules)


def evr_cmp(a: Package, b: Package, with_release: bool) -> int:
    """Compare two packages, optionally including the release."""
    ra = a.release if with_release else ""
    rb = b.release if with_release else ""
    return _label_compare((None, a.cmp_version, ra), (None, b.cmp_version, rb))


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


@dataclass
class Package:
    name: str
    version: str
    release: str
    arches: set = field(default_factory=set)
    repos: set = field(default_factory=set)

    def __post_init__(self):
        # What comparison uses; `version`/`vr` stay as packaged, for display.
        self.cmp_version, self.cmp_rules = normalize_version(self.name, self.version)

    @property
    def vr(self) -> str:
        return f"{self.version}-{self.release}"


# ./x86_64/libbz2-1-1.0.8-6.1.x86_64.rpm:    Source RPM  : bzip2-1.0.8-6.1.src.rpm
#
# `nosrc` counts too.  A spec whose sources are not redistributable — or simply
# too big to publish twice — is built as a *no-source* rpm, and rpm records that
# in the same field with a different suffix.  Reading only `.src.rpm` silently
# dropped chromium, bun, every rust bootstrap and every kernel flavour: not a
# handful of oddities but some of the most-watched packages in the distribution,
# and worse than absent, because a package that is nosrc on one side and src on
# the other (Leap 16.0's chromium is) then reads as a real difference.
SRCRPM_RE = re.compile(rb":    Source RPM  : ([^\n]+)\.(?:no)?src\.rpm\n")

_CHUNK = 64 << 20  # bytes per read when streaming a gzipped index


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    """Identify ourselves, so mirror admins can tell what this traffic is."""
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def _meta(headers) -> dict:
    """What `--refresh` compares: the two headers that say a file moved."""
    return {
        "last_modified": headers.get("Last-Modified"),
        "size": headers.get("Content-Length"),
    }


def _remote_meta(url: str) -> dict:
    """Last-Modified/size of the remote file, via a HEAD request."""
    with urllib.request.urlopen(_request(url, "HEAD"), timeout=60) as resp:
        return _meta(resp.headers)


def _exists(url: str) -> bool:
    try:
        _remote_meta(url)
        return True
    except urllib.error.HTTPError:  # 404 on a release that is not out yet
        return False
    except (urllib.error.URLError, OSError) as err:
        print(f"warning: cannot probe {url} ({err})", file=sys.stderr)
        return False


def discover_leaps(quiet: bool = False) -> list:
    """Probe download.opensuse.org for Leap releases DISTROS does not know.

    A version is only taken if its oss repository has an ARCHIVES.gz, so an
    unreleased 16.7 costs exactly one 404 and nothing else.  non-oss is probed
    only once oss is there, and skipped when absent — an early release may well
    publish the two at different times.
    """
    found = []
    for version in LEAP_PROBE:
        key = "leap" + version.replace(".", "")
        if key in DISTROS:
            continue
        base = f"{LEAP_BASE}/{version}/repo"
        path = "ARCHIVES_" + version.replace(".", "")
        oss, non_oss = _repos(base, path)
        if not _exists(oss.url):
            continue
        repos = [oss] + ([non_oss] if _exists(non_oss.url) else [])
        if not quiet:
            print(f"discovered Leap {version} ({', '.join(r.name for r in repos)})",
                  file=sys.stderr)
        found.append(Distro(key, f"Leap {version}", "Leap" + version.replace(".", ""), repos))
    return found


def fetch(repo: Repo, quiet: bool = False, refresh: bool = False) -> str:
    """Return a local, uncompressed copy of one repository's ARCHIVES file.

    Existing files are reused as-is.  With `refresh`, a HEAD request decides
    whether anything actually changed upstream — these indexes are 100+ MB and
    Leap's barely moves, so re-downloading them on a schedule would just burn
    somebody else's mirror bandwidth.
    """
    gz = repo.path + ".gz"
    meta_path = repo.path + ".meta.json"
    have_local = os.path.exists(repo.path) or os.path.exists(gz)

    if have_local and not refresh:
        return _ungzip(repo, gz, quiet)

    remote = None
    if have_local and refresh:
        try:
            remote = _remote_meta(repo.url)
        except (urllib.error.URLError, OSError) as err:
            print(f"warning: cannot check {repo.url} ({err}), using local copy",
                  file=sys.stderr)
            return _ungzip(repo, gz, quiet)
        local = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    local = json.load(fh)
            except (OSError, ValueError):
                local = {}
        if local and local == remote:
            if not quiet:
                print(f"unchanged upstream: {repo.path}", file=sys.stderr)
            return _ungzip(repo, gz, quiet)

    if not quiet:
        what = "refreshing" if have_local else "fetching"
        print(f"{what} {repo.url} -> {gz}", file=sys.stderr)
    tmp = gz + ".part"
    try:
        with urllib.request.urlopen(_request(repo.url), timeout=300) as resp, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
            if remote is None:
                remote = _meta(resp.headers)
    except (urllib.error.URLError, OSError) as err:
        # A mirror hiccup must not fail a scheduled rebuild when we already
        # have a usable copy; only a cold start has nothing to fall back on.
        if os.path.exists(tmp):
            os.unlink(tmp)
        if not have_local:
            raise
        print(f"warning: cannot download {repo.url} ({err}), using local copy",
              file=sys.stderr)
        return _ungzip(repo, gz, quiet)
    os.replace(tmp, gz)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(remote, fh)
    if os.path.exists(repo.path):
        os.unlink(repo.path)  # force the stale plain copy to be rebuilt
    return _ungzip(repo, gz, quiet)


def _ungzip(repo: Repo, gz: str, quiet: bool) -> str:
    if repo.keep_gz:
        return gz
    if os.path.exists(repo.path):
        return repo.path
    if not quiet:
        print(f"decompressing {gz} -> {repo.path}", file=sys.stderr)
    tmp = repo.path + ".part"
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out)
    os.replace(tmp, repo.path)
    return repo.path


def _scan(buf, repo_name: str, arches, pkgs: dict) -> None:
    """Collect every `Source RPM :` line in `buf`, which must start on a line.

    Anchoring the regex on the rare literal and only then walking back to the
    line start is what keeps this quick: a line-anchored `^\\./…` pattern finds
    exactly the same packages but takes 8.2 s where this takes 0.6 s, because the
    engine then has to try every one of the millions of lines.
    """
    for m in SRCRPM_RE.finditer(buf):
        # the binary package's arch is the first path component
        start = buf.rfind(b"\n", max(0, m.start() - 4096), m.start()) + 1
        line = buf[start : m.start()]
        if not line.startswith(b"./"):
            continue
        sep = line.find(b"/", 2)
        if sep < 0:
            continue
        arch = line[2:sep].decode("ascii", "replace")
        if arches is None:
            # A ports tree: take every architecture it ships, since taking
            # only x86_64 there would leave nothing at all.
            if arch in NON_ARCH_DIRS:
                continue
        elif arch not in arches:
            continue
        nvr = m.group(1).decode("utf-8", "replace")
        try:
            name, version, release = nvr.rsplit("-", 2)
        except ValueError:
            continue
        versions = pkgs.setdefault(name, {})
        key = f"{version}-{release}"
        pkg = versions.get(key)
        if pkg is None:
            pkg = versions[key] = Package(name, version, release)
        pkg.arches.add(arch)
        pkg.repos.add(repo_name)


def parse_archives(path: str, repo_name: str, pkgs: dict | None = None,
                   arches=ARCHES) -> dict:
    """Fill/extend a map of source package name -> {version-release: Package}.

    A `.gz` path is streamed rather than expanded: the ports indexes are read
    once as a lookaside and together they are ~9 GB uncompressed, which is not
    worth putting on disk for one pass.
    """
    if pkgs is None:
        pkgs = {}
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            tail = b""
            while True:
                buf = fh.read(_CHUNK)
                if not buf:
                    break
                # Hand _scan whole lines only, so its walk back to a line start
                # can never run off the front of the buffer.
                buf = tail + buf
                cut = buf.rfind(b"\n") + 1
                buf, tail = buf[:cut], buf[cut:]
                _scan(buf, repo_name, arches, pkgs)
            if tail:
                _scan(tail, repo_name, arches, pkgs)
        return pkgs
    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            _scan(mm, repo_name, arches, pkgs)
        finally:
            mm.close()
    return pkgs


def resolve_ports(spec: str) -> list:
    """Turn `--ports aarch64,s390x` into the list of trees to download."""
    if spec in ("all", ""):
        return list(PORTS_TREES)
    trees = []
    for want in spec.split(","):
        want = want.strip().lower()
        tree = PORTS_ALIASES.get(want, want)
        if tree not in PORTS_TREES:
            raise SystemExit(
                f"unknown ports tree {want!r}; known: "
                + ", ".join(sorted(set(PORTS_TREES) | set(PORTS_ALIASES))))
        if tree not in trees:  # armv7hl and aarch64 are one download
            trees.append(tree)
    return trees


def fetch_ports(trees: list, quiet: bool = False, refresh: bool = False) -> dict:
    """Source packages from Tumbleweed's non-x86_64 media.

    The result is a lookaside, not another distro: the caller merges only the
    names the x86_64/noarch tree does not already have.
    """
    pkgs = {}
    for tree in trees:
        for name in ("oss", "non-oss"):
            url = f"{PORTS_BASE}/{tree}/tumbleweed/repo/{name}/ARCHIVES.gz"
            path = f"ARCHIVES_ports_{tree}"
            if name != "oss":
                path += "_nonoss"
            repo = Repo(name, path, url, keep_gz=True)
            gz = path + ".gz"
            if not os.path.exists(gz) and not _exists(url):
                # riscv publishes no non-oss at all; a port may drop one too.
                if not quiet:
                    print(f"no ports/{tree} {name} repository", file=sys.stderr)
                continue
            try:
                local = fetch(repo, quiet, refresh)
            except (urllib.error.URLError, OSError) as err:
                # One unreachable port must not cost the whole lookaside.
                print(f"warning: skipping ports/{tree} {name} ({err})",
                      file=sys.stderr)
                continue
            before = len(pkgs)
            parse_archives(local, f"ports/{tree} {name}", pkgs, arches=None)
            if not quiet:
                print(f"ports/{tree} {name}: {len(pkgs) - before} more source "
                      f"packages ({len(pkgs)} total)", file=sys.stderr)
    return pkgs


def newest(versions: dict, with_release: bool) -> Package:
    best = None
    for pkg in versions.values():
        if best is None or evr_cmp(pkg, best, with_release) > 0:
            best = pkg
    return best


def fetch_maintainers(quiet: bool = False) -> dict:
    """Map source package name -> sorted list of maintainers.

    Returns an empty map (with a note on stderr) when the data cannot be
    obtained, so a missing network never breaks the version diff.
    """
    local = MAINTAINERS_FILE
    if not os.path.exists(local):
        local = os.path.join(MAINTAINERS_CLONE, MAINTAINERS_FILE)
    if not os.path.exists(local):
        if not quiet:
            print(f"cloning {MAINTAINERS_REPO} ({MAINTAINERS_BRANCH})…", file=sys.stderr)
        cmd = [
            "git", "clone", "--quiet", "--depth", "1",
            "--branch", MAINTAINERS_BRANCH, "--single-branch",
            "--filter=blob:none", "--sparse", MAINTAINERS_REPO, MAINTAINERS_CLONE,
        ]
        try:
            subprocess.run(cmd, check=True, timeout=300)
            subprocess.run(
                ["git", "-C", MAINTAINERS_CLONE, "sparse-checkout", "set",
                 "--no-cone", "/" + MAINTAINERS_FILE],
                check=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as err:
            print(f"warning: no maintainer data ({err})", file=sys.stderr)
            return {}
        local = os.path.join(MAINTAINERS_CLONE, MAINTAINERS_FILE)

    try:
        with open(local, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as err:
        print(f"warning: cannot read {local} ({err})", file=sys.stderr)
        return {}

    out = {}
    for name, entry in doc.get("packages", {}).items():
        who = sorted(entry.get("users", [])) + [
            g + " (group)" for g in sorted(entry.get("groups", []))
        ]
        if who:
            out[name] = who
    if not quiet:
        print(f"{len(out)} packages with maintainers from {local}", file=sys.stderr)
    return out


def _repology_page(name: str) -> list:
    """One page of outdated projects, starting after `name`."""
    url = f"{REPOLOGY_API}{urllib.parse.quote(name)}/" if name else REPOLOGY_API
    url += f"?inrepo={REPOLOGY_REPO}&outdated=1"
    with urllib.request.urlopen(_request(url), timeout=120) as resp:
        return sorted(json.load(resp).items())


def fetch_repology(quiet: bool = False, max_age_days: float = 7.0) -> dict:
    """Map source package name -> newest version Repology knows of.

    Cached in `repology_newest.json` and only refetched once the cache is older
    than `max_age_days`, so a nightly rebuild does not pull ~120 MB off
    repology.org every morning for a table that moves by a few packages a day.
    Pass 0 to force a refetch.  Failures degrade to the cache, then to nothing.
    """
    cached = None
    if os.path.exists(REPOLOGY_CACHE):
        try:
            with open(REPOLOGY_CACHE, encoding="utf-8") as fh:
                cached = json.load(fh)
        except (OSError, ValueError):
            cached = None
    if cached:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(cached["fetched"])).total_seconds()
        if age < max_age_days * 86400:
            if not quiet:
                print(f"{len(cached['versions'])} newest versions from "
                      f"{REPOLOGY_CACHE} ({age / 3600:.0f}h old)", file=sys.stderr)
            return cached["versions"]

    versions: dict = {}
    name = ""
    try:
        for page in range(REPOLOGY_MAX_PAGES):
            if page:
                time.sleep(REPOLOGY_DELAY)
            if not quiet:
                print(f"repology page {page + 1} (after {name or 'start'})…",
                      file=sys.stderr)
            projects = _repology_page(name)
            for _project, entries in projects:
                # The newest version is whatever Repology flagged as such; the
                # names to hang it on are Tumbleweed's own srcnames, so no
                # project-name mapping of our own is needed.
                newest_v = [e["version"] for e in entries if e.get("status") == "newest"]
                if not newest_v:
                    continue
                for e in entries:
                    if e.get("repo") == REPOLOGY_REPO and e.get("srcname"):
                        versions[e["srcname"]] = newest_v[0]
            if len(projects) < REPOLOGY_PAGE:
                break
            name = projects[-1][0]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as err:
        if cached:
            print(f"warning: repology fetch failed ({err}), using stale cache",
                  file=sys.stderr)
            return cached["versions"]
        print(f"warning: no repology data ({err})", file=sys.stderr)
        return {}

    with open(REPOLOGY_CACHE, "w", encoding="utf-8") as fh:
        json.dump({"fetched": datetime.now(timezone.utc).isoformat(),
                   "repo": REPOLOGY_REPO, "versions": versions}, fh)
    if not quiet:
        print(f"{len(versions)} packages outdated against repology", file=sys.stderr)
    return versions


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------


def compare(left: dict, right: dict, extras: list, with_release: bool,
            st: dict, maint: dict, upstream: dict = None) -> list:
    """Diff left against right, annotated with the versions of `extras`.

    `extras` is a list of (Distro, packages) pairs.  Their versions are shown
    but not compared; they do widen the set of rows, so a package only an older
    Leap still ships is not lost.  Such a row is `Only-in-<right>` like any
    other package Tumbleweed does not have — which Leap it survived in is what
    the version columns are for, and one status per side beats one per release.

    `upstream` maps a package to the newest version Repology knows of.  Where
    it says nothing but `left` has the package, `left`'s own version is that
    newest one — only the projects `left` is outdated on are fetched.
    """
    rows = []
    names = set(left) | set(right)
    for _distro, pkgs in extras:
        names |= set(pkgs)
    for name in sorted(names):
        lv = left.get(name)
        rv = right.get(name)
        lp = newest(lv, with_release) if lv else None
        rp = newest(rv, with_release) if rv else None
        up = upstream.get(name) if upstream is not None else None
        if lp and rp:
            rc = evr_cmp(rp, lp, with_release)  # compared distro vs reference
            status = st["same"] if rc == 0 else (st["newer"] if rc > 0 else st["older"])
            # Level with each other and with the newest anyone ships.  `up` is
            # only set for the projects Repology calls outdated, so "nothing
            # fetched" is itself the good answer.
            if rc == 0 and "perfect" in st and not up:
                status = st["perfect"]
        elif lp:
            status = st["only_left"]
        else:
            status = st["only_right"]
        row = {
            "name": name,
            "status": status,
            "left": lp.vr if lp else "",
            "right": rp.vr if rp else "",
            "left_version": lp.version if lp else "",
            "right_version": rp.version if rp else "",
            "left_all": sorted(lv) if lv else [],
            "right_all": sorted(rv) if rv else [],
            "left_arches": sorted(lp.arches) if lp else [],
            "right_arches": sorted(rp.arches) if rp else [],
            "left_repos": sorted(lp.repos) if lp else [],
            "right_repos": sorted(rp.repos) if rp else [],
            "maintainers": maint.get(name, []),
        }
        # A name the x86_64/noarch media do not carry at all, found only in a
        # ports tree.  Worth flagging: "Tumbleweed has it" then means something
        # narrower than usual, and for an s390x or riscv tool that is the point.
        if lp and all(r.startswith("ports/") for r in lp.repos):
            row["ports"] = sorted(lp.arches)
        if upstream is not None:
            row["up"] = up or (lp.version if lp else "")
        # x0, x1, … are the flat columns the text formats and the page print;
        # `extras` keeps the same detail the left/right side gets, for json.
        row["extras"] = []
        column = {"left": lp, "right": rp}
        for i, (distro, pkgs) in enumerate(extras):
            ev = pkgs.get(name)
            ep = newest(ev, with_release) if ev else None
            row[f"x{i}"] = ep.vr if ep else ""
            column[f"x{i}"] = ep
            row["extras"].append(
                {
                    "key": distro.key,
                    "version": ep.version if ep else "",
                    "version_release": ep.vr if ep else "",
                    "all": sorted(ev) if ev else [],
                    "arches": sorted(ep.arches) if ep else [],
                    "repos": sorted(ep.repos) if ep else [],
                }
            )

        # Say so wherever a column was not read at face value.  Only left and
        # right feed the verdict, but marking the extras too keeps the row
        # honest: the same string must not be flagged in one column and shown
        # plain in the next.
        norm = {k: p.cmp_version for k, p in column.items() if p and p.cmp_rules}
        if norm:
            rules = []
            for pkg in column.values():
                if pkg:
                    rules += [r for r in pkg.cmp_rules if r not in rules]
            row["normalized"] = {**norm, "rules": rules}
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def emit_table(rows, vcols, out, maintainers=False) -> None:
    cols = [
        ("status", "STATUS", 16),
        ("name", "SOURCE-PACKAGE", 48),
    ] + [(key, label.upper(), 28) for key, label in vcols]
    if maintainers:
        cols.append(("maint", "MAINTAINERS", 40))
    # Cap the padding so a handful of git-hash versions don't stretch every row;
    # over-long values overflow their column rather than being truncated.
    widths = [
        min(cap, max(len(head), max((len(str(r[key])) for r in rows), default=0)))
        for key, head, cap in cols
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths).rstrip()
    print(fmt.format(*(head for _, head, _cap in cols)), file=out)
    print("  ".join("-" * w for w in widths), file=out)
    for r in rows:
        print(fmt.format(*(str(r[key]) for key, _h, _cap in cols)).rstrip(), file=out)


def with_maint_column(rows) -> list:
    """Add a flattened `maint` field used by the text based formats."""
    for r in rows:
        r["maint"] = ",".join(r["maintainers"])
    return rows


def emit_markdown(rows, vcols, out, maintainers=False) -> None:
    extra_h, extra_s = (" Maintainers |", " --- |") if maintainers else ("", "")
    heads = "".join(f" {label} |" for _key, label in vcols)
    print(f"| Status | Source package |{heads}{extra_h}", file=out)
    print("| --- | --- |" + " --- |" * len(vcols) + extra_s, file=out)
    for r in rows:
        extra = f" {r['maint'] or '—'} |" if maintainers else ""
        cells = "".join(f" {r[key] or '—'} |" for key, _label in vcols)
        print(f"| {r['status']} | {r['name']} |{cells}{extra}", file=out)


def emit_csv(rows, vcols, out, maintainers=False) -> None:
    w = csv.writer(out)
    head = ["status", "source_package"] + [label for _key, label in vcols]
    w.writerow(head + ["maintainers"] if maintainers else head)
    for r in rows:
        row = [r["status"], r["name"]] + [r[key] for key, _label in vcols]
        if maintainers:
            row.append(" ".join(r["maintainers"]))
        w.writerow(row)


def _distro_meta(d: Distro) -> dict:
    return {
        "key": d.key,
        "label": d.label,
        "repos": [{"name": r.name, "url": r.url} for r in d.repos],
    }


def emit_json(rows, left, right, extras, counts, out, totals) -> None:
    json.dump(
        {
            "left": _distro_meta(left),
            "right": _distro_meta(right),
            "extras": [_distro_meta(d) for d in extras],
            "totals": {
                "source_packages": totals["total"],
                left.key: totals["left"],
                right.key: totals["right"],
                "in_both": totals["both"],
                **{d.key: n for d, n in zip(extras, totals["extras"])},
                **(
                    {"upstream_outdated": totals["upstream_outdated"]}
                    if "upstream_outdated" in totals
                    else {}
                ),
            },
            **(
                {
                    "upstream": {
                        "source": "repology",
                        "repo": REPOLOGY_REPO,
                        "url": f"{REPOLOGY_URL}/repository/{REPOLOGY_REPO}",
                        "note": "newest version Repology sees in any distribution, "
                                "not a release feed of the project itself",
                    }
                }
                if "upstream_outdated" in totals
                else {}
            ),
            "summary": counts,
            "packages": rows,
        },
        out,
        indent=2,
    )
    out.write("\n")


# The page itself lives in templates/page.html, next to this script, so the
# markup, CSS and JS can be edited (and syntax-highlighted) as what they are.
# Placeholders are __UPPERCASE__ and every one of them must be filled.
PAGE_TEMPLATE = Path(__file__).resolve().parent / "templates" / "page.html"
_PLACEHOLDER_RE = re.compile(r"__([A-Z_]+)__")


def render_page(values: dict) -> str:
    """Fill the page template in one pass, so no value can be substituted into.

    A missing placeholder is a bug in this file rather than something to paper
    over at build time, so it stops the run instead of shipping a page with
    __DATA__ printed in it.
    """
    try:
        template = PAGE_TEMPLATE.read_text(encoding="utf-8")
    except OSError as err:
        raise SystemExit(f"cannot read the page template: {err}") from err

    def fill(m):
        try:
            return values[m.group(1)]
        except KeyError:
            raise SystemExit(
                f"page template wants {m.group(0)}, which is not set"
            ) from None

    return _PLACEHOLDER_RE.sub(fill, template)


def _script_json(value) -> str:
    """JSON safe to inline in a <script>.

    A `</` in a string would end the script block early and `&` is a live
    character in HTML, so both are written as their JSON escapes.
    """
    return (
        json.dumps(value)
        .replace("</", "<\\/")
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
    )


def _status_cls(status: str, st: dict) -> str:
    """CSS class for a status — mirrors cls() in the page's own script."""
    if status.startswith("Only"):
        return "s-OnlyL" if status == st["only_left"] else "s-OnlyR"
    return "s-" + status.split("-")[0]


def _family(labels) -> str:
    """The words the compared distros' labels share, e.g. Leap 16.1 + 16.0 -> Leap."""
    words = [label.split() for label in labels]
    common = []
    for parts in zip(*words):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return " ".join(common) or " / ".join(labels)


def emit_html(rows, left, right, extras, vcols, counts, out, totals, st) -> None:
    right_side = _family([right.label] + [d.label for d in extras])
    title = f"{left.label} vs {right_side} — source package versions"
    sub = (
        f"<strong>{totals['total']:,} source packages</strong> · "
        + " · ".join(
            f"{html.escape(d.label)} {n:,}"
            for d, n in zip(
                (left, right, *extras),
                (totals["left"], totals["right"], *totals["extras"]),
            )
        )
        + f" · {totals['both']:,} in both {html.escape(left.label)} and "
        f"{html.escape(right.label)} · oss + non-oss · x86_64 + noarch"
    )
    if "ports" in totals:
        sub += (
            f" · {totals['ports']:,} only in {html.escape(left.label)} ports"
        )
    if "upstream_outdated" in totals:
        sub += (
            f" · {totals['upstream_outdated']:,} behind upstream in "
            f"{html.escape(left.label)}"
        )
    # One sentence per status, written once and used three times over: as the
    # legend below the counts, as the tooltip on each count, and as the tooltip
    # on every status cell in the table.  Full sentences, because the legend
    # reads them as prose — a tooltip can get away with a fragment, a legend
    # cannot.
    hints = {
        st["same"]: (
            f"{left.label} and {right.label} ship the same upstream version. "
            "Only the version is compared, so the rpm release may still differ."
        ),
        st["older"]: (
            f"{right.label} ships an older upstream version than {left.label}. "
            "This is the ordinary case for a package that moved on after "
            f"{right.label} branched, and the list worth working through."
        ),
        st["newer"]: (
            f"{right.label} ships a newer upstream version than {left.label}, "
            "which is not supposed to happen"
            + (" — changes are expected to reach Factory first"
               if left.key == "tumbleweed" else "")
            + ". Usually a package updated straight in "
            f"{right.label}, or one whose version scheme confuses the "
            "comparison; either way it is worth a look."
        ),
        st["only_left"]: (
            f"{left.label} ships this source package and {right.label} does not."
        ),
        st["only_right"]: (
            f"{right.label} ships this source package and {left.label} does not."
            # A row can land here on the strength of an extra column alone, so
            # promising that `right` has it would be a lie for those rows.
            + (" Some of these are shipped only by an older release in the "
               "columns further right." if extras else "")
        ),
    }
    if "perfect" in st:
        hints[st["perfect"]] = (
            f"Marked ✦. {left.label} and {right.label} are level with each "
            "other, and Repology knows of no distribution anywhere shipping "
            "anything newer — as up to date as a package gets."
        )
    breakdown = " · ".join(
        f'<span class="{_status_cls(s, st)}" title="{html.escape(hints[s])}">'
        f"{html.escape(s)}</span> {v:,}"
        for s, v in counts.items()
    )
    options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in counts
    )
    # Same order and same colours as the breakdown line above it, so the legend
    # reads as an expansion of the counts rather than a second list of statuses.
    legend = "".join(
        f'<dt class="status {_status_cls(s, st)}">{html.escape(s)}</dt>'
        f"<dd>{html.escape(hints[s])}</dd>"
        for s in counts
    )
    slim = [
        {
            "name": r["name"],
            "status": r["status"],
            "maint": ", ".join(r["maintainers"]),
            **{key: r[key] for key, _label in vcols},
            # `n` only rides along where it exists — 1k of 17.5k rows.
            **({"n": {k: v for k, v in r["normalized"].items() if k != "rules"}}
               if r.get("normalized") else {}),
            **({"p": ", ".join(r["ports"])} if r.get("ports") else {}),
        }
        for r in rows
    ]
    # Version columns share what is left after the fixed status/maintainer
    # ones; upstream needs less, as it carries no rpm release.
    distro_w = {2: 15, 3: 13}.get(len(vcols), 11)
    colgroup = (
        '<col style="width:8.5rem"><col>'
        + "".join(
            f'<col style="width:{9 if key == "up" else distro_w}rem">'
            for key, _label in vcols
        )
        + '<col style="width:13rem">'
    )
    thead = (
        '<th data-k="status">Status</th><th data-k="name">Source package</th>'
        + "".join(
            f'<th data-k="{key}">{html.escape(label)}</th>' for key, label in vcols
        )
        + '<th data-k="maint">Maintainers</th>'
    )
    sources = "".join(
        f'\n    <li>{html.escape(d.label)} {html.escape(r.name)}: '
        f'<a href="{html.escape(r.url)}">{html.escape(r.url)}</a></li>'
        for d in (left, right, *extras)
        for r in d.repos
    ) + (
        f'\n    <li>Maintainers: <a href="{html.escape(MAINTAINERS_REPO)}">'
        f"{html.escape(MAINTAINERS_REPO)}</a> ({html.escape(MAINTAINERS_BRANCH)})</li>"
    )
    if any(key == "up" for key, _label in vcols):
        sources += (
            f'\n    <li>Upstream: <a href="{REPOLOGY_URL}/repository/{REPOLOGY_REPO}">'
            f"Repology</a> — the newest version it sees in <em>any</em> distribution, "
            "not a release feed of the project itself</li>"
        )
    out.write(render_page({
        "TITLE": html.escape(title),
        "SUB": sub,
        "BREAKDOWN": breakdown,
        "LEGEND": legend,
        "COLGROUP": colgroup,
        "THEAD": thead,
        "VCOLS": json.dumps([key for key, _label in vcols]),
        "PROJECT": html.escape(PROJECT_URL),
        "GENERATED": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "SOURCES": sources,
        "OPTIONS": options,
        "ONLY_LEFT": html.escape(st["only_left"]),
        "PORTSNOTE": (
            f'A <sup class="ports">ports</sup> badge means {html.escape(left.label)} '
            "only builds that package for another architecture, so it is absent "
            "from the x86_64/noarch media the rest of the table is built from."
            if "ports" in totals else ""
        ),
        "HINTS": _script_json(hints),
        "DATA": _script_json(slim),
    }))


# --------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Diff source package versions between openSUSE distributions.",
        epilog="Grep the STATUS column, e.g. `osdiff.py | grep Older`.",
    )
    p.add_argument("--left", default="tumbleweed", help="reference distro (default: tumbleweed)")
    p.add_argument("--right", default="leap161", help="compared distro (default: leap161)")
    p.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="DISTRO",
        help="show this distro as an additional version column, right of --right "
        "(repeatable; takes no part in the status)",
    )
    p.add_argument(
        "--discover",
        action="store_true",
        help="probe download.opensuse.org for Leap releases this script does not "
        "know yet and add each one that is published as an extra column",
    )
    p.add_argument(
        "--repology",
        action="store_true",
        help="add an Upstream column with the newest version repology.org knows "
        f"of (cached in {REPOLOGY_CACHE})",
    )
    p.add_argument(
        "--repology-max-age",
        type=float,
        default=7.0,
        metavar="DAYS",
        help="refetch the repology cache once it is older than this (default: 7; "
        "0 forces a refetch)",
    )
    p.add_argument(
        "--format",
        choices=("table", "md", "csv", "json", "html"),
        default="table",
        help="output format (default: table)",
    )
    p.add_argument(
        "--only",
        action="append",
        metavar="STATUS",
        help="only show statuses containing this text, e.g. --only older "
        "(case insensitive, repeatable)",
    )
    p.add_argument("--grep", metavar="REGEX", help="only source packages matching REGEX")
    p.add_argument(
        "--maintainers",
        action="store_true",
        help="show a maintainers column in table/md/csv output (always in json/html)",
    )
    p.add_argument(
        "--maintainer",
        metavar="NAME",
        help="only packages maintained by NAME (implies --maintainers)",
    )
    p.add_argument(
        "--no-maintainers",
        action="store_true",
        help="do not load maintainer data at all (skips the PackageHub clone)",
    )
    p.add_argument(
        "--with-release",
        action="store_true",
        help="compare release too (by default only the upstream version is compared, "
        "since Leap and Tumbleweed use unrelated release schemes)",
    )
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of stdout")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="check upstream for newer ARCHIVES indexes (HEAD request first, "
        "so unchanged files are not re-downloaded)",
    )
    p.add_argument(
        "--ports",
        nargs="?",
        const="all",
        metavar="TREES",
        help="also look up packages missing from the reference distro's "
        "x86_64/noarch media in its ports trees (default: all of "
        + ",".join(PORTS_TREES) + "; or a comma-separated subset, e.g. "
        "--ports s390x,aarch64).  Costs ~510 MB of downloads",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="no summary on stderr")
    args = p.parse_args(argv)

    for key in (args.left, args.right, *args.extra):
        if key not in DISTROS:
            p.error(f"unknown distro {key!r}; known: {', '.join(DISTROS)}")
    if args.left == args.right:
        p.error("--left and --right must differ")
    left, right = DISTROS[args.left], DISTROS[args.right]
    ports_trees = []
    if args.ports:
        # PORTS_BASE hardcodes Tumbleweed's layout; Leap's ports live elsewhere
        # and are deliberately not read (see the README).
        if args.left != "tumbleweed":
            p.error("--ports only applies to a Tumbleweed --left")
        # Resolved here rather than at use, so a typo fails now and not after
        # four indexes have been parsed.
        ports_trees = resolve_ports(args.ports)

    extras, seen = [], {args.left, args.right}
    for key in args.extra:
        if key not in seen:
            seen.add(key)
            extras.append(DISTROS[key])
    if args.discover:
        extras += discover_leaps(args.quiet)
    # Newest first: an older Leap is the least interesting column, so it ends
    # up furthest from the versions the diff is actually about.  Sorting on the
    # numbers rather than the label keeps a 16.10 above 16.9.
    extras.sort(
        key=lambda d: tuple(int(n) for n in re.findall(r"\d+", d.label)) or (0,),
        reverse=True,
    )
    # Newest first, left to right: upstream, then the reference distro, then
    # the compared one, then any older release.
    vcols = [("left", left.label), ("right", right.label)]
    vcols += [(f"x{i}", d.label) for i, d in enumerate(extras)]

    if not HAVE_RPM and not args.quiet:
        print("note: python3-rpm not found, using built-in rpmvercmp", file=sys.stderr)

    if args.maintainer:
        args.maintainers = True
    maint = {} if args.no_maintainers else fetch_maintainers(args.quiet)
    upstream = (
        fetch_repology(args.quiet, args.repology_max_age) if args.repology else None
    )
    if upstream:
        vcols.insert(0, ("up", "Upstream"))
    elif upstream is not None:
        # An empty result would fill the column with Tumbleweed's own versions,
        # which is worse than not having the column at all.
        print("warning: no upstream versions, dropping the Upstream column",
              file=sys.stderr)
        upstream = None
    # Only now is it known whether Perfection is on the menu.
    st = statuses(left, right, upstream is not None)

    data = {}
    for distro in (left, right, *extras):
        pkgs: dict = {}
        for repo in distro.repos:
            path = fetch(repo, args.quiet, args.refresh)
            if not args.quiet:
                print(f"parsing {path} ({distro.label} {repo.name})…", file=sys.stderr)
            parse_archives(path, repo.name, pkgs)
        data[distro.key] = pkgs

    if args.ports:
        # Strictly a lookaside for the reference distro: a name the x86_64 media
        # already have keeps the version they ship, so nothing that is on the
        # page today can be moved by a port.  What it adds is the two kinds of
        # package the x86_64 view gets wrong — the arch-specific ones (yast2-s390,
        # u-boot, …) that never appear as Only-in-Tumbleweed rows at all, and the
        # ones that read as Only-in-Leap purely because Leap builds them on an
        # architecture we were not looking at.
        ports = fetch_ports(ports_trees, args.quiet, args.refresh)
        added = {n: v for n, v in ports.items() if n not in data[left.key]}
        data[left.key].update(added)
        if not args.quiet:
            print(f"ports lookaside: {len(added)} source packages "
                  f"{left.label} has nowhere else", file=sys.stderr)

    rows = compare(
        data[left.key],
        data[right.key],
        [(d, data[d.key]) for d in extras],
        args.with_release,
        st,
        maint,
        upstream,
    )

    counts = dict.fromkeys(st.values(), 0)
    for r in rows:
        counts[r["status"]] += 1

    # Totals are taken before --only/--grep/--maintainer narrow the view, so a
    # filtered page still reports the size of the whole data set.
    totals = {
        "total": len(rows),
        "left": sum(1 for r in rows if r["left_version"]),
        "right": sum(1 for r in rows if r["right_version"]),
        "both": sum(1 for r in rows if r["left_version"] and r["right_version"]),
        "extras": [
            sum(1 for r in rows if r["extras"][i]["version"]) for i in range(len(extras))
        ],
    }
    if args.ports:
        totals["ports"] = sum(1 for r in rows if r.get("ports"))
    if upstream is not None:
        totals["upstream_outdated"] = sum(
            1 for r in rows
            if r["left_version"] and r["up"] and r["up"] != r["left_version"]
        )

    if args.only:
        wanted = [o.lower() for o in args.only]
        rows = [r for r in rows if any(w in r["status"].lower() for w in wanted)]
    if args.grep:
        rx = re.compile(args.grep)
        rows = [r for r in rows if rx.search(r["name"])]
    if args.maintainer:
        who = args.maintainer.lower()
        rows = [r for r in rows if any(who in m.lower() for m in r["maintainers"])]
    with_maint_column(rows)

    # Closed in the finally below; a `with` here would have to wrap the whole
    # emit block twice over, once per branch.
    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout  # noqa: SIM115
    try:
        if args.format == "table":
            emit_table(rows, vcols, out, args.maintainers)
        elif args.format == "md":
            emit_markdown(rows, vcols, out, args.maintainers)
        elif args.format == "csv":
            emit_csv(rows, vcols, out, args.maintainers)
        elif args.format == "json":
            emit_json(rows, left, right, extras, counts, out, totals)
        elif args.format == "html":
            emit_html(rows, left, right, extras, vcols, counts, out, totals, st)
    finally:
        if args.output:
            out.close()

    if not args.quiet:
        per_distro = ", ".join(
            f"{d.label} {n}"
            for d, n in zip(
                (left, right, *extras),
                (totals["left"], totals["right"], *totals["extras"]),
            )
        )
        behind = (
            f", {totals['upstream_outdated']} behind upstream"
            if "upstream_outdated" in totals
            else ""
        )
        if "ports" in totals:
            behind = f", {totals['ports']} only in ports" + behind
        print(
            f"\n{totals['total']} source packages "
            f"({per_distro}, {totals['both']} in both{behind}): "
            + ", ".join(f"{v} {k}" for k, v in counts.items()),
            file=sys.stderr,
        )
        norm = collections.Counter(
            rule for r in rows for rule in r.get("normalized", {}).get("rules", ())
        )
        if norm:
            print(
                f"{sum(1 for r in rows if r.get('normalized'))} rows carry a version "
                "rewritten for comparison: "
                + ", ".join(f"{v} {k}" for k, v in norm.most_common()),
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # e.g. `osdiff.py | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
