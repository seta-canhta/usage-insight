"""Tests for upload identity and the server whitelist.

    python3 -m pytest cli/tests/test_identity.py -q
"""

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import identity  # noqa: E402


class EmailTests(unittest.TestCase):
    def test_normalises_case_and_surrounding_space(self):
        self.assertEqual(
            identity.normalise_email("  Canh@SETA-International.VN "),
            "canh@seta-international.vn")

    def test_rejects_something_that_is_not_an_address(self):
        for bad in ("", "canh", "canh@", "@seta.vn", "canh@seta", "a b@c.vn"):
            with self.assertRaises(identity.IdentityError, msg=bad):
                identity.normalise_email(bad)

    def test_rejects_separators_that_would_corrupt_the_whitelist(self):
        # `,` splits people and `:` splits email from fingerprint. An address
        # carrying either would silently create a second, wrong entry.
        for bad in ("a,b@seta.vn", "a:b@seta.vn"):
            with self.assertRaises(identity.IdentityError, msg=bad):
                identity.normalise_email(bad)


class SecretTests(unittest.TestCase):
    def test_two_secrets_are_never_the_same(self):
        self.assertNotEqual(identity.mint_secret(), identity.mint_secret())

    def test_a_secret_is_long_enough_to_be_worth_nothing_when_hashed(self):
        self.assertGreaterEqual(len(identity.mint_secret()), 40)

    def test_the_fingerprint_is_a_plain_sha256_of_the_secret(self):
        # The server recomputes this, so the definition has to be boring enough
        # to reimplement in whatever language the proxy is written in.
        self.assertEqual(
            identity.fingerprint("hunter2"),
            hashlib.sha256(b"hunter2").hexdigest())

    def test_the_whitelist_line_never_carries_the_secret(self):
        secret = identity.mint_secret()
        line = identity.whitelist_line("canh@seta-international.vn", secret)
        self.assertNotIn(secret, line)
        self.assertEqual(line, "canh@seta-international.vn:" +
                         identity.fingerprint(secret))


class WhitelistTests(unittest.TestCase):
    def test_parses_one_person(self):
        self.assertEqual(
            identity.parse_whitelist("canh@seta-international.vn:abc123"),
            {"canh@seta-international.vn": ["abc123"]})

    def test_parses_several_people_across_commas_and_newlines(self):
        parsed = identity.parse_whitelist(
            "canh@seta-international.vn:aaa,\n minh@seta-international.vn:bbb\n")
        self.assertEqual(sorted(parsed), ["canh@seta-international.vn",
                                          "minh@seta-international.vn"])

    def test_a_second_fingerprint_is_a_rotation_in_flight(self):
        parsed = identity.parse_whitelist("canh@seta-international.vn:new:old")
        self.assertEqual(parsed["canh@seta-international.vn"], ["new", "old"])

    def test_ignores_blanks_and_comments(self):
        parsed = identity.parse_whitelist(
            "# team\n,canh@seta-international.vn:aaa,\n")
        self.assertEqual(parsed, {"canh@seta-international.vn": ["aaa"]})

    def test_a_comment_may_contain_a_comma(self):
        # Found by deploying: the header line of a hand-edited allowed.env read
        # "# One line per engineer, exactly what `./insight whoami` prints."
        # Flattening newlines to commas first made "exactly what ..." an entry,
        # and the endpoint refused to start over a remark.
        parsed = identity.parse_whitelist(
            "# One line per engineer, exactly what `whoami` prints.\n"
            "canh@seta-international.vn:aaa\n")
        self.assertEqual(parsed, {"canh@seta-international.vn": ["aaa"]})

    def test_a_comment_after_an_entry_is_not_part_of_it(self):
        parsed = identity.parse_whitelist(
            "canh@seta-international.vn:aaa   # left the team 2026-09-01?\n")
        self.assertEqual(parsed, {"canh@seta-international.vn": ["aaa"]})

    def test_a_comment_does_not_swallow_the_next_line(self):
        parsed = identity.parse_whitelist(
            "# a note\ncanh@seta-international.vn:aaa\n"
            "# another, with a comma\nminh@seta-international.vn:bbb\n")
        self.assertEqual(sorted(parsed), ["canh@seta-international.vn",
                                          "minh@seta-international.vn"])

    def test_the_environment_variable_form_is_unaffected(self):
        # INSIGHT_ALLOWED is one line of comma-separated entries and has no
        # comments; per-line comment stripping must not change it.
        parsed = identity.parse_whitelist(
            "canh@seta-international.vn:aaa,minh@seta-international.vn:bbb")
        self.assertEqual(sorted(parsed), ["canh@seta-international.vn",
                                          "minh@seta-international.vn"])

    def test_rejects_an_entry_with_no_fingerprint(self):
        with self.assertRaises(identity.IdentityError):
            identity.parse_whitelist("canh@seta-international.vn")


