#!/usr/bin/python3
"""Tests for the version normalization rules.

These three rules decide whether a package reads as behind, ahead or level, so
they get tested both ways: that they fire on the schemes they are for, and,
more importantly, that they stay out of the way everywhere else.

Run them from the top of the checkout:

    python3 -m unittest discover -v -s tests
"""

import html
import json
import re
import unittest

import osdiff
from osdiff import Package, evr_cmp, normalize_version


def norm(name, version):
    return normalize_version(name, version)[0]


def rules(name, version):
    return normalize_version(name, version)[1]


def cmp_(name, a, b):
    """Compare two versions of `name` the way the diff does."""
    return evr_cmp(Package(name, a, "1"), Package(name, b, "1"), False)


class VPrefix(unittest.TestCase):
    def test_stripped(self):
        self.assertEqual(norm("dysk", "v3.6.1"), "3.6.1")
        self.assertEqual(rules("dysk", "v3.6.1"), ("v-prefix",))

    def test_fixes_the_comparison(self):
        # Alpha loses to numeric under rpm rules, so v3.6.1 read as older.
        self.assertGreater(cmp_("dysk", "v3.6.1", "2.9.1"), 0)

    def test_only_before_a_digit(self):
        for v in ("valgrind", "1.0", "vv1.0"):
            self.assertEqual(rules("x", v), (), v)


class PreRelease(unittest.TestCase):
    def test_markers(self):
        self.assertEqual(norm("x", "4.18.0rc1"), "4.18.0~rc1")
        self.assertEqual(norm("x", "2.1.0beta9"), "2.1.0~beta9")
        self.assertEqual(norm("x", "0.9pre"), "0.9~pre")
        self.assertEqual(norm("x", "1.0.0rc+git9"), "1.0.0~rc+git9")

    def test_sorts_below_the_release(self):
        self.assertLess(cmp_("x", "4.18.0rc1", "4.18.0"), 0)
        self.assertLess(cmp_("resource-agents",
                             "4.18.0rc1+git0.36fef17a", "4.18.0+git94.6c50a9b"), 0)

    def test_agrees_with_a_correctly_packaged_tilde(self):
        self.assertEqual(norm("x", "1.0.0~rc+git9"), "1.0.0~rc+git9")
        self.assertEqual(cmp_("x", "1.0.0rc+g1", "1.0.0~rc+g1"), 0)

    def test_leaves_ordinary_versions_alone(self):
        # Must follow a digit and end at a boundary, so no git hash (hex has no
        # r/v/t/l/p) and no word that merely contains a marker can match.
        for v in ("0.7+git6770db21b08edd907d1c9bd962297ff55664e3fe",
                  "1.2src", "3.0.0", "1.48+35.c49e7c08", "2.4.1+deb1",
                  "1.0.0release", "5.0.40", "20251025"):
            self.assertEqual(rules("x", v), (), v)


class CpanDecimal(unittest.TestCase):
    def test_perl_normal_form(self):
        # perl's own version->parse(x)->normal, minus the leading v.
        self.assertEqual(norm("perl-CPAN-Mini", "1.111017"), "1.111.17")
        self.assertEqual(norm("perl-Encode", "3.24"), "3.240.0")
        self.assertEqual(norm("perl-Alien-wxWidgets", "0.69"), "0.690.0")
        self.assertEqual(norm("perl-X", "0.01"), "0.10.0")
        self.assertEqual(norm("perl-CPAN-Perl-Releases", "5.20240720"),
                         "5.202.407.200")

    def test_the_two_notations_meet(self):
        self.assertEqual(cmp_("perl-CPAN-Mini", "1.111017", "1.111.17"), 0)
        self.assertEqual(cmp_("perl-BSD-Resource", "1.2911", "1.291.100"), 0)
        self.assertEqual(cmp_("perl-curry", "2.000001", "2.0.1"), 0)

    def test_ordering_rpm_gets_wrong(self):
        # 3.24 is v3.240.0 and 3.9 is v3.900.0, so 3.9 is the newer release —
        # the opposite of what rpm's segment-wise 24 > 9 says.
        self.assertLess(cmp_("perl-Encode", "3.24", "3.9"), 0)
        self.assertLess(cmp_("perl-Encode", "3.210.0", "3.24"), 0)

    def test_dotted_versions_are_left_alone(self):
        for v in ("1.111.17", "5.202.608.30", "3.0.0"):
            self.assertEqual(rules("perl-X", v), (), v)

    def test_scoped_to_perl(self):
        # lua's 3.02 is 3.02, not perl's 3.20.0 — reading it as a CPAN decimal
        # would make lua53-cliargs look further ahead than it is.
        self.assertEqual(rules("lua53-cliargs", "3.02"), ())
        self.assertEqual(norm("lua53-cliargs", "3.02"), "3.02")


