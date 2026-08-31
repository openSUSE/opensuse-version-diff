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
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Only x86_64 and noarch are compared.  Tumbleweed's ARCHIVES index covers no
# other arch, so pulling in Leap's aarch64/ppc64le/s390x packages would only
# invent packages that look Leap-only.
ARCHES = frozenset({"x86_64", "noarch"})

# Status values.  Keep the first word greppable and stable: whatever distros are
# compared, `grep Older` always means "the compared distro is behind".
def statuses(left: "Distro", right: "Distro") -> dict:
    return {
        "older": f"Older-in-{right.tag}",
        "newer": f"Newer-in-{right.tag}",
        "same": "Same",
        "only_left": f"Only-in-{left.tag}",
        "only_right": f"Only-in-{right.tag}",
    }


@dataclass
class Repo:
    name: str  # oss / non-oss
    path: str  # local, uncompressed copy
    url: str


@dataclass
class Distro:
    key: str
    label: str
    tag: str
    repos: list


def _distro(key, label, tag, base, oss_path) -> Distro:
    """Both repositories of one distribution; non-oss reuses the oss naming."""
    return Distro(
        key,
        label,
        tag,
        [
            Repo("oss", oss_path, f"{base}/oss/ARCHIVES.gz"),
            Repo("non-oss", oss_path + "_nonoss", f"{base}/non-oss/ARCHIVES.gz"),
        ],
    )


DISTROS = {
    "tumbleweed": _distro(
        "tumbleweed",
        "Tumbleweed",
        "TW",
        "https://download.opensuse.org/tumbleweed/repo",
        "ARCHIVES_TW",
    ),
    "leap161": _distro(
        "leap161",
        "Leap 16.1",
        "Leap",
        "https://download.opensuse.org/distribution/leap/16.1/repo",
        "ARCHIVES_161",
    ),
    "leap160": _distro(
        "leap160",
        "Leap 16.0",
        "Leap160",
        "https://download.opensuse.org/distribution/leap/16.0/repo",
        "ARCHIVES_160",
    ),
}


# Sent on every HTTP request so download.opensuse.org admins can attribute the
# traffic (and reach the project) if it ever becomes a nuisance.
USER_AGENT = "osdiff/1.0 (+https://github.com/openSUSE/opensuse-version-diff)"

# Maintainership comes from the PackageHub product repo.  Cloning it over git
# avoids the bot check that guards the src.opensuse.org web interface.
MAINTAINERS_REPO = "https://src.opensuse.org/products/PackageHub.git"
MAINTAINERS_BRANCH = "leap-16.1"
MAINTAINERS_FILE = "_maintainership.json"
MAINTAINERS_CLONE = ".packagehub"


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


def evr_cmp(a: "Package", b: "Package", with_release: bool) -> int:
    """Compare two packages, optionally including the release."""
    ra = a.release if with_release else ""
    rb = b.release if with_release else ""
    return _label_compare((None, a.version, ra), (None, b.version, rb))


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

    @property
    def vr(self) -> str:
        return f"{self.version}-{self.release}"


# ./x86_64/libbz2-1-1.0.8-6.1.x86_64.rpm:    Source RPM  : bzip2-1.0.8-6.1.src.rpm
SRCRPM_RE = re.compile(rb":    Source RPM  : ([^\n]+)\.src\.rpm\n")


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    """Identify ourselves, so mirror admins can tell what this traffic is."""
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def _remote_meta(url: str) -> dict:
    """Last-Modified/size of the remote file, via a HEAD request."""
    with urllib.request.urlopen(_request(url, "HEAD"), timeout=60) as resp:
        return {
            "last_modified": resp.headers.get("Last-Modified"),
            "size": resp.headers.get("Content-Length"),
        }


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
    with urllib.request.urlopen(_request(repo.url)) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
        if remote is None:
            remote = {
                "last_modified": resp.headers.get("Last-Modified"),
                "size": resp.headers.get("Content-Length"),
            }
    os.replace(tmp, gz)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(remote, fh)
    if os.path.exists(repo.path):
        os.unlink(repo.path)  # force the stale plain copy to be rebuilt
    return _ungzip(repo, gz, quiet)


def _ungzip(repo: Repo, gz: str, quiet: bool) -> str:
    if os.path.exists(repo.path):
        return repo.path
    if not quiet:
        print(f"decompressing {gz} -> {repo.path}", file=sys.stderr)
    tmp = repo.path + ".part"
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out)
    os.replace(tmp, repo.path)
    return repo.path


def parse_archives(path: str, repo_name: str, pkgs: dict | None = None) -> dict:
    """Fill/extend a map of source package name -> {version-release: Package}."""
    if pkgs is None:
        pkgs = {}
    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for m in SRCRPM_RE.finditer(mm):
                # the binary package's arch is the first path component
                start = mm.rfind(b"\n", max(0, m.start() - 4096), m.start()) + 1
                line = mm[start : m.start()]
                if not line.startswith(b"./"):
                    continue
                sep = line.find(b"/", 2)
                if sep < 0:
                    continue
                arch = line[2:sep].decode("ascii", "replace")
                if arch not in ARCHES:
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
        finally:
            mm.close()
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


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------


