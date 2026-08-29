import colorsys
import ctypes
import math
import random
import sys
import threading
import time
from collections import deque

import numpy as np
import pyaudiowpatch as pyaudio
import pygame

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes.wintypes

# -------------------------------------------------
# SETTINGS
# -------------------------------------------------
# Placeholder size — replaced with the actual screen resolution at startup (see run_popup)
WIDTH, HEIGHT = 900, 600
FPS = 60

CHUNK = 1024

# Frequency bands (Hz) the analyzer tracks separately
BASS_BAND = (20, 150)
MID_BAND = (150, 2000)
TREBLE_BAND = (2000, 8000)

# How much recent history the beat detector looks at to know what "normal"
# energy looks like right now, so it adapts to quiet and loud passages alike
HISTORY_SECONDS = 1.0

# A beat fires when the current bass energy exceeds SENSITIVITY times the
# recent rolling average — lower = more trigger-happy, higher = only hits hard
DEFAULT_SENSITIVITY = 1.35
SENSITIVITY_STEP = 0.05
MIN_SENSITIVITY, MAX_SENSITIVITY = 1.05, 2.5

# Minimum gap between two detected beats, so one loud transient can't fire twice
REFRACTORY_SEC = 0.12

# Below this raw energy, treat everything as silence/noise floor (never a beat)
SILENCE_FLOOR = 1e-6

# Smoothed level (used for the blob's "breathing" size) rises fast on louder
# audio and falls slower, like a peak meter
LEVEL_ATTACK_RATE = 18.0
LEVEL_RELEASE_RATE = 4.0

# Per-chunk decay of each band's running peak, used to normalize energy into
# a 0..1 range that adapts to the current volume/mix instead of a fixed scale
NORM_DECAY = 0.996

# Seconds for a beat's "punch" to decay back to zero on the blob/ripples
PULSE_DECAY_TAU = 0.18

BASE_BLOB_FRAC = 0.05  # base blob radius as a fraction of the smaller screen dimension

# Each beat spawns a randomly-shaped effect (ring/polygon/spikes) that
# expands all the way out to the screen edge, then rebounds back inward as a
# much fainter echo before dying out
RIPPLE_OUTBOUND_DURATION = 0.8
RIPPLE_REBOUND_DURATION = 0.6
RIPPLE_REBOUND_ALPHA_SCALE = 0.35
MAX_EFFECTS = 5

HINT_FADE_START_SEC = 4.0
HINT_FADE_DURATION_SEC = 4.0
HINT_MIN_ALPHA = 40

# Continuous auto color cycle — degrees of hue rotation per second
HUE_CYCLE_DEG_PER_SEC = 18.0

# Audio wave that runs continuously around the screen border — two traveling
# sine ripples (a slow big one riding the bass, a fast small one riding the
# treble) chasing each other around the loop, swelling with the beat pulse
WAVE_MARGIN_FRAC = 0.05  # inset from the screen edges, as a fraction of the smaller dimension
WAVE_BASE_AMP = 4.0      # resting amplitude in px, even in silence

WAVE_COUNT_SLOW = 3      # how many humps the slow/bass ripple has around the whole loop
WAVE_COUNT_FAST = 9      # how many humps the fast/treble ripple has around the whole loop
WAVE_SPEED_SLOW = 0.12   # loops/sec the slow ripple travels at rest energy
WAVE_SPEED_FAST = -0.22  # negative = travels the opposite way around, for visual interest

WAVE_BASS_AMP = 42.0     # extra px of amplitude per unit of bass level, for the slow ripple
WAVE_TREBLE_AMP = 16.0   # extra px of amplitude per unit of treble level, for the fast ripple
WAVE_PULSE_AMP = 60.0    # extra px added to the whole ring per unit of the beat pulse
WAVE_RENDER_POINTS = 220

WHITE = (253, 253, 253)
DIM = (141, 141, 154)

# Glow hue sits a bit warmer/lighter than the core hue so the blob reads as
# two-toned rather than a single flat color
GLOW_HUE_OFFSET_DEG = 25


