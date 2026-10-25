"""No-network Google OAuth PKCE and callback abuse regressions."""
import base64
import hashlib
import io
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

import garmin_google_oauth as google

CLIENT = "test-desktop-client.apps.googleusercontent.com"


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.attempt = google.new_authorization(CLIENT, 38501)

    def test_browser_url_is_pkce_s256_and_drive_file_only(self):
        parts = urlsplit(self.attempt.url)
        q = parse_qs(parts.query)
        self.assertEqual(parts.netloc, "accounts.google.com")
        self.assertEqual(q["scope"], [google.DRIVE_SCOPE])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(q["prompt"], ["consent"])
        self.assertEqual(q["redirect_uri"], ["http://127.0.0.1:38501/oauth/callback"])
        self.assertEqual(q["state"], [self.attempt.state])
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.attempt.verifier.encode()).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(q["code_challenge"], [challenge])
        self.assertNotIn("client_secret", q)

    def test_random_state_and_verifier_per_installation(self):
        second = google.new_authorization(CLIENT, 38501)
        self.assertNotEqual(self.attempt.state, second.state)
        self.assertNotEqual(self.attempt.verifier, second.verifier)

    def test_only_loopback_and_desktop_publisher_client(self):
        for port in (-1, 0, 65536, "9001", True):
            with self.subTest(port=port), self.assertRaises(google.OAuthError):
                google.new_authorization(CLIENT, port)
        with self.assertRaises(google.OAuthError):
            google.new_authorization("unknown-client", 38501)

    def test_auth_callback_valid(self):
        url = "/oauth/callback?" + urlencode({"state": self.attempt.state, "code": "auth-code"})
        self.assertEqual(google.consume_callback(self.attempt, url), "auth-code")

    def test_rejects_state_swap_and_duplicate_parameters(self):
        for params in ({"state": "attacker", "code": "auth-code"},
                       {"code": "auth-code"}, {"state": self.attempt.state},
                       {"state": self.attempt.state, "error": "access_denied"}):
            with self.subTest(params=params), self.assertRaises(google.OAuthError):
                google.consume_callback(self.attempt, "/oauth/callback?" + urlencode(params))
        with self.assertRaises(google.OAuthError):
            google.consume_callback(
                self.attempt, "/oauth/callback?state=" + self.attempt.state +
                "&state=" + self.attempt.state + "&code=auth-code")

    def test_rejects_redirect_path_or_external_url(self):
        for path in ("https://evil.test/oauth/callback?state=x",
                     "/other?state=x&code=x", "/oauth/callback#fragment",
                     "/oauth/callback?x=1"):
            with self.subTest(path=path), self.assertRaises(google.OAuthError):
                google.consume_callback(self.attempt, path)

    def test_access_and_refresh_required_and_scope_cannot_widen(self):
        proper = {"scope": google.DRIVE_SCOPE, "token_type": "Bearer",
                  "access_token": "access", "refresh_token": "refresh", "expires_in": 3599}
        self.assertEqual(google.validate_token_response(proper), proper)
        bad = [{"scope": "https://www.googleapis.com/auth/drive"},
               {"scope": google.DRIVE_SCOPE + " https://www.googleapis.com/auth/drive"},
               {"refresh_token": ""},
               {"token_type": "Basic"}, {"expires_in": -1}, {"scope": ""}]
        for patch in bad:
            with self.subTest(patch=patch), self.assertRaises(google.OAuthError):
                google.validate_token_response(dict(proper, **patch))

    def test_post_code_exchange_has_no_password_or_secret(self):
        response = {"scope": google.DRIVE_SCOPE, "token_type": "Bearer",
                    "access_token": "access", "refresh_token": "refresh",
                    "expires_in": 3599}
        def http(request, timeout=15):
            self.assertEqual(request.full_url, google.TOKEN_URL)
            self.assertEqual(request.get_method(), "POST")
            q = parse_qs(request.data.decode())
            self.assertEqual(q["code"], ["auth-code"])
            self.assertEqual(q["code_verifier"], [self.attempt.verifier])
            self.assertEqual(q["redirect_uri"], [self.attempt.redirect_uri])
            self.assertNotIn("client_secret", q)
            self.assertNotIn("password", q)
            return io.BytesIO(json.dumps(response).encode())
        result = google.exchange_code(self.attempt, "auth-code", urlopen=http)
        self.assertEqual(result["refresh_token"], "refresh")

    def test_refused_or_incomplete_token_fails_closed(self):
        with mock.patch.object(google.urllib.request, "urlopen",
                               return_value=io.BytesIO(b'{"error":"invalid_grant"}')):
            with self.assertRaises(google.OAuthError):
                google.exchange_code(self.attempt, "invalid-code")


if __name__ == "__main__":
    unittest.main()
