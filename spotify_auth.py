import argparse
import base64
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse, urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
]


def build_authorize_url() -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "show_dialog": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def save_env_variable(key: str, value: str) -> None:
    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w", encoding="utf-8") as env_file:
            env_file.write(f"{key}={value}\n")
        return

    with open(ENV_PATH, "r", encoding="utf-8") as env_file:
        lines = env_file.readlines()

    normalized = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}\n"
            normalized = True
            break

    if not normalized:
        lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as env_file:
        env_file.writelines(lines)


class SpotifyAuthHandler(BaseHTTPRequestHandler):
    def send_html(self, html: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        error = query.get("error", [None])[0]

        if error:
            message = f"Spotify authorization failed: {error}"
            self.server.auth_error = message
            self.server.auth_done = True
            self.send_html(f"<h1>Authorization failed</h1><p>{message}</p>", status=400)
            print(message)
            self.server.shutdown()
            return

        if not code:
            self.send_html(
                "<h1>No code received</h1><p>Waiting for Spotify to redirect with the authorization code.</p>",
                status=200,
            )
            print(f"Ignored request at {self.path}; waiting for authorization code.")
            return

        try:
            token_data = exchange_code_for_token(code)
            refresh_token = token_data.get("refresh_token")
            if not refresh_token:
                raise ValueError("Spotify did not return a refresh token.")

            self.server.auth_result = refresh_token
            self.server.auth_done = True
            save_env_variable("SPOTIFY_REFRESH_TOKEN", refresh_token)
            message = (
                "Spotify authorization succeeded!<br>"
                "Your refresh token was saved to <code>.env</code>.<br>"
                "You can close this page and return to the terminal."
            )
            self.send_html(f"<h1>Success</h1><p>{message}</p>")
            print("Spotify authorization succeeded. Refresh token saved to .env.")
            self.server.shutdown()
        except Exception as exc:
            message = f"Failed to exchange code for token: {exc}"
            self.server.auth_error = message
            self.server.auth_done = True
            self.send_html(f"<h1>Token exchange failed</h1><p>{message}</p>", status=500)
            print(message)

    def log_message(self, format: str, *args: object) -> None:
        return


def exchange_code_for_token(code: str) -> dict:
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    response = requests.post(TOKEN_URL, data=data, headers=headers)
    response.raise_for_status()
    return response.json()


def refresh_access_token(refresh_token: str) -> dict:
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    response = requests.post(TOKEN_URL, data=data, headers=headers)
    response.raise_for_status()
    return response.json()


def parse_redirect_host() -> tuple[str, int, str]:
    parsed = urlparse(REDIRECT_URI)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("SPOTIFY_REDIRECT_URI must use http or https.")
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    return host, port, path


def run_local_server() -> None:
    host, port, path = parse_redirect_host()
    server_address = (host, port)
    handler = SpotifyAuthHandler
    try:
        with HTTPServer(server_address, handler) as httpd:
            httpd.auth_result = None
            httpd.auth_error = None
            httpd.auth_done = False
            print(f"Waiting for Spotify callback at {REDIRECT_URI}...")
            while not httpd.auth_done:
                httpd.handle_request()
            if httpd.auth_error:
                print(httpd.auth_error)
                sys.exit(1)
    except OSError as exc:
        print(f"Could not start local server on {host}:{port}: {exc}")
        print("If the callback cannot reach localhost, rerun with --manual and paste the code manually.")
        sys.exit(1)


def prompt_for_code() -> str:
    code = input("Paste the Spotify authorization code: ").strip()
    if not code:
        print("No code entered. Exiting.")
        sys.exit(1)
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description="Spotify OAuth authorization helper")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Skip local callback and paste the Spotify authorization code manually.",
    )
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        print(
            "Missing Spotify configuration. Add SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, "
            "and SPOTIFY_REDIRECT_URI to your .env file."
        )
        sys.exit(1)

    if os.getenv("SPOTIFY_REFRESH_TOKEN"):
        print("SPOTIFY_REFRESH_TOKEN already exists in .env.")
        print("If you want to refresh it, remove the existing value and rerun this script.")
        return

    auth_url = build_authorize_url()
    print("Opening the Spotify authorization page in your browser...")
    print(auth_url)
    webbrowser.open(auth_url, new=2)

    parsed = urlparse(REDIRECT_URI)
    if args.manual or (parsed.hostname and parsed.hostname not in {"localhost", "127.0.0.1"}):
        print(
            "After authorizing, paste the code parameter from the redirect URL below."
        )
        code = prompt_for_code()
        token_data = exchange_code_for_token(code)
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise ValueError("Spotify did not return a refresh token.")
        save_env_variable("SPOTIFY_REFRESH_TOKEN", refresh_token)
        print("Spotify authorization succeeded. Refresh token saved to .env.")
        return

    run_local_server()


if __name__ == "__main__":
    main()
