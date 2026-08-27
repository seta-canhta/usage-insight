"""Tests for the self-update path.

    python3 -m pytest cli/tests/test_update.py -q

The decision is separated from the doing so it can be tested without either.
``plan()`` is pure and gets the table of cases; ``install()`` gets a real
directory, a real archive and a real symlink, because the properties worth
asserting there -- the running file is never overwritten, a failed self-test
leaves the old version live, a mismatched digest changes nothing -- are
properties of the filesystem and would not survive being mocked.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLI = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_CLI)
for _p in (_CLI, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import update  # noqa: E402


def manifest(version="0.4.0", digest="a" * 64, schema="1.1.0",
             schemas=("1.0.0", "1.1.0")):
    return {"version": version, "sha256": digest,
            "url": "https://example.com/insight-{}.pyz".format(version),
            "client_schema": schema,
            "schemas": list(schemas) if schemas is not None else None}


class VersionTests(unittest.TestCase):
    def test_semver_only(self):
        self.assertEqual(update.parse_version("1.2.3"), (1, 2, 3))
        for bad in ("1.2", "v1.2.3", "1.2.3-rc1", "", "abc"):
            self.assertIsNone(update.parse_version(bad), bad)

    def test_newer_is_ordered_numerically_not_lexically(self):
        # "0.10.0" > "0.9.0" is the case a string compare gets wrong, and it is
        # the tenth release rather than an exotic one.
        self.assertTrue(update.is_newer("0.9.0", "0.10.0"))
        self.assertFalse(update.is_newer("0.10.0", "0.9.0"))
        self.assertFalse(update.is_newer("1.0.0", "1.0.0"))

    def test_an_unparseable_version_is_never_newer(self):
        self.assertFalse(update.is_newer("0.3.0", "latest"))
        self.assertFalse(update.is_newer("nightly", "0.4.0"))


class PlanTests(unittest.TestCase):
    def test_the_same_version_is_current(self):
        self.assertEqual(update.plan("0.4.0", manifest())[0], "current")

    def test_an_older_release_is_never_installed(self):
        self.assertEqual(update.plan("0.5.0", manifest("0.4.0"))[0], "current")

    def test_a_patch_or_minor_bump_installs(self):
        self.assertEqual(update.plan("0.3.0", manifest("0.3.1"))[0], "install")
        self.assertEqual(update.plan("0.3.0", manifest("0.4.0"))[0], "install")

    def test_a_major_bump_is_held_and_says_why(self):
        # The rule this project actually cares about: a major version is where
        # what gets collected could change, and that must not arrive on a
        # machine whose owner agreed to the old answer without them seeing it.
        action, why = update.plan("0.4.0", manifest("1.0.0"))
        self.assertEqual(action, "held")
        self.assertIn("insight update --now", why)

    def test_a_schema_the_endpoint_refuses_is_refused_here_first(self):
        action, why = update.plan(
            "0.4.0", manifest("0.5.0", schema="2.0.0", schemas=("1.0.0", "1.1.0")))
        self.assertEqual(action, "refused")
        self.assertIn("2.0.0", why)

    def test_an_endpoint_that_advertises_nothing_does_not_block_forever(self):
        # `schemas` absent means an endpoint too old to say. Treating silence as
        # a refusal would mean a fleet that never updates again.
        self.assertEqual(
            update.plan("0.4.0", manifest("0.5.0", schemas=None))[0], "install")

    def test_pinning_holds_the_version_and_says_what_is_being_declined(self):
        action, why = update.plan("0.4.0", manifest("0.5.0"), pinned="0.4.0")
        self.assertEqual(action, "pinned")
        self.assertIn("0.5.0 is available", why)
        self.assertIn("--unpin", why)

    def test_a_pin_at_the_current_release_is_not_a_block(self):
        # Pinned to what is being offered: there is nothing to hold back.
        self.assertEqual(
            update.plan("0.3.0", manifest("0.4.0"), pinned="0.4.0")[0], "install")
        self.assertEqual(
            update.plan("0.4.0", manifest("0.4.0"), pinned="0.4.0")[0], "current")

    def test_a_pin_at_a_version_the_manifest_does_not_describe_just_holds(self):
        # There is no digest for 0.4.0 anywhere, so "go to the pin" would mean
        # guessing a URL or installing unverified bytes. It holds instead.
        self.assertEqual(
            update.plan("0.3.0", manifest("0.5.0"), pinned="0.4.0")[0], "pinned")


class ManifestTests(unittest.TestCase):
    def test_a_good_manifest_passes(self):
        got = update.validate_manifest(manifest())
        self.assertEqual(got["version"], "0.4.0")
        self.assertEqual(got["schemas"], ["1.0.0", "1.1.0"])

    def test_a_manifest_without_a_digest_is_refused_not_defaulted(self):
        # There is no path where a missing digest becomes an unverified
        # install, so the whole document is refused rather than the field
        # filled in.
        for bad in (None, "", "notahash", "a" * 63, "z" * 64):
            data = manifest()
            data["sha256"] = bad
            with self.assertRaises(update.UpdateError):
                update.validate_manifest(data)

    def test_a_non_https_url_is_refused(self):
        data = manifest()
        data["url"] = "http://example.com/x.pyz"
        with self.assertRaises(update.UpdateError):
            update.validate_manifest(data)

    def test_a_nonsense_version_is_refused(self):
        data = manifest()
        data["version"] = "@VERSION@"
        with self.assertRaises(update.UpdateError):
            update.validate_manifest(data)


class DueTests(unittest.TestCase):
    def test_never_checked_is_due(self):
        self.assertTrue(update.due({}))

    def test_checked_a_minute_ago_is_not(self):
        self.assertFalse(update.due({"last_check_epoch": 1000},
                                    clock=lambda: 1000 + 60))

    def test_checked_an_hour_ago_is_due_again(self):
        # The interval is the collection interval. A release reaches a laptop
        # on its next run rather than its next working day -- which is what
        # 24 hours meant against a 09:00-19:00 day.
        self.assertEqual(update.CHECK_INTERVAL, 3600)
        self.assertTrue(update.due({"last_check_epoch": 1000},
                                   clock=lambda: 1000 + 3600))

    def test_checked_two_days_ago_is(self):
        self.assertTrue(update.due({"last_check_epoch": 1000},
                                   clock=lambda: 1000 + 2 * 86400))

    def test_a_clock_that_went_backwards_does_not_mean_never_again(self):
        # A laptop resuming, or a timezone correction. A naive `now - last <
        # interval` reads a negative delta as "recently checked" forever.
        self.assertTrue(update.due({"last_check_epoch": 9999},
                                   clock=lambda: 1000))


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="insight-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_checkout_is_not_an_installation(self):
        self.assertIsNone(update.installation(None))
        self.assertIsNone(update.installation(os.path.join(self.tmp, "repo")))

    def test_a_loose_pyz_is_not_an_installation(self):
        # Somebody downloaded the archive and ran it out of ~/Downloads. That
        # directory is not ours and nothing here may start writing to it.
        loose = os.path.join(self.tmp, "insight.pyz")
        with open(loose, "wb") as handle:
            handle.write(b"x")
        self.assertIsNone(update.installation(loose))

    def test_the_installer_layout_is_recognised(self):
        real = os.path.join(self.tmp, "insight-0.3.0.pyz")
        with open(real, "wb") as handle:
            handle.write(b"x")
        link = os.path.join(self.tmp, "current.pyz")
        os.symlink(real, link)
        place = update.installation(link)
        self.assertIsNotNone(place)
        self.assertEqual(place["resolved"], real)


class InstallTests(unittest.TestCase):
    """The filesystem half. Real files, real symlink, real subprocess."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="insight-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = os.path.join(self.tmp, "insight-0.3.0.pyz")
        self._write(self.old, "0.3.0")
        self.link = os.path.join(self.tmp, "current.pyz")
        os.symlink(self.old, self.link)
        self.place = update.installation(self.link)

    def _write(self, path, version, ok=True):
        """A stand-in archive: a script that answers --version like one."""
        with open(path, "w", encoding="utf-8") as handle:
            if ok:
                handle.write(
                    "import sys\nprint('insight {} (schema 1.1.0)')\n".format(version))
            else:
                handle.write("import sys\nsys.exit(3)\n")
        os.chmod(path, 0o755)
        return path

    def _release(self, version, ok=True):
        src = os.path.join(self.tmp, "release-{}".format(version))
        self._write(src, version, ok)
        with open(src, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return src, manifest(version, digest)

    def _download(self, src):
        def download(url, dest, **kwargs):
            shutil.copyfile(src, dest)
        return download

    def test_a_good_release_is_installed_and_the_symlink_moves(self):
        src, m = self._release("0.4.0")
        done = update.install(m, self.place, python=sys.executable,
                              download=self._download(src))
        self.assertEqual(done["installed"], "0.4.0")
        self.assertEqual(os.path.realpath(self.link),
                         os.path.join(self.tmp, "insight-0.4.0.pyz"))

    def test_the_previous_archive_survives_so_rollback_is_a_symlink_flip(self):
        src, m = self._release("0.4.0")
        update.install(m, self.place, python=sys.executable,
                       download=self._download(src))
        self.assertTrue(os.path.isfile(self.old))
        update.rollback(self.place, self.old)
        self.assertEqual(os.path.realpath(self.link), self.old)

    def test_a_digest_mismatch_changes_nothing(self):
        src, m = self._release("0.4.0")
        m["sha256"] = "b" * 64
        with self.assertRaises(update.UpdateError) as caught:
            update.install(m, self.place, python=sys.executable,
                           download=self._download(src))
        self.assertIn("checksum mismatch", str(caught.exception))
        self.assertEqual(os.path.realpath(self.link), self.old)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "insight-0.4.0.pyz")))

    def test_an_archive_that_does_not_run_never_gets_pointed_at(self):
        # The ordering that matters. Verified bytes can still fail to run, and
        # discovering that after the symlink moved means the hourly job is now
        # running the broken thing, once an hour, forever.
        src, m = self._release("0.4.0", ok=False)
        with self.assertRaises(update.UpdateError):
            update.install(m, self.place, python=sys.executable,
                           download=self._download(src))
        self.assertEqual(os.path.realpath(self.link), self.old)

    def test_an_archive_reporting_the_wrong_version_is_refused(self):
        src, m = self._release("0.4.0")
        m["version"] = "0.5.0"
        with open(src, "rb") as handle:
            m["sha256"] = hashlib.sha256(handle.read()).hexdigest()
        with self.assertRaises(update.UpdateError) as caught:
            update.install(m, self.place, python=sys.executable,
                           download=self._download(src))
        self.assertIn("0.5.0", str(caught.exception))
        self.assertEqual(os.path.realpath(self.link), self.old)

    def test_no_stray_temporary_file_is_left_behind_on_failure(self):
        src, m = self._release("0.4.0")
        m["sha256"] = "c" * 64
        with self.assertRaises(update.UpdateError):
            update.install(m, self.place, python=sys.executable,
                           download=self._download(src))
        strays = [n for n in os.listdir(self.tmp) if n.startswith(".update-")]
        self.assertEqual(strays, [])