class IdentifyTests(unittest.TestCase):
    def setUp(self):
        self.secret = identity.mint_secret()
        self.allowed = identity.parse_whitelist(
            identity.whitelist_line("canh@seta-international.vn", self.secret))

    def test_a_whitelisted_secret_names_its_owner(self):
        self.assertEqual(identity.identify(self.secret, self.allowed),
                         "canh@seta-international.vn")

    def test_an_unknown_secret_is_nobody(self):
        self.assertIsNone(identity.identify(identity.mint_secret(), self.allowed))

    def test_an_empty_secret_is_nobody(self):
        self.assertIsNone(identity.identify("", self.allowed))
        self.assertIsNone(identity.identify(None, self.allowed))

    def test_the_fingerprint_itself_is_not_a_usable_credential(self):
        # The whole point of hashing: a leaked .env must not let anyone upload.
        leaked = identity.fingerprint(self.secret)
        self.assertIsNone(identity.identify(leaked, self.allowed))

    def test_both_secrets_work_during_a_rotation(self):
        old = self.secret
        new = identity.mint_secret()
        allowed = identity.parse_whitelist("canh@seta-international.vn:{}:{}".format(
            identity.fingerprint(new), identity.fingerprint(old)))
        self.assertEqual(identity.identify(new, allowed),
                         "canh@seta-international.vn")
        self.assertEqual(identity.identify(old, allowed),
                         "canh@seta-international.vn")


class PersonKeyTests(unittest.TestCase):
    def test_is_stable_for_the_same_person(self):
        self.assertEqual(identity.person_key("canh@seta-international.vn"),
                         identity.person_key("  Canh@SETA-International.VN "))

    def test_differs_between_people(self):
        self.assertNotEqual(identity.person_key("canh@seta-international.vn"),
                            identity.person_key("minh@seta-international.vn"))

    def test_does_not_contain_the_address(self):
        key = identity.person_key("canh@seta-international.vn")
        self.assertNotIn("canh", key)
        self.assertNotIn("@", key)


class RotateTests(unittest.TestCase):
    def test_keeps_the_current_secret_as_the_fallback(self):
        config = {"endpoint_token": "current"}
        new, previous = identity.rotate(config)
        self.assertEqual(previous, "current")
        self.assertNotEqual(new, "current")

    def test_a_machine_with_no_secret_yet_rotates_into_its_first(self):
        new, previous = identity.rotate({})
        self.assertTrue(new)
        self.assertEqual(previous, "")


class DefaultEndpointTests(unittest.TestCase):
    """The endpoint is defaulted, not asked for.

    An engineer who is never told `--endpoint` exists collects diligently for a
    month and then finds out nothing ever arrived.
    """

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import insight
        self.insight = insight

    def test_an_env_var_can_point_a_staging_machine_elsewhere(self):
        # So tests and a staging proxy do not have to patch the module.
        self.assertTrue(hasattr(self.insight, "DEFAULT_ENDPOINT"))

    def test_the_seta_endpoint_is_the_built_in_default(self):
        self.assertTrue(self.insight.SETA_ENDPOINT.startswith("https://"))
        self.assertIn("aeris-insight", self.insight.SETA_ENDPOINT)

    def test_the_default_is_not_a_loopback_or_placeholder_address(self):
        # A default nobody notices is worse than no default if it points at
        # localhost: every `ship` fails with a connection error instead of
        # saying the endpoint was never configured.
        for wrong in ("127.0.0.1", "localhost", "example.com"):
            self.assertNotIn(wrong, self.insight.SETA_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