def hue_to_theme(hue_deg, energy=0.6):
    """(core color, glow color) for a given hue, in degrees. `energy` (0..1)
    pulls saturation/brightness up for a vivid look on energetic songs, and
    down toward muted/dim pastel for calm, quiet ones."""
    def hsv(h, s, v):
        r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, s, v)
        return (int(r * 255), int(g * 255), int(b * 255))

    energy = max(0.0, min(1.0, energy))
    core = hsv(hue_deg, 0.40 + 0.40 * energy, 0.60 + 0.35 * energy)
    glow = hsv(hue_deg + GLOW_HUE_OFFSET_DEG, 0.30 + 0.35 * energy, 0.75 + 0.25 * energy)
    return core, glow


# -------------------------------------------------
# AUDIO CAPTURE + BEAT DETECTION
# -------------------------------------------------
class AudioBeatAnalyzer:
    """Captures whatever the system is currently playing via WASAPI loopback
    and detects beats/levels from it in a background thread — independent of
    which app or service is actually producing the sound."""

    def __init__(self, sensitivity=DEFAULT_SENSITIVITY):
        self._lock = threading.Lock()
        self._pa = None
        self._stream = None
        self._channels = 2
        self._rate = 48000

        self.sensitivity = sensitivity
        self._bass_history = deque(maxlen=1)
        self._last_beat_time = 0.0
        self._beat_times = deque(maxlen=6)
        self._band_peaks = {"bass": 1e-6, "mid": 1e-6, "treble": 1e-6}

        self.bass_level = 0.0
        self.mid_level = 0.0
        self.treble_level = 0.0
        self.level = 0.0
        self.beat_count = 0
        self.last_beat_strength = 0.0
        # How hard recent beats have hit (decays if beats stop) and how fast
        # they're coming — together these approximate the song's "energy"
        self.beat_intensity = 0.0
        self.beat_rate = 0.0

    def start(self):
        self._pa = pyaudio.PyAudio()
        device = self._find_loopback_device()

        self._rate = int(device["defaultSampleRate"])
        self._channels = max(1, int(device["maxInputChannels"]))
        self._bass_history = deque(maxlen=max(1, int(HISTORY_SECONDS * self._rate / CHUNK)))

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=self._rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=CHUNK,
            stream_callback=self._on_audio,
        )
        self._stream.start_stream()

    def stop(self):
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:
            pass

    def _find_loopback_device(self):
        try:
            return self._pa.get_default_wasapi_loopback()
        except Exception:
            pass

        # Older pyaudiowpatch builds / unusual setups: resolve it by hand
        # instead — find the default output device, then the loopback
        # device that shadows it.
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        if default_output.get("isLoopbackDevice"):
            return default_output

        for loopback in self._pa.get_loopback_device_info_generator():
            if default_output["name"] in loopback["name"]:
                return loopback

        raise RuntimeError("No WASAPI loopback device found for the default output device.")

    def _on_audio(self, in_data, frame_count, time_info, status):
        samples = np.frombuffer(in_data, dtype=np.float32)
        if self._channels > 1:
            samples = samples.reshape(-1, self._channels).mean(axis=1)

        if len(samples) == 0:
            return (None, pyaudio.paContinue)

        windowed = samples * np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(samples), 1.0 / self._rate)

        bass_energy = self._band_energy(spectrum, freqs, BASS_BAND)
        mid_energy = self._band_energy(spectrum, freqs, MID_BAND)
        treble_energy = self._band_energy(spectrum, freqs, TREBLE_BAND)
        overall_rms = float(np.sqrt(np.mean(np.square(samples))))

        chunk_dt = len(samples) / self._rate
        attack = 1 - math.exp(-chunk_dt * LEVEL_ATTACK_RATE)
        release = 1 - math.exp(-chunk_dt * LEVEL_RELEASE_RATE)
        now = time.monotonic()

        with self._lock:
            self._band_peaks["bass"] = max(bass_energy, self._band_peaks["bass"] * NORM_DECAY)
            self._band_peaks["mid"] = max(mid_energy, self._band_peaks["mid"] * NORM_DECAY)
            self._band_peaks["treble"] = max(treble_energy, self._band_peaks["treble"] * NORM_DECAY)

            bass_norm = min(1.0, bass_energy / self._band_peaks["bass"])
            mid_norm = min(1.0, mid_energy / self._band_peaks["mid"])
            treble_norm = min(1.0, treble_energy / self._band_peaks["treble"])

            self.bass_level = self._smooth(self.bass_level, bass_norm, attack, release)
            self.mid_level = self._smooth(self.mid_level, mid_norm, attack, release)
            self.treble_level = self._smooth(self.treble_level, treble_norm, attack, release)
            self.level = self._smooth(self.level, min(1.0, overall_rms * 6.0), attack, release)

            # Fades on its own if beats stop coming, so a quiet/ambient
            # passage settles back toward calm within a couple of seconds
            self.beat_intensity *= math.exp(-chunk_dt / 2.0)
            if now - self._last_beat_time > 3.0:
                self.beat_rate = 0.0

            self._bass_history.append(bass_energy)
            if len(self._bass_history) >= 8:
                avg = sum(self._bass_history) / len(self._bass_history)
                threshold = avg * self.sensitivity
                since_last_beat = now - self._last_beat_time

                if bass_energy > threshold and bass_energy > SILENCE_FLOOR and since_last_beat > REFRACTORY_SEC:
                    self._last_beat_time = now
                    self.beat_count += 1
                    self.last_beat_strength = min(2.5, bass_energy / (avg + 1e-9))
                    self.beat_intensity += (self.last_beat_strength - self.beat_intensity) * 0.5

                    self._beat_times.append(now)
                    if len(self._beat_times) >= 2:
                        intervals = [b - a for a, b in zip(self._beat_times, list(self._beat_times)[1:])]
                        avg_interval = sum(intervals) / len(intervals)
                        if avg_interval > 1e-3:
                            self.beat_rate = 1.0 / avg_interval

        return (None, pyaudio.paContinue)

    @staticmethod
    def _band_energy(spectrum, freqs, band):
        lo, hi = band
        mask = (freqs >= lo) & (freqs < hi)
        if not np.any(mask):
            return 0.0
        return float(np.sum(spectrum[mask] ** 2))

    @staticmethod
    def _smooth(current, target, attack, release):
        rate = attack if target > current else release
        return current + (target - current) * rate

    def snapshot(self):
        with self._lock:
            return {
                "bass": self.bass_level,
                "mid": self.mid_level,
                "treble": self.treble_level,
                "level": self.level,
                "beat_count": self.beat_count,
                "beat_strength": self.last_beat_strength,
                "beat_intensity": self.beat_intensity,
                "beat_rate": self.beat_rate,
            }

    def adjust_sensitivity(self, delta):
        with self._lock:
            self.sensitivity = max(MIN_SENSITIVITY, min(MAX_SENSITIVITY, self.sensitivity + delta))
            return self.sensitivity