def compare(left: dict, right: dict, with_release: bool, st: dict, maint: dict) -> list:
    rows = []
    for name in sorted(set(left) | set(right)):
        lv = left.get(name)
        rv = right.get(name)
        lp = newest(lv, with_release) if lv else None
        rp = newest(rv, with_release) if rv else None
        if lp and rp:
            rc = evr_cmp(rp, lp, with_release)  # compared distro vs reference
            status = st["same"] if rc == 0 else (st["newer"] if rc > 0 else st["older"])
        elif lp:
            status = st["only_left"]
        else:
            status = st["only_right"]
        rows.append(
            {
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
        )
    return rows


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def emit_table(rows, left_label, right_label, out, maintainers=False) -> None:
    cols = [
        ("status", "STATUS", 14),
        ("name", "SOURCE-PACKAGE", 48),
        ("left", left_label.upper(), 28),
        ("right", right_label.upper(), 28),
    ]
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


def emit_markdown(rows, left_label, right_label, out, maintainers=False) -> None:
    extra_h, extra_s = (" Maintainers |", " --- |") if maintainers else ("", "")
    print(f"| Status | Source package | {left_label} | {right_label} |{extra_h}", file=out)
    print(f"| --- | --- | --- | --- |{extra_s}", file=out)
    for r in rows:
        extra = f" {r['maint'] or '—'} |" if maintainers else ""
        print(
            f"| {r['status']} | {r['name']} | {r['left'] or '—'} | {r['right'] or '—'} |{extra}",
            file=out,
        )


def emit_csv(rows, left_label, right_label, out, maintainers=False) -> None:
    w = csv.writer(out)
    head = ["status", "source_package", left_label, right_label]
    w.writerow(head + ["maintainers"] if maintainers else head)
    for r in rows:
        row = [r["status"], r["name"], r["left"], r["right"]]
        w.writerow(row + [" ".join(r["maintainers"])] if maintainers else row)


def _distro_meta(d: Distro) -> dict:
    return {
        "key": d.key,
        "label": d.label,
        "repos": [{"name": r.name, "url": r.url} for r in d.repos],
    }


def emit_json(rows, left, right, counts, out) -> None:
    json.dump(
        {
            "left": _distro_meta(left),
            "right": _distro_meta(right),
            "summary": counts,
            "packages": rows,
        },
        out,
        indent=2,
    )
    out.write("\n")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1c1c1c; --muted: #6b6b6b; --line: #e2e2e2;
    --older: #b3261e; --newer: #1f6f43; --same: #6b6b6b; --only: #7a4b00;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #17181a; --fg: #e8e8e8; --muted: #9a9a9a; --line: #303235;
            --older: #ff8a80; --newer: #7ddba3; --same: #9a9a9a; --only: #f0b866; }
  }
  body { background: var(--bg); color: var(--fg); margin: 0 auto; max-width: 1400px;
         padding: 2rem 1.5rem 4rem; font: 14px/1.5 system-ui, sans-serif; }
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  p.sub { color: var(--muted); margin: 0 0 1.5rem; }
  .bar { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1rem; position: sticky;
         top: 0; background: var(--bg); padding: .75rem 0; border-bottom: 1px solid var(--line); }
  input, select { font: inherit; padding: .4rem .6rem; border: 1px solid var(--line);
                  border-radius: 6px; background: var(--bg); color: var(--fg); }
  input { flex: 1 1 16rem; }
  table { border-collapse: collapse; width: 100%; table-layout: fixed; }
  th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  th { cursor: pointer; user-select: none; white-space: nowrap; font-size: .8rem;
       letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
  td.v { font-family: ui-monospace, monospace; white-space: nowrap; overflow: hidden;
         text-overflow: ellipsis; }
  td.m { color: var(--muted); font-size: .85em; max-width: 18rem; overflow: hidden;
         text-overflow: ellipsis; white-space: nowrap; }
  .status { font-weight: 600; white-space: nowrap; }
  .s-Older { color: var(--older); }
  .s-Newer { color: var(--newer); }
  .s-Same { color: var(--same); }
  .s-Only { color: var(--only); }
  #count { color: var(--muted); margin: .75rem 0; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<p class="sub">__SUB__</p>
<div class="bar">
  <input id="q" type="search" placeholder="Filter by package or maintainer…" autofocus>
  <select id="st"><option value="">All statuses</option>__OPTIONS__</select>
</div>
<p id="count"></p>
<table>
  <colgroup>
    <col style="width:8.5rem"><col><col style="width:15rem">
    <col style="width:15rem"><col style="width:13rem">
  </colgroup>
  <thead><tr>
    <th data-k="status">Status</th><th data-k="name">Source package</th>
    <th data-k="left">__LEFT__</th><th data-k="right">__RIGHT__</th>
    <th data-k="maint">Maintainers</th>
  </tr></thead>
  <tbody id="tb"></tbody>
</table>
<script>
const DATA = __DATA__;
const tb = document.getElementById('tb'), q = document.getElementById('q'),
      st = document.getElementById('st'), count = document.getElementById('count');
let rows = DATA, sortKey = null, sortDir = 1;
function cls(s) { return 's-' + s.split('-')[0]; }
function esc(s) { return String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function render() {
  const needle = q.value.toLowerCase(), status = st.value;
  let view = rows.filter(r => (!status || r.status === status) &&
      (!needle || r.name.toLowerCase().includes(needle) ||
                  r.maint.toLowerCase().includes(needle)));
  if (sortKey) {
    const num = sortKey === 'left' || sortKey === 'right';  // numeric only for versions
    view = [...view].sort((a, b) =>
      sortDir * String(a[sortKey]).localeCompare(String(b[sortKey]), 'en', {numeric: num}));
  }
  count.textContent = view.length + ' of ' + rows.length + ' source packages';
  tb.innerHTML = view.map(r =>
    `<tr><td class="status ${cls(r.status)}">${esc(r.status)}</td><td>${esc(r.name)}</td>` +
    `<td class="v" title="${esc(r.left)}">${esc(r.left) || '—'}</td>` +
    `<td class="v" title="${esc(r.right)}">${esc(r.right) || '—'}</td>` +
    `<td class="m" title="${esc(r.maint)}">${esc(r.maint) || '—'}</td></tr>`).join('');
}
q.oninput = st.onchange = render;
document.querySelectorAll('th').forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  sortDir = sortKey === k ? -sortDir : 1; sortKey = k; render();
});
render();
</script>
</body>
</html>
"""


def emit_html(rows, left, right, counts, out) -> None:
    title = f"{left.label} vs {right.label} — source package versions"
    sub = "oss + non-oss · x86_64 + noarch · " + " · ".join(
        f"{k}: {v}" for k, v in counts.items()
    )
    options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in counts
    )
    slim = [
        {
            "name": r["name"],
            "status": r["status"],
            "left": r["left"],
            "right": r["right"],
            "maint": ", ".join(r["maintainers"]),
        }
        for r in rows
    ]
    page = HTML_TEMPLATE
    for needle, value in (
        ("__TITLE__", html.escape(title)),
        ("__SUB__", html.escape(sub)),
        ("__LEFT__", html.escape(left.label)),
        ("__RIGHT__", html.escape(right.label)),
        ("__OPTIONS__", options),
        ("__DATA__", json.dumps(slim).replace("</", "<\\/").replace("&", "\\u0026").replace("<", "\\u003c")),
    ):
        page = page.replace(needle, value)
    out.write(page)


# --------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Diff source package versions between openSUSE distributions.",
        epilog="Grep the STATUS column, e.g. `osdiff.py | grep Older`.",
    )
    p.add_argument("--left", default="tumbleweed", help="reference distro (default: tumbleweed)")
    p.add_argument("--right", default="leap161", help="compared distro (default: leap161)")
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
    p.add_argument("-q", "--quiet", action="store_true", help="no summary on stderr")
    args = p.parse_args(argv)

    for key in (args.left, args.right):
        if key not in DISTROS:
            p.error(f"unknown distro {key!r}; known: {', '.join(DISTROS)}")
    if args.left == args.right:
        p.error("--left and --right must differ")
    left, right = DISTROS[args.left], DISTROS[args.right]
    st = statuses(left, right)

    if not HAVE_RPM and not args.quiet:
        print("note: python3-rpm not found, using built-in rpmvercmp", file=sys.stderr)

    if args.maintainer:
        args.maintainers = True
    maint = {} if args.no_maintainers else fetch_maintainers(args.quiet)

    data = {}
    for distro in (left, right):
        pkgs: dict = {}
        for repo in distro.repos:
            path = fetch(repo, args.quiet, args.refresh)
            if not args.quiet:
                print(f"parsing {path} ({distro.label} {repo.name})…", file=sys.stderr)
            parse_archives(path, repo.name, pkgs)
        data[distro.key] = pkgs

    rows = compare(data[left.key], data[right.key], args.with_release, st, maint)

    counts = {s: 0 for s in st.values()}
    for r in rows:
        counts[r["status"]] += 1

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

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        if args.format == "table":
            emit_table(rows, left.label, right.label, out, args.maintainers)
        elif args.format == "md":
            emit_markdown(rows, left.label, right.label, out, args.maintainers)
        elif args.format == "csv":
            emit_csv(rows, left.label, right.label, out, args.maintainers)
        elif args.format == "json":
            emit_json(rows, left, right, counts, out)
        elif args.format == "html":
            emit_html(rows, left, right, counts, out)
    finally:
        if args.output:
            out.close()

    if not args.quiet:
        total = sum(counts.values())
        print(
            f"\n{total} source packages: "
            + ", ".join(f"{v} {k}" for k, v in counts.items()),
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
