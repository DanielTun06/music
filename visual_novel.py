"""Dark cinematic pixel-art visual novel UI — sliding dialogue box, name
panel, typewriter text, floating embers, parallax background and camera
shake, all driven by delta time so nothing ever blocks on time.sleep()."""

import math
import random
import sys

import pygame

pygame.init()

WIDTH, HEIGHT = 1280, 720
FPS = 60

# -------------------------------------------------
# PALETTE
# -------------------------------------------------
COLOR_BG_TOP = (9, 9, 11)
COLOR_BG_BOTTOM = (16, 12, 13)
COLOR_VOID = (4, 4, 5)
COLOR_PANEL = (14, 13, 15)
COLOR_PANEL_EDGE = (28, 25, 27)
COLOR_RED = (168, 28, 40)
COLOR_RED_DIM = (78, 18, 24)
COLOR_RED_BRIGHT = (222, 54, 64)
COLOR_WHITE = (232, 230, 227)
COLOR_DIM = (118, 114, 116)

FONT_PATH = pygame.font.match_font("consolas,couriernew,monospace")
FONT_TEXT = pygame.font.Font(FONT_PATH, 24)
FONT_NAME = pygame.font.Font(FONT_PATH, 20)
FONT_HINT = pygame.font.Font(FONT_PATH, 14)

random.seed()


# -------------------------------------------------
# EASING
# -------------------------------------------------
def ease_linear(t):
    return t


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def ease_in_cubic(t):
    return t * t * t


def ease_out_back(t, overshoot=0.7):
    """Fast start, decelerating approach, a small overshoot past 1.0 that
    settles back — used for anything sliding into frame so it reads as
    responsive rather than robotic."""
    t -= 1
    return 1 + (overshoot + 1) * t ** 3 + overshoot * t ** 2


def ease_in_out_sine(t):
    return -(math.cos(math.pi * t) - 1) / 2


# -------------------------------------------------
# ANIMATION
# -------------------------------------------------
class Animation:
    """Tweens a single float from `start` to `end` over `duration_ms`,
    optionally held off by `delay_ms` before it starts moving. Advance with
    update(dt_ms); .value always reflects the current eased position, and
    .finished latches once the tween has fully played out."""

    def __init__(self, start, end, duration_ms, easing=ease_out_cubic, delay_ms=0, on_complete=None):
        self.start = start
        self.end = end
        self.duration = max(1, duration_ms)
        self.delay = delay_ms
        self.easing = easing
        self.on_complete = on_complete
        self.elapsed = 0
        self.finished = False
        self.value = start

    def update(self, dt_ms):
        if self.finished:
            return self.value

        if self.delay > 0:
            self.delay -= dt_ms
            if self.delay > 0:
                return self.value
            dt_ms = -self.delay
            self.delay = 0

        self.elapsed += dt_ms
        t = min(1.0, self.elapsed / self.duration)
        self.value = self.start + (self.end - self.start) * self.easing(t)

        if t >= 1.0:
            self.finished = True
            self.value = self.end
            if self.on_complete:
                self.on_complete()

        return self.value


def still(value):
    """An already-finished Animation sitting at a constant value — used to
    seed fields before their first real tween exists."""
    anim = Animation(value, value, 1)
    anim.finished = True
    anim.value = value
    return anim


