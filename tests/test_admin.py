"""The pipeline side: periods, the admin token, and who is expected."""

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin  # noqa: E402


class PeriodTests(unittest.TestCase):
    """A week or a month becomes a name and two dates, in one place."""

    def bounds(self, **kwargs):
        args = argparse.Namespace(week=None, month=None)
        for key, value in kwargs.items():
            setattr(args, key, value)
        return admin.period(args)

    def test_a_week_is_monday_to_sunday(self):
        name, start, end = self.bounds(week="2026-W35")
        self.assertEqual(name, "2026-W35")
        self.assertEqual(start, date(2026, 8, 24))
        self.assertEqual(end, date(2026, 8, 30))
        self.assertEqual(start.weekday(), 0)

    def test_a_month_ends_on_its_own_last_day(self):
        self.assertEqual(self.bounds(month="2026-02")[2], date(2026, 2, 28))
        self.assertEqual(self.bounds(month="2026-08")[2], date(2026, 8, 31))
        self.assertEqual(self.bounds(month="2026-12")[2], date(2026, 12, 31))

    def test_the_default_is_the_last_complete_week(self):
        # Somebody typing `pull` on a Wednesday means last week, not the four
        # days of this one -- a partial week rendered beside full ones reads
        # as a collapse in activity.
        name, start, end = self.bounds()
        today = date.today()
        self.assertLess(end, today)
        self.assertEqual((end - start).days, 6)

    def test_both_at_once_is_refused_rather_than_guessed(self):
        with self.assertRaises(SystemExit):
            self.bounds(week="2026-W35", month="2026-08")

    def test_a_malformed_period_says_the_shape_it_wanted(self):
        for kwargs in ({"week": "week 35"}, {"month": "August"}):
            with self.assertRaises(SystemExit) as caught:
                self.bounds(**kwargs)
            self.assertIn("e.g.", str(caught.exception))


class AdminEnvTests(unittest.TestCase):
    """The token lives in a file, not in a shell history or a process list."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="admin-env-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, ".admin.env")
        os.environ.pop("INSIGHT_ADMIN_TOKEN", None)
        self.addCleanup(os.environ.pop, "INSIGHT_ADMIN_TOKEN", None)

    def write(self, text, mode=0o600):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(self.path, mode)

    def test_it_is_read_and_comments_are_not(self):
        self.write("# the endpoint's own token\nINSIGHT_ADMIN_TOKEN=abc123\n")
        admin.load_admin_env(self.path)
        self.assertEqual(admin.admin_token(), "abc123")

    def test_a_real_environment_variable_wins(self):
        os.environ["INSIGHT_ADMIN_TOKEN"] = "from-the-shell"
        self.write("INSIGHT_ADMIN_TOKEN=from-the-file\n")
        admin.load_admin_env(self.path)
        self.assertEqual(admin.admin_token(), "from-the-shell")

    def test_a_world_readable_token_file_is_complained_about(self):
        self.write("INSIGHT_ADMIN_TOKEN=abc\n", mode=0o644)
        with mock.patch("sys.stderr") as err:
            admin.load_admin_env(self.path)
        self.assertTrue(err.write.called)

    def test_no_token_says_where_to_put_one(self):
        admin.load_admin_env(os.path.join(self.tmp, "absent"))
        with self.assertRaises(SystemExit) as caught:
            admin.admin_token()
        self.assertIn("INSIGHT_ADMIN_TOKEN=", str(caught.exception))

    def test_quotes_are_stripped(self):
        self.write('INSIGHT_ADMIN_TOKEN="quoted-value"\n')
        admin.load_admin_env(self.path)
        self.assertEqual(admin.admin_token(), "quoted-value")


class PeopleOutputTests(unittest.TestCase):
    """`people` names the state a person is in, not a count nobody can act on."""

    def render(self, payload):
        buffer = []
        with mock.patch.object(admin, "call", return_value=(200, payload)), \
                mock.patch("builtins.print", lambda *a, **k:
                           buffer.append(" ".join(str(x) for x in a))):
            admin.cmd_people(argparse.Namespace(json=False))
        return "\n".join(buffer)

    def test_someone_expected_who_has_not_arrived_is_named_as_waiting(self):
        out = self.render({
            "people": [{"email": "lan@seta-international.vn", "on_roster": True,
                        "enrolled": False, "fingerprints": 0}],
            "expected": 1, "enrolled": 0, "waiting": ["lan@seta-international.vn"]})
        self.assertIn("waiting", out)
        self.assertIn("lan@seta-international.vn", out)

    def test_an_empty_roster_says_what_to_type_next(self):
        out = self.render({"people": [], "expected": 0, "enrolled": 0,
                           "waiting": []})
        self.assertIn("admin.py add", out)


if __name__ == "__main__":
    unittest.main()