# -------------------------------------------------
# VISUAL HELPERS
# -------------------------------------------------
def lerp_color(color_a, color_b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a + (b - a) * t) for a, b in zip(color_a, color_b))


def _wave_border_point(u, width, height, margin):
    """Point + outward normal at fraction `u` (0..1) around the rectangle
    perimeter inset by `margin` px, traversed clockwise starting top-left."""
    w = max(1.0, width - margin * 2)
    h = max(1.0, height - margin * 2)
    d = (u % 1.0) * 2 * (w + h)

    if d < w:
        return margin + d, margin, 0.0, -1.0
    d -= w
    if d < h:
        return margin + w, margin + d, 1.0, 0.0
    d -= h
    if d < w:
        return margin + w - d, margin + h, 0.0, 1.0
    d -= w
    return margin, margin + h - d, -1.0, 0.0


def draw_audio_wave(canvas, bass_level, treble_level, phase_slow, phase_fast, width, height, margin, pulse, glow_color, core_color, flash_t):
    """Two sine ripples continuously traveling around the screen border in
    opposite directions — a slow big one riding the bass, a fast small one
    riding the treble — swelling together with the beat pulse. `phase_slow`
    and `phase_fast` are in loops (0..1 each), advanced every frame by the
    caller so the wave is always visibly in motion, beat or no beat."""
    num_points = WAVE_RENDER_POINTS
    u = np.arange(num_points) / num_points

    slow_wave = 0.5 + 0.5 * np.sin(2 * np.pi * (WAVE_COUNT_SLOW * u + phase_slow))
    fast_wave = 0.5 + 0.5 * np.sin(2 * np.pi * (WAVE_COUNT_FAST * u + phase_fast))
    amps = WAVE_BASE_AMP + bass_level * WAVE_BASS_AMP * slow_wave + treble_level * WAVE_TREBLE_AMP * fast_wave + pulse * WAVE_PULSE_AMP

    # Clamped so the wave can never balloon entirely off-screen at high energy
    max_amp = margin * 0.95
    amps = np.minimum(amps, max_amp)

    points = []
    for i in range(num_points):
        x, y, nx, ny = _wave_border_point(u[i], width, height, margin)
        points.append((x + nx * amps[i], y + ny * amps[i]))

    bright_color = lerp_color(core_color, WHITE, flash_t * 0.5)
    pygame.draw.lines(canvas, (*glow_color, 55), True, points, 9)
    pygame.draw.lines(canvas, (*glow_color, 110), True, points, 4)
    pygame.draw.lines(canvas, (*bright_color, 255), True, points, 2)


