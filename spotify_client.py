import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_ID_FILE = os.path.join(HERE, ".spotify_client_id")
TOKEN_CACHE_FILE = os.path.join(HERE, ".spotify_token_cache.json")
SP_DC_FILE = os.path.join(HERE, ".spotify_sp_dc")
INTERNAL_TOKEN_CACHE_FILE = os.path.join(HERE, ".spotify_internal_token_cache.json")

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

OPEN_SPOTIFY_TOKEN_URL = "https://open.spotify.com/get_access_token"
LYRICS_API_BASE = "https://spclient.wg.spotify.com/color-lyrics/v2/track"


# -------------------------------------------------
# LOCAL SECRETS (read a cached value from disk, or prompt for it once)
# -------------------------------------------------
def _get_or_prompt_secret(file_path, prompt_lines, prompt_text):
    if os.path.exists(file_path):
        with open(file_path) as f:
            value = f.read().strip()
        if value:
            return value

    for line in prompt_lines:
        print(line)
    value = input(prompt_text).strip()
    with open(file_path, "w") as f:
        f.write(value)

    return value


def get_client_id():
    return _get_or_prompt_secret(CLIENT_ID_FILE, [], "Enter your Spotify app's Client ID: ")


# -------------------------------------------------
# JSON TOKEN CACHES (OAuth token + the internal web-player token below)
# -------------------------------------------------
def _load_json_cache(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_json_cache(path, lock, data):
    with lock:
        with open(path, "w") as f:
            json.dump(data, f)


# -------------------------------------------------
# PKCE LOGIN
# -------------------------------------------------
def _generate_pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("utf-8").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).decode("utf-8").rstrip("=")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        self.server.auth_code = params.get("code", [None])[0]
        self.server.auth_error = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        message = (
            "Login successful — you can close this tab."
            if self.server.auth_code
            else "Login failed — you can close this tab."
        )
        self.wfile.write(f"<html><body><h2>{message}</h2></body></html>".encode("utf-8"))

    def log_message(self, format, *args):
        pass


def _wait_for_callback():
    server = http.server.HTTPServer(("127.0.0.1", 8888), _CallbackHandler)
    server.auth_code = None
    server.auth_error = None
    server.timeout = 120

    server.handle_request()
    server.server_close()

    return server.auth_code, server.auth_error


_token_lock = threading.Lock()


def _save_token(token_data):
    _save_json_cache(TOKEN_CACHE_FILE, _token_lock, token_data)


def _load_token():
    return _load_json_cache(TOKEN_CACHE_FILE)


def _login(client_id):
    verifier, challenge = _generate_pkce_pair()

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print("Opening your browser to log in to Spotify...")
    webbrowser.open(auth_url)

    code, error = _wait_for_callback()

    if error or not code:
        raise RuntimeError(f"Spotify login failed: {error or 'no code returned'}")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=10,
    )
    resp.raise_for_status()

    token_data = resp.json()
    token_data["obtained_at"] = time.time()
    token_data["client_id"] = client_id
    _save_token(token_data)

    return token_data


def _refresh(token_data):
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
            "client_id": token_data["client_id"],
        },
        timeout=10,
    )
    resp.raise_for_status()

    new_data = resp.json()
    new_data.setdefault("refresh_token", token_data["refresh_token"])
    new_data["obtained_at"] = time.time()
    new_data["client_id"] = token_data["client_id"]
    _save_token(new_data)

    return new_data


def get_access_token():
    token_data = _load_token()

    if token_data is None:
        token_data = _login(get_client_id())

    expires_in = token_data.get("expires_in", 3600)
    obtained_at = token_data.get("obtained_at", 0)

    if time.time() >= obtained_at + expires_in - 60:
        token_data = _refresh(token_data)

    return token_data["access_token"]


