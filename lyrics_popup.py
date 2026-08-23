import bisect
import colorsys
import ctypes
import html
import io
import json
import math
import os
import random
import re
import sys
import threading
import time

import numpy as np
import pygame
import requests
import syncedlyrics

import spotify_client

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes.wintypes

# -------------------------------------------------
# SETTINGS
# -------------------------------------------------
# Placeholder size — replaced with the actual screen resolution at startup (see run_popup)
WIDTH, HEIGHT = 900, 600
FPS = 60

# How many seconds before a line's timestamp it's allowed to start easing into view
PREROLL_SEC = 2.0

# How long each card's own slide+fade animation takes, independent of the gap to the
# next line — a new card spawns the instant its line's timestamp arrives regardless
# of whether earlier cards are still on screen, so pacing follows the song directly
# and multiple cards can be visible at once during fast-paced lyrics
CARD_ANIMATION_DURATION = 4.5

# Cap on how many cards can be alive at once — the oldest is retired early if a new
# line would push past this, but eased into a quick finish rather than cut instantly
MAX_ACTIVE_CARDS = 2
RETIRE_FADE_SEC = 0.4

# Top band of the screen (as a fraction of height) where the card fades out as it arrives
CARD_FADE_ZONE_FRAC = 0.16

# Typewriter reveal speed for the card text (milliseconds per character)
TYPE_MS_PER_CHAR = 45

# Card look
CARD_COLOR = (255, 0, 0)
CARD_AGED_COLOR = (78, 78, 82)  # what the card fades toward as it ages
CARD_BORDER_COLOR = (245, 245, 245)
CARD_CORNER_RADIUS = 20
CARD_MAX_TEXT_WIDTH = 260
CARD_MIN_WIDTH = 320
CARD_MIN_HEIGHT = 260

# Fraction of a card's lifetime over which it desaturates from CARD_COLOR to CARD_AGED_COLOR
CARD_AGE_START_FRAC = 0.15
CARD_AGE_END_FRAC = 0.65

# How much UP/DOWN nudges the sync offset per press, in seconds
OFFSET_STEP = 0.1

HERE = os.path.dirname(os.path.abspath(__file__))
OFFSET_FILE = os.path.join(HERE, ".lyrics_offset.json")

# -------------------------------------------------
# COLORS
# -------------------------------------------------
PANEL = (28, 28, 38)
BORDER = (110, 30, 45)

WHITE = (230, 230, 230)
DIM = (110, 110, 120)
RED = (200, 40, 60)
DARK_RED = (100, 20, 30)

SNOW_COLOR = (235, 240, 250)
LEAF_COLORS = [(196, 88, 42), (214, 138, 46), (168, 60, 40), (222, 168, 60)]
NOTE_COLORS = [(210, 45, 65), (235, 95, 105)]
NOTE_GLYPHS = ["♪", "♫"]

WEATHER_MODES = ["notes", "snow", "leaves", "off"]


def load_offset():
    if not os.path.exists(OFFSET_FILE):
        return 0.0
    try:
        with open(OFFSET_FILE) as f:
            return json.load(f).get("offset_sec", 0.0)
    except Exception:
        return 0.0


def save_offset(offset_sec):
    try:
        with open(OFFSET_FILE, "w") as f:
            json.dump({"offset_sec": offset_sec}, f)
    except Exception:
        pass


# -------------------------------------------------
# SPOTIFY METADATA (no API key needed for this part)
# -------------------------------------------------
def resolve_spotify_track(url):
    """Pull track title, artist, and cover art URL from a Spotify track link."""
    match = re.search(r"track/([a-zA-Z0-9]+)", url)
    if not match:
        raise ValueError("That doesn't look like a Spotify track link.")
    track_id = match.group(1)

    page_url = f"https://open.spotify.com/track/{track_id}"
    resp = requests.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    page_html = resp.text

    title_match = re.search(r'property="og:title" content="([^"]+)"', page_html)
    desc_match = re.search(r'property="og:description" content="([^"]+)"', page_html)
    image_match = re.search(r'property="og:image" content="([^"]+)"', page_html)

    if not title_match:
        raise ValueError("Couldn't read track info from that link.")

    title = html.unescape(title_match.group(1))
    artist = html.unescape(desc_match.group(1).split(" · ")[0]) if desc_match else ""
    art_url = image_match.group(1) if image_match else None

    return title, artist, track_id, art_url


def download_album_art(art_url):
    if not art_url:
        return None
    try:
        resp = requests.get(art_url, timeout=10)
        resp.raise_for_status()
        return pygame.image.load(io.BytesIO(resp.content)).convert()
    except Exception:
        return None


