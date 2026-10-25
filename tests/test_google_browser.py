"""Loopback OAuth callback tests. Google is mocked; no internet access."""
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit
import urllib.error
import urllib.request

import garmin_google_browser as browser
import garmin_google_oauth as oauth

CLIENT = "test-client.apps.googleusercontent.com"


class LoopbackTests(unittest.TestCase):
    def test_valid_local_callback_exchanges_one_code_only(self):
        workers = []
        def open_fake_browser(url):
            uri = urlsplit(url)
            args = parse_qs(uri.query)
            redirect = args["redirect_uri"][0]
            state = args["state"][0]
            def submit():
                callback = redirect + "?" + urlencode({"code": "offline-code",
                                                         "state": state})
                with urllib.request.urlopen(callback, timeout=2) as response:
                    self.assertEqual(response.status, 200)
            worker = threading.Thread(target=submit)
            workers.append(worker)
            worker.start()
            return True
        with mock.patch.object(browser.webbrowser, "open",
                               side_effect=open_fake_browser), \
             mock.patch.object(oauth, "exchange_code",
                               return_value={"access_token": "mock-access"}) as exchange:
            result = browser.authorize_with_browser(CLIENT, timeout_seconds=4)
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(result["access_token"], "mock-access")
        exchange.assert_called_once()
        self.assertEqual(exchange.call_args.args[1], "offline-code")

    def test_callback_with_wrong_state_is_rejected_and_times_out(self):
        workers = []
        def open_fake_browser(url):
            uri = urlsplit(url)
            args = parse_qs(uri.query)
            redirect = args["redirect_uri"][0]
            def submit():
                wrong = redirect + "?" + urlencode({"code": "evil",
                                                     "state": "foreign"})
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(wrong, timeout=2)
            worker = threading.Thread(target=submit)
            workers.append(worker)
            worker.start()
            return True
        with mock.patch.object(browser.webbrowser, "open",
                               side_effect=open_fake_browser), \
             mock.patch.object(oauth, "exchange_code") as exchange:
            with self.assertRaisesRegex(oauth.OAuthError, "timed out"):
                browser.authorize_with_browser(CLIENT, timeout_seconds=1)
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        exchange.assert_not_called()

    def test_picker_one_folder_round_trip_over_localhost(self):
        threads = []
        folder = "pickedFolderABC12345"
        def open_fake_browser(url):
            query = parse_qs(urlsplit(url).query)
            redirect = query["redirect_uri"][0]
            def send():
                callback = redirect + "?" + urlencode({
                    "code": "picker-code", "state": query["state"][0],
                    "scope": oauth.DRIVE_SCOPE, "picked_file_ids": folder})
                with urllib.request.urlopen(callback, timeout=2) as response:
                    self.assertEqual(response.status, 200)
            t = threading.Thread(target=send)
            threads.append(t)
            t.start()
            return True
        with mock.patch.object(browser.webbrowser, "open", side_effect=open_fake_browser), \
             mock.patch.object(oauth, "exchange_code",
                               return_value={"access_token": "mock-token"}) as exchange:
            token, selected = browser.authorize_with_browser(
                CLIENT, choose_folder=True, timeout_seconds=4)
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(selected, folder)
        self.assertEqual(token["access_token"], "mock-token")
        exchange.assert_called_once()

    def test_rejects_bad_timeout(self):
        for timeout in (0, -5, 601):
            with self.subTest(timeout=timeout), self.assertRaises(oauth.OAuthError):
                browser.authorize_with_browser(CLIENT, timeout_seconds=timeout,
                                               open_browser=False)


if __name__ == "__main__":
    unittest.main()