# -------------------------------------------------
# PLAYER API
# -------------------------------------------------
def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def list_devices(token):
    resp = requests.get(f"{API_BASE}/me/player/devices", headers=_auth_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json().get("devices", [])


def start_playback(token, device_id, track_uri):
    resp = requests.put(
        f"{API_BASE}/me/player/play",
        params={"device_id": device_id},
        headers={**_auth_headers(token), "Content-Type": "application/json"},
        json={"uris": [track_uri]},
        timeout=10,
    )
    if resp.status_code not in (200, 202, 204):
        raise RuntimeError(f"Failed to start playback ({resp.status_code}): {resp.text}")


def pause_playback(token):
    requests.put(f"{API_BASE}/me/player/pause", headers=_auth_headers(token), timeout=10)


def resume_playback(token):
    requests.put(f"{API_BASE}/me/player/play", headers=_auth_headers(token), timeout=10)


def seek_playback(token, position_ms):
    requests.put(
        f"{API_BASE}/me/player/seek",
        params={"position_ms": position_ms},
        headers=_auth_headers(token),
        timeout=10,
    )


def get_playback_state(token):
    resp = requests.get(f"{API_BASE}/me/player", headers=_auth_headers(token), timeout=10)
    if resp.status_code != 200 or not resp.content:
        return None
    return resp.json()


# -------------------------------------------------
# BACKGROUND POSITION TRACKER
# -------------------------------------------------
class PlaybackTracker:
    """Polls real Spotify playback position in the background and lets the
    UI thread read a smoothly-interpolated 'current position' every frame."""

    def __init__(self, poll_interval=1.5):
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._progress_ms = 0
        self._duration_ms = 0
        self._is_playing = False
        self._last_poll_monotonic = time.monotonic()
        self._stop_flag = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag = True

    def _poll_loop(self):
        while not self._stop_flag:
            try:
                token = get_access_token()
                state = get_playback_state(token)

                if state:
                    with self._lock:
                        self._progress_ms = state.get("progress_ms") or 0
                        self._is_playing = bool(state.get("is_playing"))
                        item = state.get("item") or {}
                        self._duration_ms = item.get("duration_ms") or self._duration_ms
                        self._last_poll_monotonic = time.monotonic()
            except Exception:
                pass

            time.sleep(self.poll_interval)

    def get_position_seconds(self):
        with self._lock:
            progress_ms = self._progress_ms
            is_playing = self._is_playing
            last_poll = self._last_poll_monotonic

        if is_playing:
            progress_ms += (time.monotonic() - last_poll) * 1000

        return progress_ms / 1000, is_playing

    def get_duration_seconds(self):
        with self._lock:
            return self._duration_ms / 1000

    def force_position(self, seconds):
        """Immediately reflect a local seek in the interpolated position,
        instead of waiting up to poll_interval seconds for confirmation."""
        with self._lock:
            self._progress_ms = seconds * 1000
            self._last_poll_monotonic = time.monotonic()


# -------------------------------------------------
# SPOTIFY'S OWN LYRICS (private/undocumented endpoint, Premium accounts only)
# -------------------------------------------------
# There is no lyrics endpoint in Spotify's public Web API. This talks to the
# same private endpoint the open.spotify.com web player itself uses, which
# needs the browser session's 'sp_dc' cookie rather than the OAuth token used
# everywhere else in this file. It can change or stop working at any time
# since it's not a supported public API.
def get_sp_dc():
    return _get_or_prompt_secret(
        SP_DC_FILE,
        [
            "Spotify's own lyrics require your 'sp_dc' session cookie (Premium account,",
            "logged into open.spotify.com). In your browser's dev tools > Application/Storage",
            "> Cookies for open.spotify.com, copy the 'sp_dc' cookie's value.",
        ],
        "sp_dc cookie: ",
    )


_internal_token_lock = threading.Lock()


def _load_internal_token():
    return _load_json_cache(INTERNAL_TOKEN_CACHE_FILE)


def _save_internal_token(token_data):
    _save_json_cache(INTERNAL_TOKEN_CACHE_FILE, _internal_token_lock, token_data)


def _fetch_internal_token_via_browser(sp_dc):
    """Fallback for get_internal_web_token(): Spotify now hard-blocks direct
    HTTP calls to the old get_access_token endpoint at the CDN (403 'URL
    Blocked'), and the web player's current endpoint (/api/token) requires a
    TOTP-signed request generated by Spotify's own obfuscated JS. Rather than
    reverse-engineering that signing scheme, drive a real headless Chromium
    to open.spotify.com with the sp_dc cookie set, and capture the token
    response the page's own JS produces — indistinguishable from a real user
    since it genuinely is Spotify's own front-end making the call."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
                )
            )
            context.add_cookies([
                {"name": "sp_dc", "value": sp_dc, "domain": ".spotify.com", "path": "/"}
            ])
            page = context.new_page()
            try:
                with page.expect_response(
                    lambda r: "open.spotify.com/api/token" in r.url, timeout=20000
                ) as resp_info:
                    page.goto("https://open.spotify.com/", wait_until="load", timeout=30000)
                response = resp_info.value
            except PlaywrightError as exc:
                raise RuntimeError(
                    "Couldn't capture a token request from Spotify's web player — "
                    "its page structure may have changed."
                ) from exc

            if response.status != 200:
                raise RuntimeError(f"Browser-based token fetch failed ({response.status}).")
            return response.json()
        finally:
            browser.close()


def get_internal_web_token():
    """Access token for Spotify's internal web-player endpoints — distinct
    from the public Web API OAuth token — derived from the sp_dc cookie."""
    token_data = _load_internal_token()
    expires_at = (token_data or {}).get("accessTokenExpirationTimestampMs", 0) / 1000

    if token_data and time.time() < expires_at - 30:
        return token_data["accessToken"]

    sp_dc = get_sp_dc()

    try:
        resp = requests.get(
            OPEN_SPOTIFY_TOKEN_URL,
            params={"reason": "transport", "productType": "web_player"},
            cookies={"sp_dc": sp_dc},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        token_data = resp.json()
    except requests.HTTPError:
        token_data = _fetch_internal_token_via_browser(sp_dc)

    if token_data.get("isAnonymous", True):
        raise RuntimeError(
            "Spotify rejected the sp_dc cookie (expired or invalid) — "
            f"delete {os.path.basename(SP_DC_FILE)} and paste a fresh one."
        )

    _save_internal_token(token_data)
    return token_data["accessToken"]


def get_spotify_lyrics(track_id):
    """Fetch this track's own lyrics straight from Spotify as
    [(timestamp_sec, text), ...], or None if unavailable (no lyrics for this
    track, not line-synced, non-Premium account, etc.)."""
    token = get_internal_web_token()
    resp = requests.get(
        f"{LYRICS_API_BASE}/{track_id}",
        params={"format": "json", "market": "from_token"},
        headers={
            "Authorization": f"Bearer {token}",
            "App-platform": "WebPlayer",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=10,
    )

    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    lyrics = resp.json().get("lyrics") or {}
    if lyrics.get("syncType") != "LINE_SYNCED":
        return None

    lines = [
        (int(line.get("startTimeMs", 0)) / 1000, line.get("words", ""))
        for line in lyrics.get("lines", [])
    ]
    lines.sort(key=lambda pair: pair[0])
    return lines