# -------------------------------------------------
# LYRICS (LRC parsing)
# -------------------------------------------------
LRC_TIME_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def fetch_synced_lyrics(title, artist):
    query = f"{title} {artist}".strip()
    lrc = syncedlyrics.search(query)

    if not lrc:
        return None

    lines = []
    for raw_line in lrc.splitlines():
        times = LRC_TIME_RE.findall(raw_line)
        if not times:
            continue

        text = LRC_TIME_RE.sub("", raw_line).strip()

        for minutes, seconds in times:
            timestamp = int(minutes) * 60 + float(seconds)
            lines.append((timestamp, text))

    lines.sort(key=lambda pair: pair[0])
    return lines


# -------------------------------------------------
# SPOTIFY PLAYBACK CONTROL
# -------------------------------------------------
def pick_device(token, wait_seconds=30):
    """Return an active (or first available) device id, waiting for the
    user to open Spotify somewhere if nothing is currently available."""
    waited = 0
    printed_prompt = False

    while waited <= wait_seconds:
        devices = spotify_client.list_devices(token)

        if devices:
            active = next((d for d in devices if d.get("is_active")), None)
            chosen = active or devices[0]
            return chosen["id"]

        if not printed_prompt:
            print("No Spotify device found. Open Spotify on your phone or computer...")
            printed_prompt = True

        time.sleep(3)
        waited += 3

    return None


def _fire_and_forget(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# -------------------------------------------------
# VISUAL HELPERS
# -------------------------------------------------
def extract_accent_color(art_surface):
    """Average the art's color, then re-saturate it to a fixed, legible vividness."""
    if art_surface is None:
        return RED

    tiny = pygame.transform.smoothscale(art_surface, (8, 8))
    r_total = g_total = b_total = 0
    count = 0

    for x in range(8):
        for y in range(8):
            r, g, b, *_ = tiny.get_at((x, y))
            r_total += r
            g_total += g
            b_total += b
            count += 1

    avg_r, avg_g, avg_b = r_total / count / 255, g_total / count / 255, b_total / count / 255
    hue, _, _ = colorsys.rgb_to_hsv(avg_r, avg_g, avg_b)

    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.80)
    return (int(r * 255), int(g * 255), int(b * 255))


def lerp_color(color_a, color_b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a + (b - a) * t) for a, b in zip(color_a, color_b))


GLOW_TEXT_OFFSETS = [(-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, -2), (-2, 2), (2, 2)]


def blit_glow_text(surface, font, text, pos, text_color, glow_color, glow_alpha=110):
    """Draw text with a soft glowing halo (cheap poor-man's blur via offset copies)
    behind the crisp main text, anchored at `pos` (midleft)."""
    glow_surf = font.render(text, True, glow_color)
    glow_surf.set_alpha(glow_alpha)
    for dx, dy in GLOW_TEXT_OFFSETS:
        surface.blit(glow_surf, glow_surf.get_rect(midleft=(pos[0] + dx, pos[1] + dy)))

    main_surf = font.render(text, True, text_color)
    main_rect = main_surf.get_rect(midleft=pos)
    surface.blit(main_surf, main_rect)
    return main_rect


