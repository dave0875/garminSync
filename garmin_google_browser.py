"""Local-only browser authorization for Garmin Sync Google Drive onboarding.

The callback binds exclusively to loopback, uses a one-shot PKCE/state grant,
never logs authorization codes, and has a bounded expiry. No hosted broker.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
import webbrowser

import garmin_google_oauth as oauth
import garmin_google_picker as picker


def authorize_with_browser(client_id, *, timeout_seconds=180, open_browser=True,
                           choose_folder=False):
    """Return an OAuth response to a *local* caller, never to the browser page.

    The returned payload contains secrets. Caller must immediately hand it to
    a protected credential store and must not log/print it.
    """
    if not 1 <= timeout_seconds <= 600:
        raise oauth.OAuthError("OAuth callback lifetime must be between 1 and 600 seconds")
    # Bind before building redirect_uri so the authorization URL has the actual
    # random OS-assigned port. Never bind 0.0.0.0 or a public interface.
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        port = server.server_address[1]
        attempt = (picker.folder_picker_attempt(client_id, port) if choose_folder
                   else oauth.new_authorization(client_id, port))
        received = []
        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                if (self.client_address[0] != "127.0.0.1" or
                        self.headers.get("Host") != f"127.0.0.1:{port}"):
                    self.send_error(400, "Untrusted loopback request")
                    return
                if received:
                    self.send_error(409, "Authorization already completed")
                    return
                try:
                    grant = (picker.consume_folder_picker_callback(attempt, self.path)
                             if choose_folder else (oauth.consume_callback(attempt, self.path), None))
                except oauth.OAuthError:
                    self.send_error(400, "Google authorization could not be accepted")
                    return
                received.append(grant)
                self.send_response(200)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Google authorization received. Return to Garmin Sync.")

            def log_message(self, *_):
                # An HTTP request target contains an OAuth code: never log it.
                pass

        server.RequestHandlerClass = Callback
        server.timeout = 0.5
        if open_browser and not webbrowser.open(attempt.url):
            raise oauth.OAuthError("Could not open the default browser for Google sign-in")
        deadline = time.monotonic() + timeout_seconds
        while not received and time.monotonic() < deadline:
            server.handle_request()
        if not received:
            raise oauth.OAuthError("Google authorization timed out")
        # Code exchange runs only after the authorization state is checked.
        code, folder_id = received.pop()
        token = oauth.exchange_code(attempt, code)
        return (token, folder_id) if choose_folder else token
    finally:
        server.server_close()