class DisplayIsUntouched(unittest.TestCase):
    def test_package_keeps_what_was_packaged(self):
        p = Package("perl-CPAN-Mini", "1.111017", "bp161.1.2")
        self.assertEqual(p.version, "1.111017")
        self.assertEqual(p.vr, "1.111017-bp161.1.2")
        self.assertEqual(p.cmp_version, "1.111.17")
        self.assertEqual(p.cmp_rules, ("cpan-decimal",))


class PerfectionStatus(unittest.TestCase):
    """Perfection is Same plus "and nobody ships anything newer"."""

    TW = osdiff.Distro("tumbleweed", "Tumbleweed", "TW", [])
    LEAP = osdiff.Distro("leap161", "Leap 16.1", "Leap", [])

    def rows(self, upstream):
        st = osdiff.statuses(self.TW, self.LEAP, upstream is not None)
        left = {n: {v: Package(n, v, "1") for v in (vs[0],)}
                for n, vs in self.PKGS.items()}
        right = {n: {vs[1]: Package(n, vs[1], "1")} for n, vs in self.PKGS.items()}
        rows = osdiff.compare(left, right, [], False, st, {}, upstream)
        return {r["name"]: r["status"] for r in rows}

    PKGS = {
        "level-and-current": ("1.0", "1.0"),   # Repology knows nothing newer
        "level-but-stale": ("1.0", "1.0"),     # …but Repology does
        "behind": ("2.0", "1.0"),
    }

    def test_splits_out_of_same(self):
        got = self.rows({"level-but-stale": "3.0"})
        self.assertEqual(got["level-and-current"], "Perfection")
        self.assertEqual(got["level-but-stale"], "Same")
        self.assertEqual(got["behind"], "Older-in-Leap")

    def test_absent_without_an_upstream_column(self):
        # No --repology means no way to earn it, so the status must not appear.
        got = self.rows(None)
        self.assertEqual(set(got.values()), {"Same", "Older-in-Leap"})
        self.assertNotIn("perfect", osdiff.statuses(self.TW, self.LEAP))

    def test_never_awarded_to_a_mismatch(self):
        # Same-in-name only: an outdated pair stays Older even with no upstream
        # entry of its own.
        got = self.rows({})
        self.assertEqual(got["behind"], "Older-in-Leap")