EFFECT_KINDS = ("ring", "polygon", "spikes")


class BeatEffect:
    """A shape spawned each time a beat fires: it expands out to the screen
    edge, then rebounds back inward as a much fainter echo before dying out.
    Its exact shape and orientation are randomized per beat so consecutive
    beats never look identical."""

    def __init__(self, strength, mood=0.6):
        mood = max(0.0, min(1.0, mood))
        self.spawn_time = time.monotonic()
        self.strength = min(1.6, max(0.4, strength))
        self.kind = random.choice(EFFECT_KINDS)
        self.rotation_offset = random.uniform(0, 360)
        self.spin_speed = random.uniform(30, 110) * random.choice((-1, 1)) * (0.3 + mood * 1.3)
        self.sides = random.randint(3, 7)
        self.spike_count = random.randint(5, 14)
        # Calm songs get a slow, lingering expansion; energetic ones snap out fast
        self.outbound_duration = RIPPLE_OUTBOUND_DURATION * (1.6 - 0.9 * mood)
        self.rebound_duration = RIPPLE_REBOUND_DURATION * (1.5 - 0.8 * mood)

    def elapsed(self, now):
        return now - self.spawn_time

    def total_duration(self):
        return self.outbound_duration + self.rebound_duration


def _polygon_points(center_xy, radius, sides, rotation_deg):
    return [
        (
            center_xy[0] + radius * math.cos(math.radians(rotation_deg + i * (360 / sides))),
            center_xy[1] + radius * math.sin(math.radians(rotation_deg + i * (360 / sides))),
        )
        for i in range(sides)
    ]


def draw_beat_effect(canvas, effect, now, center, base_radius, border_radius, glow_color):
    elapsed = effect.elapsed(now)
    if elapsed >= effect.total_duration():
        return False

    if elapsed < effect.outbound_duration:
        # Outbound leg: travels from the blob's edge out to the screen border
        t = elapsed / effect.outbound_duration
        eased = 1 - (1 - t) ** 2  # ease-out
        radius = base_radius + eased * (border_radius - base_radius)
        alpha = min(255, int(160 * effect.strength * (1 - t * 0.4)))
        width = max(2, int(4 * (1 - t) + 1))
    else:
        # Rebound leg: a faint echo travels back inward from the border
        t = (elapsed - effect.outbound_duration) / effect.rebound_duration
        eased = t * t  # ease-in, as if accelerating back toward the blob
        radius = border_radius - eased * (border_radius - base_radius)
        alpha = min(255, int(160 * effect.strength * RIPPLE_REBOUND_ALPHA_SCALE * (1 - t)))
        width = max(1, int(2 * (1 - t) + 1))

    radius = int(radius)
    if radius <= 0 or alpha <= 0:
        return True

    rotation = effect.rotation_offset + elapsed * effect.spin_speed
    pad = width * 2 + 4
    surf_size = radius * 2 + pad * 2
    surf = pygame.Surface((surf_size, surf_size), pygame.SRCALPHA)
    surf_center = (surf_size / 2, surf_size / 2)
    color = (*glow_color, alpha)

    if effect.kind == "polygon":
        pygame.draw.polygon(surf, color, _polygon_points(surf_center, radius, effect.sides, rotation), width)
    elif effect.kind == "spikes":
        inner_radius = radius * 0.55
        for i in range(effect.spike_count):
            angle = math.radians(rotation + i * (360 / effect.spike_count))
            start = (surf_center[0] + inner_radius * math.cos(angle), surf_center[1] + inner_radius * math.sin(angle))
            end = (surf_center[0] + radius * math.cos(angle), surf_center[1] + radius * math.sin(angle))
            pygame.draw.line(surf, color, start, end, width)
    else:
        pygame.draw.circle(surf, color, surf_center, radius, width)

    canvas.blit(surf, surf.get_rect(center=center))
    return True