def build_card_glow_surface(card_w, card_h, color, alpha_scale=1.0):
    """Build a compact drop-shadow + glow-ring halo sized to an unrotated card_w x
    card_h box, as its own Surface — so the caller can rotate it together with the
    card to keep the glow aligned with its tilt."""
    rings = ((14, 20), (9, 40), (4, 65))
    pad = rings[0][0] + 10

    surf_w, surf_h = card_w + pad * 2, card_h + pad * 2
    glow_surf = pygame.Surface((surf_w, surf_h), pygame.SRCALPHA)
    center = (surf_w // 2, surf_h // 2)

    shadow = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
    pygame.draw.rect(shadow, (0, 0, 0, int(110 * alpha_scale)), (0, 0, card_w, card_h), border_radius=CARD_CORNER_RADIUS)
    glow_surf.blit(shadow, shadow.get_rect(center=(center[0] + 4, center[1] + 6)))

    for expand, alpha in rings:
        ring_w, ring_h = card_w + expand * 2, card_h + expand * 2
        ring_surf = pygame.Surface((ring_w, ring_h), pygame.SRCALPHA)
        pygame.draw.rect(
            ring_surf, (*color, int(alpha * alpha_scale)), (0, 0, ring_w, ring_h),
            border_radius=CARD_CORNER_RADIUS + expand // 2,
        )
        glow_surf.blit(ring_surf, ring_surf.get_rect(center=center))

    return glow_surf


def smoothstep(t):
    """Ease-in-out curve (0 -> 1) so motion accelerates then decelerates instead of
    moving at constant speed."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


# -------------------------------------------------
# MULTI-SCRIPT TEXT RENDERING
# -------------------------------------------------
# Consolas (the card font) only has glyphs for Latin/Greek/Cyrillic, so lyrics
# in CJK, Hangul, Arabic, Thai, Devanagari, etc. would otherwise render as
# empty tofu boxes. Map Unicode blocks to Windows fonts that actually have
# those glyphs, and fall back to those on a per-character basis.
SCRIPT_FONT_NAMES = {
    "cjk": ["Microsoft YaHei", "Microsoft JhengHei", "SimSun", "MS Gothic"],
    "japanese_kana": ["Yu Gothic", "MS Gothic", "Meiryo"],
    "korean": ["Malgun Gothic", "Gulim", "Batang"],
    "arabic": ["Segoe UI", "Tahoma", "Arial"],
    "hebrew": ["Segoe UI", "Tahoma", "Arial"],
    "thai": ["Leelawadee UI", "Tahoma"],
    "devanagari": ["Nirmala UI", "Mangal"],
    "indic": ["Nirmala UI"],
    "myanmar": ["Myanmar Text", "Padauk"],
    "armenian_georgian": ["Segoe UI"],
    "default": ["Consolas"],
}


def _char_script_key(ch):
    cp = ord(ch)
    if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
        return "cjk"
    if 0x3040 <= cp <= 0x30FF:
        return "japanese_kana"
    if 0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF:
        return "korean"
    if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
        return "arabic"
    if 0x0590 <= cp <= 0x05FF:
        return "hebrew"
    if 0x0E00 <= cp <= 0x0E7F:
        return "thai"
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if 0x0980 <= cp <= 0x0DFF:  # Bengali through Sinhala
        return "indic"
    if 0x1000 <= cp <= 0x109F or 0xA9E0 <= cp <= 0xA9FF or 0xAA60 <= cp <= 0xAA7F:
        return "myanmar"
    if 0x0530 <= cp <= 0x10FF:  # Armenian + Georgian
        return "armenian_georgian"
    return "default"


class MultiScriptFont:
    """Drop-in replacement for a pygame Font that picks a fallback font per
    character based on Unicode script, so a single lyric line can mix Latin,
    CJK, Hangul, Arabic, Thai, Devanagari, etc. and still render correctly.
    Exposes the render()/size()/get_height() subset of the Font API that the
    rest of this file actually uses."""

    def __init__(self, size, bold=False):
        self.size_px = size
        self.bold = bold
        self._fonts = {}
        self._default = self._get_font("default")

    def _get_font(self, script_key):
        font = self._fonts.get(script_key)
        if font is not None:
            return font

        names = ",".join(SCRIPT_FONT_NAMES.get(script_key, SCRIPT_FONT_NAMES["default"]))
        match_path = pygame.font.match_font(names, bold=self.bold)
        if match_path:
            font = pygame.font.Font(match_path, self.size_px)
        else:
            font = pygame.font.SysFont("consolas", self.size_px, bold=self.bold)

        self._fonts[script_key] = font
        return font

    def _font_for_char(self, ch):
        if ch.isspace():
            return self._default
        return self._get_font(_char_script_key(ch))

    def _split_runs(self, text):
        runs = []
        current_font, current_text = None, ""

        for ch in text:
            font = self._font_for_char(ch)
            if font is current_font:
                current_text += ch
            else:
                if current_text:
                    runs.append((current_font, current_text))
                current_font, current_text = font, ch

        if current_text:
            runs.append((current_font, current_text))

        return runs or [(self._default, "")]

    def size(self, text):
        if not text:
            return self._default.size(text)
        width = sum(font.size(chunk)[0] for font, chunk in self._split_runs(text))
        return width, self.get_height()

    def get_height(self):
        return self._default.get_height()

    def render(self, text, antialias, color):
        runs = self._split_runs(text)
        chunk_surfs = [(font, font.render(chunk, antialias, color)) for font, chunk in runs if chunk]

        if not chunk_surfs:
            return self._default.render("", antialias, color)

        common_ascent = max(font.get_ascent() for font, _ in chunk_surfs)
        common_descent = min(font.get_descent() for font, _ in chunk_surfs)
        height = common_ascent - common_descent
        total_width = sum(surf.get_width() for _, surf in chunk_surfs)

        composite = pygame.Surface((total_width, height), pygame.SRCALPHA)
        x = 0
        for font, surf in chunk_surfs:
            y = common_ascent - font.get_ascent()
            composite.blit(surf, (x, y))
            x += surf.get_width()

        return composite


def wrap_text(font, text, max_width):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        trial = f"{current} {word}".strip()
        if font.size(trial)[0] <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


class LyricCard:
    """One independently-animating popup for a single lyric line. Its whole
    animation (slide + fade + typewriter) is driven purely by how much time has
    passed since `timestamp`, so any number of these can be alive at once."""

    def __init__(self, text, timestamp, duration):
        self.text = text
        self.timestamp = timestamp
        self.duration = duration
        # Scatter each card a little so simultaneous cards don't stack in one column
        self.x_offset = random.uniform(-0.16, 0.16) * WIDTH
        self.rotation = random.uniform(-6, 6)
        # Set when retired early (cap on simultaneous cards) so it finishes smoothly
        # from wherever it currently is, instead of jumping or cutting instantly
        self.retiring_since = None
        self.retiring_start_progress = None

        # Ambient floating motion, layered on top of the main bottom-to-top slide —
        # each card gets its own phase/speed/amount so several on screen never move
        # in lockstep
        self.float_phase = random.uniform(0, math.tau)
        self.float_speed = random.uniform(0.6, 1.0)
        self.float_amount = random.uniform(3, 6)

        self.bob_phase = random.uniform(0, math.tau)
        self.bob_speed = random.uniform(0.4, 0.8)
        self.bob_amount = random.uniform(1, 3)

        self.rotation_phase = random.uniform(0, math.tau)
        self.rotation_speed = random.uniform(0.3, 0.7)
        self.rotation_sway = random.uniform(0.5, 1.5)

        # One-shot overshoot that decays away during the card's first ~12% of
        # life, so it settles into its ambient float instead of starting stiff
        self.entrance_sign = random.choice((-1, 1))

    def retire(self, synced_elapsed):
        if self.retiring_since is not None:
            return
        elapsed_so_far = max(0.0, synced_elapsed - self.timestamp)
        self.retiring_since = synced_elapsed
        self.retiring_start_progress = min(1.0, elapsed_so_far / self.duration)


def draw_lyric_card(canvas, card, synced_elapsed, active_font):
    """Render one LyricCard at whatever point in its slide/fade/typewriter
    animation it's currently at. Returns its on-screen Rect (for click-to-seek
    hit-testing), or None if it's fully faded out."""
    if card.retiring_since is not None:
        # Continue smoothly from wherever it already was, finishing quickly
        t = min(1.0, (synced_elapsed - card.retiring_since) / RETIRE_FADE_SEC)
        card_progress = card.retiring_start_progress + (1.0 - card.retiring_start_progress) * t
        card_elapsed = card.duration  # text stays fully revealed while retiring
    else:
        card_elapsed = max(0.0, synced_elapsed - card.timestamp)
        card_progress = min(1.0, card_elapsed / card.duration)

    quoted_text = f"“{card.text}”"
    lines = wrap_text(active_font, quoted_text, CARD_MAX_TEXT_WIDTH)
    line_surfs = [active_font.render(line, True, WHITE) for line in lines]  # sizing only
    total_chars = sum(len(line) for line in lines)

    pad_x = 32
    line_gap = 6
    handle_zone = 34
    bottom_zone = 26

    text_block_h = sum(s.get_height() for s in line_surfs) + line_gap * (len(line_surfs) - 1)
    card_w = max(max(s.get_width() for s in line_surfs) + pad_x * 2, CARD_MIN_WIDTH)
    card_h = max(handle_zone + text_block_h + bottom_zone, CARD_MIN_HEIGHT)

    # Center the text block in the space between the handle and the bottom accent
    available_middle = card_h - handle_zone - bottom_zone
    top_margin = handle_zone + max(0, (available_middle - text_block_h) / 2)

    # Slide from fully off-screen below (its top edge starting right at the
    # bottom of the screen) to fully off-screen above the top, eased so it
    # accelerates/decelerates instead of moving at constant speed
    card_bottom_y = HEIGHT + card_h / 2
    card_top_y = -card_h / 2
    eased_progress = smoothstep(card_progress)
    card_y = card_bottom_y + (card_top_y - card_bottom_y) * eased_progress

    # Fade out only once it's actually arrived near the top of the screen
    fade_zone_px = HEIGHT * CARD_FADE_ZONE_FRAC
    card_alpha = int(255 * max(0.0, min(1.0, card_y / fade_zone_px))) if card_y < fade_zone_px else 255

    if card_alpha <= 0:
        return None

    # Desaturate from vivid red toward gray as the card ages
    age_t = (card_progress - CARD_AGE_START_FRAC) / (CARD_AGE_END_FRAC - CARD_AGE_START_FRAC)
    card_color = lerp_color(CARD_COLOR, CARD_AGED_COLOR, age_t)

    # Text types in right alongside the popup's own entrance, not delayed behind it
    reveal_count = int((card_elapsed * 1000) / TYPE_MS_PER_CHAR)
    typing_done = reveal_count >= total_chars

    revealed_lines = []
    remaining = reveal_count
    for line in lines:
        if remaining <= 0:
            revealed_lines.append("")
        elif remaining >= len(line):
            revealed_lines.append(line)
            remaining -= len(line)
        else:
            revealed_lines.append(line[:remaining])
            remaining = 0

    card_surf = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
    pygame.draw.rect(card_surf, (*card_color, 255), (0, 0, card_w, card_h), border_radius=CARD_CORNER_RADIUS)

    # Glossy highlight across the top half
    highlight_h = int(card_h * 0.45)
    highlight = pygame.Surface((card_w, highlight_h), pygame.SRCALPHA)
    pygame.draw.rect(
        highlight, (255, 255, 255, 35), (0, 0, card_w, highlight_h),
        border_top_left_radius=CARD_CORNER_RADIUS, border_top_right_radius=CARD_CORNER_RADIUS,
    )
    card_surf.blit(highlight, (0, 0))

    pygame.draw.rect(card_surf, (*CARD_BORDER_COLOR, 255), (0, 0, card_w, card_h), 3, border_radius=CARD_CORNER_RADIUS)

    # Top drag-handle marks
    pygame.draw.rect(card_surf, CARD_BORDER_COLOR, (card_w // 2 - 20, 14, 14, 4), border_radius=2)
    pygame.draw.rect(card_surf, CARD_BORDER_COLOR, (card_w // 2 + 6, 14, 14, 4), border_radius=2)

    # Bottom-left accent square
    pygame.draw.rect(card_surf, CARD_BORDER_COLOR, (16, card_h - 26, 12, 12), border_radius=2)

    y_cursor = top_margin
    cursor_pos = None
    for full_surf, revealed_line in zip(line_surfs, revealed_lines):
        # Left edge the full (centered) line would start at — keeps revealed
        # text growing left-to-right in place instead of re-centering per letter
        start_x = card_w // 2 - full_surf.get_width() // 2
        mid_y = y_cursor + full_surf.get_height() // 2

        if revealed_line:
            revealed_rect = blit_glow_text(
                card_surf, active_font, revealed_line, (start_x, mid_y), WHITE, card_color
            )
            cursor_pos = (revealed_rect.right + 2, mid_y)
        elif cursor_pos is None:
            cursor_pos = (start_x, mid_y)

        y_cursor += full_surf.get_height() + line_gap

    if not typing_done and cursor_pos is not None:
        blink_alpha = int(200 * ((math.sin(pygame.time.get_ticks() / 150) + 1) / 2))
        cursor_h = active_font.get_height() - 6
        cursor_rect = pygame.Rect(0, 0, 2, cursor_h)
        cursor_rect.midleft = cursor_pos
        cursor_surf = pygame.Surface(cursor_rect.size, pygame.SRCALPHA)
        cursor_surf.fill((*WHITE, blink_alpha))
        card_surf.blit(cursor_surf, cursor_rect)

    glow_surf = build_card_glow_surface(card_w, card_h, card_color, alpha_scale=card_alpha / 255)

    # Subtle ambient float/bob/sway on top of the main slide — each card has its
    # own phase so multiple cards never move identically
    motion_time = pygame.time.get_ticks() / 1000.0
    float_x = math.sin(motion_time * card.float_speed + card.float_phase) * card.float_amount
    bob_y = math.sin(motion_time * card.bob_speed + card.bob_phase) * card.bob_amount
    rotation_offset = math.sin(motion_time * card.rotation_speed + card.rotation_phase) * card.rotation_sway

    # Entrance settle: a tiny sideways/rotation overshoot that decays away over
    # the card's first ~12% of life, so it eases into the ambient motion above
    # instead of starting stiff
    entrance_t = min(1.0, card_progress / 0.12)
    settle_decay = (1.0 - entrance_t) ** 2
    float_x += settle_decay * 7 * card.entrance_sign
    rotation_offset += settle_decay * 1.6 * card.entrance_sign

    display_rotation = card.rotation + rotation_offset
    if abs(display_rotation) > 0.01:
        card_surf = pygame.transform.rotate(card_surf, display_rotation)
        glow_surf = pygame.transform.rotate(glow_surf, display_rotation)

    card_surf.set_alpha(card_alpha)

    final_center = (WIDTH // 2 + card.x_offset + float_x, int(card_y + bob_y))
    canvas.blit(glow_surf, glow_surf.get_rect(center=final_center))

    card_rect = card_surf.get_rect(center=final_center)
    canvas.blit(card_surf, card_rect)

    return card_rect if card_alpha > 20 else None


# -------------------------------------------------
# FLOATING PARTICLES (notes / snow / leaves)
# -------------------------------------------------
class WeatherParticle:
    def __init__(self, mode, note_font=None):
        self.mode = mode
        self.x = random.uniform(0, WIDTH)

        if mode == "notes":
            self.y = random.uniform(HEIGHT + 10, HEIGHT + 40)
            self.speed_y = -random.uniform(25, 55)
            self.glyph = random.choice(NOTE_GLYPHS)
            self.color = random.choice(NOTE_COLORS)
            self.note_font = note_font
        elif mode == "snow":
            self.y = random.uniform(-40, -10)
            self.speed_y = random.uniform(25, 55)
            self.size = random.uniform(2, 4)
            self.color = SNOW_COLOR
        else:
            self.y = random.uniform(-40, -10)
            self.speed_y = random.uniform(40, 75)
            self.size = random.uniform(5, 9)
            self.color = random.choice(LEAF_COLORS)

        self.drift_amount = random.uniform(10, 30)
        self.sway_phase = random.uniform(0, math.tau)
        self.sway_speed = random.uniform(0.8, 1.8)
        self.angle = random.uniform(0, 360)
        self.spin = random.uniform(-90, 90)
        self.alpha = random.randint(130, 220)

    def update(self, dt):
        self.y += self.speed_y * dt
        self.x += math.sin(self.sway_phase + pygame.time.get_ticks() / 1000 * self.sway_speed) * self.drift_amount * dt
        self.angle += self.spin * dt

    def offscreen(self):
        if self.mode == "notes":
            return self.y < -30
        return self.y - self.size > HEIGHT

    def draw(self, surface):
        if self.mode == "notes":
            glyph_surf = self.note_font.render(self.glyph, True, self.color)
            glyph_surf.set_alpha(self.alpha)
            rect = glyph_surf.get_rect(center=(self.x, self.y))
            surface.blit(glyph_surf, rect)
            return

        size = int(self.size)

        if self.mode == "snow":
            particle_surf = pygame.Surface((size * 2, size * 2), pygame.SRCALPHA)
            pygame.draw.circle(particle_surf, (*self.color, self.alpha), (size, size), size)
            surface.blit(particle_surf, (self.x - size, self.y - size))
        else:
            particle_surf = pygame.Surface((size * 2, size * 2), pygame.SRCALPHA)
            pygame.draw.ellipse(particle_surf, (*self.color, self.alpha), (0, 0, size * 2, int(size * 1.3)))
            rotated = pygame.transform.rotate(particle_surf, self.angle)
            rect = rotated.get_rect(center=(self.x, self.y))
            surface.blit(rotated, rect)


def set_console_visible(visible):
    """Show/hide the terminal window this script was launched from."""
    if not IS_WINDOWS:
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            SW_HIDE, SW_SHOW = 0, 5
            ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)
    except Exception:
        pass


if IS_WINDOWS:
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", ctypes.c_ubyte),
            ("BlendFlags", ctypes.c_ubyte),
            ("SourceConstantAlpha", ctypes.c_ubyte),
            ("AlphaFormat", ctypes.c_ubyte),
        ]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01
    DIB_RGB_COLORS = 0
    BI_RGB = 0


class LayeredWindow:
    """Pushes a 32-bit ARGB pygame Surface onto a Windows layered window each frame
    via UpdateLayeredWindow, so its per-pixel alpha blends against the real desktop
    instead of pygame's normal (opaque) window presentation."""

    def __init__(self, hwnd, width, height):
        self.hwnd = hwnd
        self.width = width
        self.height = height
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32

        ex_style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)

        self.hdc_screen = self.user32.GetDC(None)
        self.hdc_mem = self.gdi32.CreateCompatibleDC(self.hdc_screen)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # negative = top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        self.ppv_bits = ctypes.c_void_p()
        self.hbitmap = self.gdi32.CreateDIBSection(
            self.hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(self.ppv_bits), None, 0,
        )
        self.old_bitmap = self.gdi32.SelectObject(self.hdc_mem, self.hbitmap)

    def present(self, surface):
        """Convert the surface to premultiplied-alpha BGRA and push it to the window."""
        rgb = pygame.surfarray.array3d(surface)         # (W, H, 3)
        alpha = pygame.surfarray.array_alpha(surface)    # (W, H)

        rgb = np.transpose(rgb, (1, 0, 2))                # (H, W, 3)
        alpha = np.transpose(alpha, (1, 0))                # (H, W)

        alpha16 = alpha.astype(np.uint16)
        premult = (rgb.astype(np.uint16) * alpha16[:, :, None] // 255).astype(np.uint8)

        bgra = np.empty((self.height, self.width, 4), dtype=np.uint8)
        bgra[:, :, 0] = premult[:, :, 2]
        bgra[:, :, 1] = premult[:, :, 1]
        bgra[:, :, 2] = premult[:, :, 0]
        bgra[:, :, 3] = alpha

        data = bgra.tobytes()
        ctypes.memmove(self.ppv_bits, data, len(data))

        size = SIZE(self.width, self.height)
        point_src = ctypes.wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

        self.user32.UpdateLayeredWindow(
            self.hwnd, self.hdc_screen, None, ctypes.byref(size),
            self.hdc_mem, ctypes.byref(point_src), 0, ctypes.byref(blend), ULW_ALPHA,
        )

    def close(self):
        try:
            self.gdi32.SelectObject(self.hdc_mem, self.old_bitmap)
            self.gdi32.DeleteObject(self.hbitmap)
            self.gdi32.DeleteDC(self.hdc_mem)
            self.user32.ReleaseDC(None, self.hdc_screen)
        except Exception:
            pass


# -------------------------------------------------
# WINDOWS WIDGET-MODE HELPERS (frameless / always-on-top / draggable)
# -------------------------------------------------
class WindowController:
    def __init__(self):
        self.enabled = IS_WINDOWS
        self.hwnd = None

        if self.enabled:
            try:
                self.hwnd = pygame.display.get_wm_info()["window"]
                self.user32 = ctypes.windll.user32
            except Exception:
                self.enabled = False

    def apply_topmost(self):
        if not self.enabled:
            return
        HWND_TOPMOST = -1
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        self.user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        )

    def get_cursor_pos(self):
        pt = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def get_window_pos(self):
        rect = ctypes.wintypes.RECT()
        self.user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        return rect.left, rect.top

    def move_to(self, x, y):
        self.user32.MoveWindow(self.hwnd, x, y, WIDTH, HEIGHT, True)


# -------------------------------------------------
# POPUP
# -------------------------------------------------
def run_popup(title, lyric_lines, tracker, art_surface):
    global WIDTH, HEIGHT

    pygame.init()

    desktop_sizes = pygame.display.get_desktop_sizes()
    if desktop_sizes:
        WIDTH, HEIGHT = desktop_sizes[0]

    os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
    screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.NOFRAME)
    pygame.display.set_caption(f"Lyrics — {title}")

    window = WindowController()
    window.apply_topmost()

    layered_window = LayeredWindow(window.hwnd, WIDTH, HEIGHT) if window.enabled else None
    canvas = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)

    pygame.key.set_repeat(300, 50)

    clock = pygame.time.Clock()

    active_font = MultiScriptFont(26, bold=True)
    line_font = pygame.font.SysFont("consolas", 22)
    hint_font = pygame.font.SysFont("consolas", 15)
    note_font = pygame.font.SysFont("consolas", 22, bold=True)

    accent = extract_accent_color(art_surface)

    timestamps = [t for t, _ in lyric_lines]
    offset_sec = load_offset()

    mouse_down_pos = None
    drag_started = False
    drag_offset = (0, 0)
    line_rects = []  # [(pygame.Rect, timestamp_sec), ...] for the previously drawn frame
    DRAG_THRESHOLD_SQ = 6 * 6

    weather_mode_index = 0  # WEATHER_MODES[0] == "notes" by default
    weather_particles = []

    active_cards = []  # LyricCard instances currently animating (possibly several at once)
    next_spawn_index = 0  # index into `timestamps` of the next line still waiting to spawn

    def draw_text_centered(surface_font, text, color, y, alpha=255):
        surf = surface_font.render(text, True, color)
        if alpha < 255:
            surf.set_alpha(alpha)
        rect = surf.get_rect(center=(WIDTH // 2, y))
        canvas.blit(surf, rect)

    set_console_visible(False)

    running = True
    while running:
        dt = clock.tick(FPS) / 1000

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False

                if event.key == pygame.K_SPACE:
                    _, is_playing = tracker.get_position_seconds()
                    token = spotify_client.get_access_token()
                    if is_playing:
                        _fire_and_forget(spotify_client.pause_playback, token)
                    else:
                        _fire_and_forget(spotify_client.resume_playback, token)

                if event.key == pygame.K_r:
                    token = spotify_client.get_access_token()
                    _fire_and_forget(spotify_client.seek_playback, token, 0)

                if event.key == pygame.K_p:
                    weather_mode_index = (weather_mode_index + 1) % len(WEATHER_MODES)
                    weather_particles.clear()

                if event.key == pygame.K_UP:
                    offset_sec += OFFSET_STEP
                    save_offset(offset_sec)

                if event.key == pygame.K_DOWN:
                    offset_sec -= OFFSET_STEP
                    save_offset(offset_sec)

            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mouse_down_pos = event.pos
                drag_started = False

                if window.enabled:
                    cursor_x, cursor_y = window.get_cursor_pos()
                    win_x, win_y = window.get_window_pos()
                    drag_offset = (cursor_x - win_x, cursor_y - win_y)

            if event.type == pygame.MOUSEMOTION and mouse_down_pos is not None:
                dx = event.pos[0] - mouse_down_pos[0]
                dy = event.pos[1] - mouse_down_pos[1]

                if not drag_started and (dx * dx + dy * dy) > DRAG_THRESHOLD_SQ:
                    drag_started = True

                if drag_started and window.enabled:
                    cursor_x, cursor_y = window.get_cursor_pos()
                    window.move_to(cursor_x - drag_offset[0], cursor_y - drag_offset[1])

            if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                if not drag_started and mouse_down_pos is not None:
                    for rect, timestamp in line_rects:
                        if rect.collidepoint(event.pos):
                            token = spotify_client.get_access_token()
                            _fire_and_forget(spotify_client.seek_playback, token, int(timestamp * 1000))
                            tracker.force_position(timestamp)
                            break

                mouse_down_pos = None
                drag_started = False

        elapsed_sec, is_playing = tracker.get_position_seconds()
        synced_elapsed = elapsed_sec - offset_sec

        # Spawn a fresh card the instant each line's timestamp arrives, independent
        # of whether earlier cards are still animating — this is what lets pacing
        # follow the song directly instead of being gated to one card at a time
        spawn_target = bisect.bisect_right(timestamps, synced_elapsed)
        if spawn_target > next_spawn_index:
            if spawn_target - next_spawn_index > 1:
                # big forward jump (seek) — don't burst-spawn every skipped line
                next_spawn_index = spawn_target - 1
            while next_spawn_index < spawn_target:
                spawn_text = lyric_lines[next_spawn_index][1] or "♪"
                spawn_timestamp = timestamps[next_spawn_index]

                # Never let a card disappear before the next one arrives — stretch
                # its lifetime to cover the actual gap when lines are far apart,
                # but never shrink it below the base animation time when they're close
                if next_spawn_index + 1 < len(timestamps):
                    gap_to_next = timestamps[next_spawn_index + 1] - spawn_timestamp
                    spawn_duration = max(CARD_ANIMATION_DURATION, gap_to_next)
                else:
                    spawn_duration = CARD_ANIMATION_DURATION

                active_cards.append(LyricCard(spawn_text, spawn_timestamp, spawn_duration))
                if len(active_cards) > MAX_ACTIVE_CARDS:
                    # Retire the oldest to make room, but let it finish its slide
                    # and fade quickly instead of vanishing instantly
                    active_cards[0].retire(synced_elapsed)
                next_spawn_index += 1
        elif spawn_target < next_spawn_index:
            # backward jump (seek) — drop stale cards and resync
            active_cards.clear()
            next_spawn_index = spawn_target

        active_cards[:] = [
            c for c in active_cards
            if (
                (synced_elapsed - c.retiring_since) < RETIRE_FADE_SEC
                if c.retiring_since is not None
                else (synced_elapsed - c.timestamp) < c.duration
            )
        ]

        waiting_for_start = bool(
            not active_cards
            and next_spawn_index == 0
            and timestamps
            and (timestamps[0] - synced_elapsed) > PREROLL_SEC
        )

        current_weather = WEATHER_MODES[weather_mode_index]

        if current_weather != "off":
            spawn_chance = 0.25 if current_weather == "snow" else 0.15
            if random.random() < spawn_chance:
                weather_particles.append(WeatherParticle(current_weather, note_font=note_font))

        for particle in weather_particles[:]:
            particle.update(dt)
            if particle.offscreen():
                weather_particles.remove(particle)

        # -----------------------------------------
        # DRAW
        # -----------------------------------------
        canvas.fill((0, 0, 0, 0))

        for particle in weather_particles:
            particle.draw(canvas)

        next_line_rects = []

        if not lyric_lines:
            draw_text_centered(line_font, "No synced lyrics found for this track.", WHITE, HEIGHT // 2)
        elif waiting_for_start:
            pulse = (math.sin(pygame.time.get_ticks() / 400) + 1) / 2
            note_surf = active_font.render("♪", True, accent)
            note_surf.set_alpha(int(80 + pulse * 120))
            rect = note_surf.get_rect(center=(WIDTH // 2, HEIGHT // 2))
            canvas.blit(note_surf, rect)
        else:
            for card in active_cards:
                card_rect = draw_lyric_card(canvas, card, synced_elapsed, active_font)
                if card_rect is not None:
                    next_line_rects.append((card_rect, card.timestamp))

            if active_cards and not is_playing:
                draw_text_centered(hint_font, "PAUSED — press SPACE to resume", DIM, HEIGHT - 20)

        line_rects = next_line_rects

        hovering_line = any(rect.collidepoint(pygame.mouse.get_pos()) for rect, _ in line_rects)
        pygame.mouse.set_cursor(
            pygame.SYSTEM_CURSOR_HAND if hovering_line else pygame.SYSTEM_CURSOR_ARROW
        )

        if layered_window is not None:
            layered_window.present(canvas)
        else:
            screen.blit(canvas, (0, 0))
            pygame.display.flip()

    if layered_window is not None:
        layered_window.close()

    set_console_visible(True)
    pygame.quit()


# -------------------------------------------------
# MAIN
# -------------------------------------------------
def main():
    url = input("Paste a Spotify track link: ").strip()

    print("Looking up track info...")
    try:
        title, artist, track_id, art_url = resolve_spotify_track(url)
    except Exception as exc:
        print(f"Failed to resolve track: {exc}")
        sys.exit(1)

    print(f"Found: {title} — {artist}")

    print("Connecting to Spotify (this may open a browser to log in)...")
    try:
        token = spotify_client.get_access_token()
    except Exception as exc:
        print(f"Spotify login failed: {exc}")
        sys.exit(1)

    device_id = pick_device(token)
    if device_id is None:
        print("No active Spotify device found. Open Spotify on your phone or computer and try again.")
        sys.exit(1)

    print("Starting playback...")
    try:
        spotify_client.start_playback(token, device_id, f"spotify:track:{track_id}")
    except Exception as exc:
        print(f"Failed to start playback: {exc}")
        sys.exit(1)

    print("Searching for synced lyrics...")
    lyric_lines = fetch_synced_lyrics(title, artist)

    if lyric_lines:
        print(f"Found {len(lyric_lines)} synced lines.")
    else:
        print("No synced lyrics found — popup will say so.")

    tracker = spotify_client.PlaybackTracker()
    tracker.start()

    # pygame must be initialized before we can decode the album art image
    pygame.init()
    art_surface = download_album_art(art_url)

    run_popup(title, lyric_lines or [], tracker, art_surface)

    tracker.stop()


if __name__ == "__main__":
    main()