class StatusLegend(unittest.TestCase):
    """Every status the run can produce must be explained on the page."""

    TW = osdiff.Distro("tumbleweed", "Tumbleweed", "TW", [])
    LEAP = osdiff.Distro("leap161", "Leap 16.1", "Leap", [])
    OLD = osdiff.Distro("leap160", "Leap 16.0", "Leap160", [])

    def page(self, extras=(), upstream=True):
        import io
        st = osdiff.statuses(self.TW, self.LEAP, upstream)
        counts = dict.fromkeys(st.values(), 1)
        vcols = [("left", "Tumbleweed"), ("right", "Leap 16.1")]
        totals = {"total": 1, "left": 1, "right": 1, "both": 1,
                  "extras": [1] * len(extras)}
        out = io.StringIO()
        osdiff.emit_html([], self.TW, self.LEAP, list(extras), vcols, counts,
                         out, totals, st)
        return out.getvalue(), st

    def test_every_status_has_a_legend_entry(self):
        page, st = self.page()
        legend = re.search(r'<details class="legend">.*?</details>', page, re.S)
        self.assertIsNotNone(legend, "no legend on the page")
        for status in st.values():
            self.assertIn(f">{status}</dt>", legend.group(0))

    def test_legend_and_tooltip_say_the_same_thing(self):
        # One wording, three places.  If these ever drift, the table is
        # explaining itself two different ways depending on where you look.
        page, st = self.page()
        hints = json.loads(re.search(r"const HINTS = (\{.*?\});", page).group(1))
        self.assertEqual(sorted(hints), sorted(st.values()))
        legend = re.search(r"<dl>(.*?)</dl>", page, re.S).group(1)
        for status, text in hints.items():
            self.assertIn(html.escape(text), legend, status)

    def test_only_in_right_admits_the_extra_columns(self):
        # A row can be Only-in-Leap on the strength of Leap 16.0 alone, so the
        # explanation must not promise that Leap 16.1 ships it.
        with_extra, st = self.page(extras=[self.OLD])
        without, _ = self.page()
        hint = lambda p: json.loads(  # noqa: E731
            re.search(r"const HINTS = (\{.*?\});", p).group(1))[st["only_right"]]
        self.assertIn("older release", hint(with_extra))
        self.assertNotIn("older release", hint(without))

    def test_perfection_names_its_mark(self):
        page, st = self.page()
        hints = json.loads(re.search(r"const HINTS = (\{.*?\});", page).group(1))
        self.assertIn("✦", hints[st["perfect"]])


class ResolvePorts(unittest.TestCase):
    def test_all(self):
        self.assertEqual(osdiff.resolve_ports("all"), list(osdiff.PORTS_TREES))

    def test_aliases(self):
        self.assertEqual(osdiff.resolve_ports("s390x,ppc64le"), ["zsystems", "ppc"])

    def test_arm_is_one_download(self):
        # /ports/armv7hl redirects to the aarch64 tree, so asking for both must
        # not fetch 151 MB twice.
        self.assertEqual(osdiff.resolve_ports("armv7hl,armv6hl,aarch64"), ["aarch64"])

    def test_rejects_nonsense(self):
        with self.assertRaises(SystemExit):
            osdiff.resolve_ports("sparc")