class CheckTests(unittest.TestCase):
    """The orchestrator. Its one hard requirement: it never raises."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="insight-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        real = os.path.join(data, "insight-0.3.0.pyz")
        with open(real, "wb") as handle:
            handle.write(b"x")
        self.link = os.path.join(data, "current.pyz")
        os.symlink(real, self.link)

    def run_check(self, **kwargs):
        args = {"home": self.home, "endpoint": "https://e.example",
                "current_version": "0.3.0", "archive": self.link,
                "enabled": True}
        args.update(kwargs)
        return update.check(**args)

    def test_disabled_does_nothing_and_looks_at_no_network(self):
        def boom(*a, **k):
            raise AssertionError("must not reach the network when off")
        self.assertEqual(self.run_check(enabled=False, fetch=boom)["action"], "off")

    def test_no_endpoint_is_not_an_error(self):
        self.assertEqual(self.run_check(endpoint=None)["action"], "unavailable")

    def test_a_checkout_is_unavailable_rather_than_a_failure(self):
        got = self.run_check(archive=None)
        self.assertEqual(got["action"], "unavailable")
        self.assertIn("git pull", got["detail"])

    def test_an_unreachable_endpoint_is_a_no_op_that_records_why(self):
        # The property the whole feature hangs on: a laptop on a plane still
        # collects. A failed check is logged and nothing else.
        def fetch(endpoint, **kwargs):
            raise update.UpdateError("connection refused")
        got = self.run_check(fetch=fetch)
        self.assertEqual(got["action"], "failed")
        self.assertIn("connection refused", got["detail"])
        self.assertEqual(update.load_state(self.home)["last_result"], "failed")

    def test_a_manifest_of_garbage_is_a_no_op_too(self):
        got = self.run_check(fetch=lambda e, **k: {"version": "nope"})
        self.assertEqual(got["action"], "failed")

    def test_an_installer_that_explodes_does_not_reach_the_caller(self):
        def installer(m, place):
            raise OSError("read-only file system")
        got = self.run_check(fetch=lambda e, **k: manifest(), installer=installer)
        self.assertEqual(got["action"], "failed")
        self.assertIn("read-only", got["detail"])

    def test_a_successful_update_names_both_versions(self):
        def installer(m, place):
            return {"installed": m["version"], "archive": "x",
                    "previous_archive": "y", "pruned": []}
        got = self.run_check(fetch=lambda e, **k: manifest(), installer=installer)
        self.assertEqual((got["action"], got["from"], got["to"]),
                         ("updated", "0.3.0", "0.4.0"))
        state = update.load_state(self.home)
        self.assertEqual(state["history"][0]["to"], "0.4.0")

    def test_it_does_not_look_again_within_the_interval(self):
        calls = []

        def fetch(endpoint, **kwargs):
            calls.append(endpoint)
            return manifest("0.3.0")
        self.run_check(fetch=fetch)
        self.run_check(fetch=fetch)
        self.assertEqual(len(calls), 1)

    def test_force_looks_anyway(self):
        calls = []

        def fetch(endpoint, **kwargs):
            calls.append(endpoint)
            return manifest("0.3.0")
        self.run_check(fetch=fetch)
        self.run_check(fetch=fetch, force=True)
        self.assertEqual(len(calls), 2)

    def test_check_only_reports_without_installing(self):
        def installer(m, place):
            raise AssertionError("apply_it=False must not install")
        got = self.run_check(fetch=lambda e, **k: manifest(),
                             installer=installer, apply_it=False)
        self.assertEqual(got["action"], "install")

    def test_status_survives_a_machine_that_has_never_checked(self):
        got = update.status(self.home, "0.3.0", self.link, True)
        self.assertTrue(got["supported"])
        self.assertIsNone(got["last_check_at"])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insight-state-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_corrupt_state_file_is_ignored_not_fatal(self):
        os.makedirs(self.tmp, exist_ok=True)
        with open(update.state_path(self.tmp), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(update.load_state(self.tmp), {})

    def test_a_round_trip(self):
        update.save_state(self.tmp, {"pinned": "0.3.0"})
        self.assertEqual(update.load_state(self.tmp)["pinned"], "0.3.0")

    def test_the_state_file_is_json_a_person_can_read(self):
        update.save_state(self.tmp, {"pinned": "0.3.0"})
        with open(update.state_path(self.tmp), encoding="utf-8") as handle:
            self.assertIn("\n", handle.read())


if __name__ == "__main__":
    unittest.main()