# -------------------------------------------------
# PARTICLES
# -------------------------------------------------
class Particle:
    """One floating red ember drifting upward with a gentle horizontal sway;
    respawns at the bottom once it drifts off the top or burns out."""

    def __init__(self):
        self._respawn(initial=True)

    def _respawn(self, initial=False):
        self.x = random.uniform(0, WIDTH)
        self.y = random.uniform(0, HEIGHT) if initial else HEIGHT + random.uniform(10, 60)
        self.size = random.uniform(1.2, 3.2)
        self.speed_y = random.uniform(14, 34)
        self.sway_amount = random.uniform(6, 22)
        self.sway_phase = random.uniform(0, math.tau)
        self.sway_speed = random.uniform(0.4, 1.1)
        self.max_alpha = random.uniform(70, 170)
        self.life = random.uniform(6.0, 13.0)
        self.age = 0.0

    def update(self, dt):
        self.age += dt
        self.y -= self.speed_y * dt
        self.x += math.sin(self.sway_phase + self.age * self.sway_speed) * self.sway_amount * dt

        if self.y < -20 or self.age > self.life:
            self._respawn()

    def draw(self, surface):
        life_t = min(1.0, self.age / 0.8)
        fade_t = max(0.0, min(1.0, (self.life - self.age) / 1.0))
        alpha = int(self.max_alpha * min(life_t, fade_t))
        if alpha <= 2:
            return

        r = max(2, int(self.size * 3))
        glow = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
        pygame.draw.circle(glow, (*COLOR_RED_BRIGHT, alpha // 3), (r, r), r)
        pygame.draw.circle(glow, (*COLOR_RED_BRIGHT, alpha), (r, r), max(1, int(self.size)))
        surface.blit(glow, (self.x - r, self.y - r))


class ParticleField:
    def __init__(self, count=45):
        self.particles = [Particle() for _ in range(count)]

    def update(self, dt):
        for p in self.particles:
            p.update(dt)

    def draw(self, surface):
        for p in self.particles:
            p.draw(surface)


# -------------------------------------------------
# CAMERA SHAKE
# -------------------------------------------------
class CameraShake:
    """Short decaying random offset applied to the whole frame — used to
    punch up emotionally 'important' dialogue lines."""

    def __init__(self):
        self.magnitude = 0.0
        self.duration = 0.0
        self.remaining = 0.0

    def trigger(self, magnitude, duration_ms):
        self.magnitude = magnitude
        self.duration = duration_ms
        self.remaining = duration_ms

    def update(self, dt):
        if self.remaining <= 0:
            return 0, 0

        self.remaining -= dt
        strength = self.magnitude * max(0.0, self.remaining / self.duration)
        return (
            random.uniform(-strength, strength),
            random.uniform(-strength, strength),
        )


# -------------------------------------------------
# BACKGROUND (gradient + parallax grid + vignette)
# -------------------------------------------------
class Background:
    def __init__(self):
        self.gradient = self._build_gradient()
        self.vignette = self._build_vignette()
        self.scroll = 0.0

    def _build_gradient(self):
        surf = pygame.Surface((WIDTH, HEIGHT))
        for y in range(HEIGHT):
            t = y / HEIGHT
            color = tuple(int(a + (b - a) * t) for a, b in zip(COLOR_BG_TOP, COLOR_BG_BOTTOM))
            pygame.draw.line(surf, color, (0, y), (WIDTH, y))
        return surf

    def _build_vignette(self):
        surf = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        max_dist = math.hypot(WIDTH / 2, HEIGHT / 2)
        step = 6
        for y in range(0, HEIGHT, step):
            for x in range(0, WIDTH, step):
                dist = math.hypot(x - WIDTH / 2, y - HEIGHT / 2) / max_dist
                alpha = max(0, int((dist - 0.45) * 180))
                if alpha > 0:
                    pygame.draw.rect(surf, (0, 0, 0, min(160, alpha)), (x, y, step, step))
        return surf

    def update(self, dt):
        self.scroll += dt * 6

    def draw(self, surface, mouse_pos):
        surface.blit(self.gradient, (0, 0))

        mx, my = mouse_pos
        parallax_x = (mx - WIDTH / 2) / (WIDTH / 2)
        parallax_y = (my - HEIGHT / 2) / (HEIGHT / 2)

        # two faint scanline layers drifting at different speeds/depths
        for layer, (spacing, speed, alpha, depth) in enumerate([(46, 0.4, 14, 10), (28, 0.9, 9, 22)]):
            offset_x = -parallax_x * depth
            offset_y = (self.scroll * speed) % spacing - parallax_y * depth
            y = offset_y - spacing
            while y < HEIGHT + spacing:
                pygame.draw.line(surface, (*COLOR_RED_DIM, alpha), (0 + offset_x, y), (WIDTH + offset_x, y))
                y += spacing

        surface.blit(self.vignette, (0, 0))


# -------------------------------------------------
# CHARACTER / LINE / SCENE
# -------------------------------------------------
class Character:
    def __init__(self, name, accent_color):
        self.name = name
        self.accent_color = accent_color


class Line:
    def __init__(self, character, text, important=False):
        self.character = character
        self.text = text
        self.important = important


class Scene:
    def __init__(self, lines):
        self.lines = lines
        self.index = 0

    def current(self):
        return self.lines[self.index]

    def has_next(self):
        return self.index < len(self.lines) - 1

    def advance(self):
        if self.has_next():
            self.index += 1
            return True
        return False


# -------------------------------------------------
# DIALOGUE BOX
# -------------------------------------------------
BOX_W = WIDTH - 220
BOX_H = 190
BOX_LEFT = 110
BOX_TARGET_Y = HEIGHT - BOX_H - 46
BOX_HIDDEN_Y = HEIGHT + 60
BOX_OFFSCREEN = BOX_W + 180

NAME_W, NAME_H = 260, 50
NAME_TARGET_X = BOX_LEFT + 18
NAME_Y = BOX_TARGET_Y - NAME_H + 16
NAME_HIDDEN_OFFSET = -(NAME_W + 90)
NAME_NUDGE_OFFSET = -46

MS_PER_CHAR = 24

PHASE_INITIAL = "initial"
PHASE_EXIT = "exit"
PHASE_ENTER = "enter"
PHASE_IDLE = "idle"


class DialogueBox:
    def __init__(self):
        self.line = None
        self.phase = PHASE_IDLE

        self.anim_box_y = still(BOX_HIDDEN_Y)
        self.anim_box_x = still(0.0)          # horizontal offset from rest position
        self.anim_box_alpha = still(0.0)
        self.anim_name_x = still(NAME_HIDDEN_OFFSET)
        self.anim_name_alpha = still(0.0)

        self.reveal_elapsed = 0
        self.blink_phase = 0.0

    # ---- state entry points ----
    def start(self, line):
        self.line = line
        self.phase = PHASE_INITIAL
        self.reveal_elapsed = 0

        self.anim_box_y = Animation(BOX_HIDDEN_Y, BOX_TARGET_Y, 450, ease_out_back)
        self.anim_box_x = still(0.0)
        self.anim_box_alpha = Animation(0, 255, 400, ease_out_cubic)

        self.anim_name_x = Animation(NAME_HIDDEN_OFFSET, 0.0, 380, ease_out_back, delay_ms=130)
        self.anim_name_alpha = Animation(0, 255, 320, ease_in_out_sine, delay_ms=130)

    def go_to(self, line):
        if self.phase == PHASE_INITIAL:
            return  # still entering for the first time; ignore rapid re-triggers

        self.phase = PHASE_EXIT

        def _swap_and_enter():
            self.line = line
            self.reveal_elapsed = 0
            self.phase = PHASE_ENTER

            self.anim_box_x = Animation(BOX_OFFSCREEN, 0.0, 420, ease_out_back)
            self.anim_box_alpha = Animation(0, 255, 360, ease_out_cubic)
            self.anim_name_x = Animation(NAME_NUDGE_OFFSET, 0.0, 340, ease_out_back, delay_ms=90)
            self.anim_name_alpha = Animation(0, 255, 260, ease_in_out_sine, delay_ms=90)

        self.anim_box_x = Animation(0.0, -BOX_OFFSCREEN, 260, ease_in_cubic, on_complete=_swap_and_enter)
        self.anim_box_alpha = Animation(255, 0, 220, ease_in_cubic)
        self.anim_name_x = Animation(0.0, NAME_NUDGE_OFFSET, 200, ease_in_cubic)
        self.anim_name_alpha = Animation(255, 0, 180, ease_in_cubic)

    # ---- input ----
    def is_typing(self):
        return self.phase == PHASE_IDLE and self.reveal_count() < len(self.line.text)

    def is_settled(self):
        return self.phase == PHASE_IDLE

    def reveal_count(self):
        return min(len(self.line.text), int(self.reveal_elapsed / MS_PER_CHAR))

    def skip_typewriter(self):
        self.reveal_elapsed = len(self.line.text) * MS_PER_CHAR

    # ---- update / draw ----
    def update(self, dt):
        self.anim_box_y.update(dt)
        self.anim_box_x.update(dt)
        self.anim_box_alpha.update(dt)
        self.anim_name_x.update(dt)
        self.anim_name_alpha.update(dt)

        if self.phase in (PHASE_INITIAL, PHASE_ENTER):
            if self.anim_box_alpha.finished and self.anim_box_x.finished and self.anim_box_y.finished:
                self.phase = PHASE_IDLE

        if self.phase == PHASE_IDLE:
            self.reveal_elapsed += dt

        self.blink_phase += dt

    def draw(self, surface):
        if self.line is None:
            return

        box_x = BOX_LEFT + self.anim_box_x.value
        box_y = self.anim_box_y.value
        box_alpha = max(0, min(255, int(self.anim_box_alpha.value)))

        self._draw_character_glow(surface, box_alpha)
        self._draw_box(surface, box_x, box_y, box_alpha)
        self._draw_name_panel(surface, box_y)

    def _draw_character_glow(self, surface, alpha):
        accent = self.line.character.accent_color
        glow_w, glow_h = 520, 460
        glow = pygame.Surface((glow_w, glow_h), pygame.SRCALPHA)
        for i in range(glow_h, 0, -4):
            t = i / glow_h
            a = int(26 * (1 - t) * (alpha / 255))
            pygame.draw.ellipse(glow, (*accent, a), (glow_w / 2 - i / 2.6, glow_h - i, i / 1.3, i))
        surface.blit(glow, (WIDTH / 2 - glow_w / 2, HEIGHT - glow_h - 40), special_flags=pygame.BLEND_RGBA_ADD)

    def _draw_box(self, surface, x, y, alpha):
        panel = pygame.Surface((BOX_W, BOX_H), pygame.SRCALPHA)
        pygame.draw.rect(panel, (*COLOR_PANEL, 235), (0, 0, BOX_W, BOX_H), border_radius=6)
        pygame.draw.rect(panel, (*COLOR_PANEL_EDGE, 235), (0, 0, BOX_W, BOX_H), width=1, border_radius=6)
        pygame.draw.rect(panel, (*COLOR_RED, 255), (0, 0, BOX_W, BOX_H), width=2, border_radius=6)
        pygame.draw.line(panel, (*COLOR_RED_BRIGHT, 200), (18, 14), (18, BOX_H - 14), 2)

        text = self.line.text[: self.reveal_count()]
        self._blit_wrapped(panel, text, (40, 26), BOX_W - 80)

        if self.is_settled() and self.reveal_count() >= len(self.line.text):
            self._draw_arrow(panel)

        panel.set_alpha(alpha)
        surface.blit(panel, (x, y))

    def _blit_wrapped(self, panel, text, pos, max_width):
        words = text.split(" ")
        lines, current = [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if FONT_TEXT.size(trial)[0] <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)

        x, y = pos
        for line in lines[:5]:
            surf = FONT_TEXT.render(line, True, COLOR_WHITE)
            panel.blit(surf, (x, y))
            y += FONT_TEXT.get_height() + 8

    def _draw_arrow(self, panel):
        pulse = (math.sin(self.blink_phase / 260) + 1) / 2
        alpha = int(90 + pulse * 160)
        ax, ay = BOX_W - 34, BOX_H - 26
        arrow = pygame.Surface((16, 12), pygame.SRCALPHA)
        pygame.draw.polygon(arrow, (*COLOR_RED_BRIGHT, alpha), [(0, 0), (16, 0), (8, 12)])
        panel.blit(arrow, (ax, ay))

    def _draw_name_panel(self, surface, box_y):
        alpha = max(0, min(255, int(self.anim_name_alpha.value)))
        if alpha <= 0:
            return

        x = NAME_TARGET_X + self.anim_name_x.value
        y = NAME_Y

        panel = pygame.Surface((NAME_W, NAME_H), pygame.SRCALPHA)
        accent = self.line.character.accent_color
        pygame.draw.rect(panel, (*COLOR_PANEL, 245), (0, 0, NAME_W, NAME_H), border_radius=4)
        pygame.draw.rect(panel, (*accent, 255), (0, 0, NAME_W, NAME_H), width=2, border_radius=4)

        name_surf = FONT_NAME.render(self.line.character.name.upper(), True, COLOR_WHITE)
        panel.blit(name_surf, (18, NAME_H / 2 - name_surf.get_height() / 2))

        panel.set_alpha(alpha)
        surface.blit(panel, (x, y))


# -------------------------------------------------
# DEMO CONTENT
# -------------------------------------------------
def build_demo_scene():
    unknown = Character("UNKNOWN", COLOR_RED_BRIGHT)
    echo = Character("ECHO-9", (150, 150, 160))

    lines = [
        Line(echo, "Signal acquired. ...You're not supposed to be awake yet."),
        Line(unknown, "Then it's a good thing I never listen."),
        Line(echo, "The lower levels went dark six hours ago. Nothing since."),
        Line(unknown, "WHO IS DOWN THERE WITH ME?!", important=True),
        Line(echo, "...I don't know. I've never seen a signature like it."),
        Line(echo, "Keep moving. The corridor lights won't hold much longer."),
    ]
    return Scene(lines)


# -------------------------------------------------
# MAIN LOOP
# -------------------------------------------------
def main():
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("VISUAL NOVEL — INTERFACE DEMO")
    clock = pygame.time.Clock()

    background = Background()
    particles = ParticleField()
    shake = CameraShake()
    box = DialogueBox()

    scene = build_demo_scene()
    box.start(scene.current())
    if scene.current().important:
        shake.trigger(7, 320)

    world = pygame.Surface((WIDTH, HEIGHT))

    def handle_advance():
        if box.is_typing():
            box.skip_typewriter()
            return
        if not box.is_settled():
            return
        if scene.advance():
            line = scene.current()
            box.go_to(line)
            if line.important:
                shake.trigger(7, 320)

    running = True
    while running:
        dt = clock.tick(FPS)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key in (pygame.K_SPACE, pygame.K_RETURN):
                    handle_advance()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                handle_advance()

        mouse_pos = pygame.mouse.get_pos()

        background.update(dt / 1000)
        particles.update(dt / 1000)
        box.update(dt)
        shake_x, shake_y = shake.update(dt)

        world.fill(COLOR_VOID)
        background.draw(world, mouse_pos)
        particles.draw(world)
        box.draw(world)

        hint = FONT_HINT.render("SPACE / CLICK — CONTINUE      ESC — QUIT", True, COLOR_DIM)
        world.blit(hint, (BOX_LEFT, HEIGHT - 22))

        screen.fill(COLOR_VOID)
        screen.blit(world, (shake_x, shake_y))
        pygame.draw.rect(screen, COLOR_RED_DIM, (0, 0, WIDTH, HEIGHT), width=2)

        pygame.display.flip()

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