class StreamedParse(unittest.TestCase):
    """A gzipped index must parse to exactly what the plain one does."""

    ARCHES = ("s390x", "noarch", "boot", "x86_64")

    def _write(self, tmp, gzipped):
        import gzip as gz
        import os
        path = os.path.join(tmp, "ARCHIVES" + (".gz" if gzipped else ""))
        # Every record gets its own release, so a match lost to a chunk
        # boundary shows up as a missing version rather than hiding behind an
        # identical copy.  Interleaved with lines that must not match at all.
        lines = []
        for i in range(4000):
            for arch in self.ARCHES:
                nvr = f"{arch}pkg-1.0-{i}.1"
                lines.append(f"./{arch}/{nvr}.{arch}.rpm:"
                             f"    Source RPM  : {nvr}.src.rpm".encode())
                lines.append(f"Name        : {nvr}".encode())
        body = b"\n".join(lines) + b"\n"
        opener = gz.open if gzipped else open
        with opener(path, "wb") as fh:
            fh.write(body)
        return path

    def test_matches_the_mmap_path(self):
        import tempfile
        import unittest.mock
        with tempfile.TemporaryDirectory() as tmp:
            plain = osdiff.parse_archives(self._write(tmp, False), "oss", arches=None)
            # A 4 kB chunk over a ~1.4 MB body puts hundreds of boundaries in
            # mid-line, which is the only thing this test is really about.
            with tempfile.TemporaryDirectory() as tmp2, \
                    unittest.mock.patch.object(osdiff, "_CHUNK", 4096):
                streamed = osdiff.parse_archives(
                    self._write(tmp2, True), "oss", arches=None)
        self.assertEqual({n: sorted(v) for n, v in plain.items()},
                         {n: sorted(v) for n, v in streamed.items()})
        self.assertEqual(sum(len(v) for v in streamed.values()), 3 * 4000)

    def test_ports_take_every_arch_but_the_non_arch_dirs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pkgs = osdiff.parse_archives(self._write(tmp, True), "oss", arches=None)
        self.assertEqual(sorted(pkgs), ["noarchpkg", "s390xpkg", "x86_64pkg"])
        self.assertEqual(pkgs["s390xpkg"]["1.0-0.1"].arches, {"s390x"})

    def test_the_x86_64_tree_stays_narrow(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pkgs = osdiff.parse_archives(self._write(tmp, True), "oss")
        self.assertEqual(sorted(pkgs), ["noarchpkg", "x86_64pkg"])


class NoSourceRpms(unittest.TestCase):
    """chromium, bun, the rust bootstraps and every kernel are `nosrc`."""

    BODY = (b"./x86_64/chromium-152.0.7977.64-1.1.x86_64.rpm:"
            b"    Source RPM  : chromium-152.0.7977.64-1.1.nosrc.rpm\n"
            b"./x86_64/bzip2-1.0.8-6.1.x86_64.rpm:"
            b"    Source RPM  : bzip2-1.0.8-6.1.src.rpm\n")

    def _parse(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ARCHIVES")
            with open(path, "wb") as fh:
                fh.write(self.BODY)
            return osdiff.parse_archives(path, "oss")

    def test_nosrc_packages_are_not_dropped(self):
        pkgs = self._parse()
        self.assertEqual(sorted(pkgs), ["bzip2", "chromium"])
        # The suffix must not leak into the version the table prints.
        self.assertEqual(sorted(pkgs["chromium"]), ["152.0.7977.64-1.1"])


class Rpmvercmp(unittest.TestCase):
    """The fallback CI runs on must agree with rpm, where rpm is available."""

    @unittest.skipUnless(osdiff.HAVE_RPM, "no python3-rpm")
    def test_fallback_matches_rpm(self):
        import sys
        import types

        # Re-execute the module with the rpm import forced to fail, which is
        # the only way to reach the pure-python _rpmvercmp on a host that has
        # python3-rpm.  Rewriting the import beats patching sys.modules: the
        # module is re-exec'd, not re-imported, so no finder would be consulted.
        with open(osdiff.__file__, encoding="utf-8") as fh:
            src = fh.read()
        src = src.replace(
            "from rpm import labelCompare as _label_compare",
            "raise ImportError('blocked by the test')", 1)
        self.assertIn("blocked by the test", src)
        mod = types.ModuleType("osdiff_nonrpm")
        mod.__file__ = osdiff.__file__
        sys.modules["osdiff_nonrpm"] = mod
        exec(compile(src, osdiff.__file__, "exec"), mod.__dict__)
        self.assertFalse(mod.HAVE_RPM)

        pairs = [
            ("1.0", "1.0"), ("1.0", "1.1"), ("3.0.0", "3.0.0~alpha1"),
            ("4.18.0~rc1", "4.18.0"), ("1.111.17", "1.111.17"),
            ("3.240.0", "3.900.0"), ("2.9.1", "3.6.1"), ("1.0^git1", "1.0"),
            ("0.10.0", "0.10.0"), ("5.202.407.200", "5.202.608.30"),
        ]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertEqual(
                    mod._label_compare((None, a, ""), (None, b, "")),
                    osdiff._label_compare((None, a, ""), (None, b, "")),
                )


if __name__ == "__main__":
    unittest.main()