def build_blob_surface(radius, core_color, glow_color, glow_alpha_scale):
    """A soft glowing circle: a bright core fading out through a few
    translucent rings, all pre-composited onto one padded surface."""
    rings = (
        (int(radius * 0.9), 25, glow_color),
        (int(radius * 0.55), 55, lerp_color(glow_color, core_color, 0.5)),
        (int(radius * 0.2), 90, glow_color),
    )
    pad = rings[0][0] + 12
    size = radius * 2 + pad * 2
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    center = (size // 2, size // 2)

    for expand, alpha, ring_color in rings:
        ring_radius = radius + expand
        pygame.draw.circle(surf, (*ring_color, int(alpha * glow_alpha_scale)), center, ring_radius)

    pygame.draw.circle(surf, (*core_color, 255), center, radius)
    return surf


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


# -------------------------------------------------
# WINDOWS LAYERED (TRANSPARENT, ALWAYS-ON-TOP) WINDOW
# -------------------------------------------------
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
        rgb = np.transpose(pygame.surfarray.pixels3d(surface), (1, 0, 2))     # (H, W, 3)
        alpha = np.transpose(pygame.surfarray.pixels_alpha(surface), (1, 0))  # (H, W)

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
def run_popup(analyzer):
    global WIDTH, HEIGHT

    desktop_sizes = pygame.display.get_desktop_sizes()
    if desktop_sizes:
        WIDTH, HEIGHT = desktop_sizes[0]

    import os
    os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
    screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.NOFRAME)
    pygame.display.set_caption("Beat")

    window = WindowController()
    window.apply_topmost()

    layered_window = LayeredWindow(window.hwnd, WIDTH, HEIGHT) if window.enabled else None
    canvas = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)

    pygame.key.set_repeat(300, 50)
    clock = pygame.time.Clock()

    hint_font = pygame.font.SysFont("consolas", 15)

    mouse_down_pos = None
    drag_started = False
    drag_offset = (0, 0)
    DRAG_THRESHOLD_SQ = 6 * 6

    hue = random.uniform(0, 360)
    effects = []
    pulse = 0.0
    rotation_angle = 0.0
    wave_phase_slow = 0.0
    wave_phase_fast = 0.0
    wave_margin = min(WIDTH, HEIGHT) * WAVE_MARGIN_FRAC
    mood = 0.3  # smoothed 0..1 "energy" reading — how soft/calm vs. loud/intense the song is right now
    last_beat_count = analyzer.snapshot()["beat_count"]
    start_time = time.monotonic()

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
        now = time.monotonic()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False

                if event.key == pygame.K_c:
                    hue = random.uniform(0, 360)

                if event.key == pygame.K_UP:
                    analyzer.adjust_sensitivity(-SENSITIVITY_STEP)

                if event.key == pygame.K_DOWN:
                    analyzer.adjust_sensitivity(SENSITIVITY_STEP)

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
                mouse_down_pos = None
                drag_started = False

        snap = analyzer.snapshot()

        # Overall "energy" reading for the song right now — soft/quiet passages
        # pull this toward 0 (slow, dim, gentle motion), loud/fast/hard-hitting
        # passages pull it toward 1 (fast, bright, more action)
        beat_intensity_norm = min(1.0, snap["beat_intensity"] / 1.4)
        beat_rate_norm = min(1.0, snap["beat_rate"] / 2.5)
        target_mood = max(0.0, min(1.0, 0.45 * snap["level"] + 0.30 * beat_intensity_norm + 0.25 * beat_rate_norm))
        mood += (target_mood - mood) * min(1.0, dt * 2.0)

        if snap["beat_count"] != last_beat_count:
            last_beat_count = snap["beat_count"]
            pulse = min(1.6, pulse + snap["beat_strength"] * (0.35 + mood * 0.9))
            effects.append(BeatEffect(snap["beat_strength"], mood))
            if len(effects) > MAX_EFFECTS:
                effects.pop(0)

        pulse_tau = PULSE_DECAY_TAU * (1.7 - 0.9 * mood)
        pulse *= math.exp(-dt / pulse_tau)
        rotation_angle += dt * (10 + snap["mid"] * 40) * (0.35 + mood * 1.3)
        hue = (hue + dt * HUE_CYCLE_DEG_PER_SEC * (0.3 + mood * 1.3)) % 360
        wave_speed_scale = 0.4 + mood * 1.2
        wave_phase_slow = (wave_phase_slow + dt * WAVE_SPEED_SLOW * wave_speed_scale) % 1.0
        wave_phase_fast = (wave_phase_fast + dt * WAVE_SPEED_FAST * wave_speed_scale) % 1.0

        core_color, glow_color = hue_to_theme(hue, energy=mood)
        flash_t = min(1.0, pulse * 0.6)
        core_color = lerp_color(core_color, WHITE, flash_t)

        base_radius = min(WIDTH, HEIGHT) * BASE_BLOB_FRAC
        radius = base_radius * (1 + snap["level"] * 0.5 + pulse * 0.8)

        # -----------------------------------------
        # DRAW
        # -----------------------------------------
        canvas.fill((0, 0, 0, 0))
        center = (WIDTH // 2, HEIGHT // 2)
        border_radius = min(center[0], WIDTH - center[0], center[1], HEIGHT - center[1])

        draw_audio_wave(
            canvas, snap["bass"], snap["treble"], wave_phase_slow, wave_phase_fast, WIDTH, HEIGHT, wave_margin,
            pulse, glow_color, core_color, flash_t,
        )

        effects[:] = [
            e for e in effects if draw_beat_effect(canvas, e, now, center, radius, border_radius, glow_color)
        ]

        # Slow outer ring, its radius/brightness riding the mid band, sparkling with treble
        ring_radius = int(radius * 1.7 + snap["mid"] * 24)
        ring_alpha = int((50 + snap["treble"] * 120) * (0.35 + mood * 0.9))
        ring_surf = pygame.Surface((ring_radius * 2 + 8, ring_radius * 2 + 8), pygame.SRCALPHA)
        dash_count = 18
        for i in range(dash_count):
            angle = math.radians(rotation_angle + i * (360 / dash_count))
            if (i + int(rotation_angle / 20)) % 3 == 0:
                continue
            x = ring_surf.get_width() // 2 + math.cos(angle) * ring_radius
            y = ring_surf.get_height() // 2 + math.sin(angle) * ring_radius
            pygame.draw.circle(ring_surf, (*glow_color, ring_alpha), (int(x), int(y)), 3)
        canvas.blit(ring_surf, ring_surf.get_rect(center=center))

        glow_alpha_scale = min(1.0, 0.4 + mood * 0.4 + flash_t * 0.4)
        blob_surf = build_blob_surface(int(radius), core_color, glow_color, glow_alpha_scale=glow_alpha_scale)
        canvas.blit(blob_surf, blob_surf.get_rect(center=center))

        hint_elapsed = now - start_time
        if hint_elapsed > HINT_FADE_START_SEC:
            fade_t = min(1.0, (hint_elapsed - HINT_FADE_START_SEC) / HINT_FADE_DURATION_SEC)
            hint_alpha = int(220 + (HINT_MIN_ALPHA - 220) * fade_t)
        else:
            hint_alpha = 220
        draw_text_centered(
            hint_font,
            f"ESC quit  ·  drag to move  ·  UP/DOWN sensitivity ({analyzer.sensitivity:.2f})  ·  C color",
            DIM, HEIGHT - 24, alpha=hint_alpha,
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
    if not IS_WINDOWS:
        print("This popup needs WASAPI loopback capture, which is Windows-only.")
        sys.exit(1)

    analyzer = AudioBeatAnalyzer()

    print("Starting audio capture (WASAPI loopback of your default output device)...")
    try:
        analyzer.start()
    except Exception as exc:
        print(f"Couldn't start audio capture: {exc}")
        sys.exit(1)

    print("Listening — play something! Press ESC in the popup to quit.")

    pygame.init()
    try:
        run_popup(analyzer)
    finally:
        analyzer.stop()


if __name__ == "__main__":
    main()
