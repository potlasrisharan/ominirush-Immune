from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import edge_tts

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "video" / "omniimmune_product_demo.mp4"
WIDTH, HEIGHT = 1920, 1080
FPS = 30

# Cyberpunk / Dark Resilience Palette
BG = (10, 14, 20)
PANEL_BG = (15, 23, 34)
CARD_BG = (18, 29, 43)
CRIMSON = (255, 59, 92)
CYAN = (0, 229, 255)
GREEN = (0, 230, 118)
AMBER = (255, 171, 0)
WHITE = (240, 246, 252)
MUTED = (139, 148, 158)
BORDER = (38, 54, 72)
BORDER_GLOW = (56, 80, 107)

SCENE_DEFS = [
    {
        "key": "server",
        "tag": "THE PROBLEM",
        "title": "Unit tests lie. Production crashes at 3 AM.",
        "voice": "Unit tests lie. Code passes CI, but at 3 AM in production, unexpected traffic spikes, unhandled panics, and upstream deadlocks take down critical services. Traditional APM monitoring only tells you when your system is already dead. What if your infrastructure could fight back and heal itself?",
        "subtitles": [
            (0.0, 5.0, "Unit tests lie. Code passes CI in staging...", ["Unit", "tests", "lie"]),
            (5.0, 11.0, "...but at 3 AM, unhandled panics and upstream deadlocks crash production.", ["3", "AM", "deadlocks", "crash"]),
            (11.0, 16.0, "Traditional APM monitoring only alerts you after you are already down.", ["Traditional", "APM"]),
            (16.0, 21.0, "What if your infrastructure could fight back and heal itself?", ["fight", "back", "heal"]),
        ],
    },
    {
        "key": "architecture",
        "tag": "THE ARCHITECTURE",
        "title": "Autonomous Closed-Loop Resilience: Break it. Heal it. Prove it.",
        "voice": "Introducing OmniImmune: an autonomous closed-loop resilience engine built for mission-critical infrastructure. Its core thesis is simple: Break it, Heal it, Prove it. It deploys competing Red and Blue AI agent swarms to continuously inoculate your services against catastrophic failures.",
        "subtitles": [
            (0.0, 5.5, "Introducing OmniImmune: Autonomous closed-loop cyber resilience.", ["OmniImmune", "resilience"]),
            (5.5, 11.0, "Core thesis: Break it. Heal it. Prove it.", ["Break", "it", "Heal", "Prove"]),
            (11.0, 19.0, "Competing Red & Blue agent swarms continuously inoculate your services.", ["Red", "Blue", "agent", "swarms"]),
        ],
    },
    {
        "key": "chaos",
        "tag": "PHASE 1 · BREAK IT",
        "title": "Deterministic Chaos Swarm: Fingerprinting AttackDNA.",
        "voice": "First, the Red Swarm deploys deterministic chaos. FuzzNet floods boundary payloads triggering an unhandled order panic. StormLoad stresses concurrency locks until response times breach SLA limits. And our async latency proxy simulates upstream network failure. The target crashes, and each incident is fingerprinted into cryptographic AttackDNA.",
        "subtitles": [
            (0.0, 5.0, "Red Swarm deploys deterministic chaos across live endpoints.", ["Red", "Swarm", "chaos"]),
            (5.0, 11.0, "FuzzNet triggers an unhandled ledger panic with boundary data.", ["FuzzNet", "panic"]),
            (11.0, 17.0, "StormLoad forces concurrency starvation until SLA breaches.", ["StormLoad", "concurrency", "SLA"]),
            (17.0, 24.5, "LatencyProxy simulates upstream failure — fingerprinted into AttackDNA.", ["LatencyProxy", "AttackDNA"]),
        ],
    },
    {
        "key": "heal",
        "tag": "PHASE 2 & 3 · HEAL IT & PROVE IT",
        "title": "LibCST Semantic Healing & Adversarial Escalation Proof.",
        "voice": "Next, the Blue Swarm triages the AST and applies a LibCST semantic patch, preserving clean code formatting. But a patch is not enough: the Arbiter replays the exact AttackDNA to verify recovery, checks golden-path regressions, and runs a three-round Adversarial Escalation with Unicode and type confusion mutations to certify generalized immunity without overfitting.",
        "subtitles": [
            (0.0, 6.0, "Blue Swarm triages the AST and synthesizes a LibCST semantic patch.", ["Blue", "Swarm", "LibCST", "patch"]),
            (6.0, 12.0, "Formatting & comments preserved. Zero syntax degradation.", ["Formatting", "comments", "preserved"]),
            (12.0, 18.0, "Arbiter replays exact AttackDNA to verify golden endpoints.", ["Arbiter", "replays", "verify"]),
            (18.0, 24.5, "Three-round Adversarial Escalation certifies generalized immunity.", ["Adversarial", "Escalation", "generalized", "immunity"]),
        ],
    },
    {
        "key": "git",
        "tag": "PERSISTENCE · ZERO-TRUST VAULT",
        "title": "Isolated Git Repair Branches & O(1) Immune Memory.",
        "voice": "Verified repairs are automatically committed to isolated Git branches with cryptographic audit trails ready for one-click human review. And when the Red Swarm strikes again? Immune Memory delivers instant O one vaccination with zero downtime and zero regressions.",
        "subtitles": [
            (0.0, 5.5, "Repairs committed to isolated Git branches (heal/sig-*) with audit trails.", ["isolated", "Git", "branches", "audit"]),
            (5.5, 11.0, "HMAC-SHA256 signed memory vault prevents tampering.", ["HMAC-SHA256", "signed", "vault"]),
            (11.0, 17.0, "Subsequent attacks hit Immune Memory: Instant O(1) vaccination.", ["Instant", "O(1)", "vaccination"]),
        ],
    },
    {
        "key": "summary",
        "tag": "MISSION CONTROL",
        "title": "100/100 Immunity Score · Docker-Reproducible Resilience.",
        "voice": "OmniImmune transforms fragile code into self-healing infrastructure. With 95 tests passing, zero-trust cryptographic memory, and single-command Docker reproducibility, it is built for real-world resilience. Explore the project on GitHub.",
        "subtitles": [
            (0.0, 5.0, "OmniImmune transforms fragile code into self-healing infrastructure.", ["OmniImmune", "self-healing"]),
            (5.0, 11.0, "95/95 tests passing · Zero-trust memory · Single-command Docker demo.", ["95/95", "tests", "Zero-trust", "Docker"]),
            (11.0, 17.0, "Explore OmniImmune on GitHub: Systems, Infrastructure & Cybersecurity.", ["GitHub", "Systems", "Infrastructure", "Cybersecurity"]),
        ],
    },
]

def load_font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("/System/Library/Fonts/SFNSMono.ttf" if mono else "/System/Library/Fonts/SFNS.ttf"),
        "/System/Library/Fonts/Menlo.ttc" if mono else "/System/Library/Fonts/Helvetica.ttc",
    )
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()

FONTS = {
    "display": load_font(64, False),
    "h1": load_font(44, False),
    "h2": load_font(32, False),
    "h3": load_font(24, False),
    "body": load_font(26, False),
    "caption": load_font(20, False),
    "code": load_font(22, True),
    "code_sm": load_font(18, True),
    "score": load_font(140, False),
}

def lerp(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))

def draw_header(image: Image.Image, tag: str, title: str, scene_idx: int) -> ImageDraw.ImageDraw:
    draw = ImageDraw.Draw(image)
    # Background subtle radial/vignette gradient
    w, h = image.size
    for y in range(0, h, 4):
        t = y / h
        draw.rectangle((0, y, w, y + 4), fill=lerp(BG, (16, 24, 36), t * 0.7))
    # Subtle tech grid
    for x in range(0, w, 64):
        draw.line((x, 0, x, h), fill=(20, 30, 44), width=1)
    for y in range(0, h, 64):
        draw.line((0, y, w, y), fill=(20, 30, 44), width=1)

    # Top brand bar
    draw.rectangle((80, 50, 100, 70), fill=CYAN)
    draw.text((112, 48), "OMNIIMMUNE", font=FONTS["h3"], fill=CYAN)
    draw.text((310, 52), "·   AUTONOMOUS CHAOS INOCULATION", font=FONTS["caption"], fill=MUTED)
    draw.text((w - 220, 48), f"0{scene_idx + 1}  /  06", font=FONTS["h3"], fill=MUTED)
    draw.line((80, 88, w - 80, 88), fill=BORDER, width=2)

    # Scene Title
    draw.text((80, 108), tag, font=FONTS["caption"], fill=AMBER)
    draw.text((80, 138), title, font=FONTS["h1"], fill=WHITE)
    return draw

def draw_subtitles(draw: ImageDraw.ImageDraw, current_time: float, subs: list[tuple[float, float, str, list[str]]]):
    for start, end, text, accents in subs:
        if start <= current_time < end:
            box = (160, HEIGHT - 130, WIDTH - 160, HEIGHT - 45)
            draw.rounded_rectangle(box, radius=18, fill=(8, 12, 18), outline=BORDER_GLOW, width=2)
            words = text.split()
            pieces = []
            for w in words:
                clean = w.strip(".,?!:;()")
                pieces.append((w + " ", CYAN if clean in accents else WHITE))
            total_w = sum(draw.textlength(w, font=FONTS["body"]) for w, _ in pieces)
            cur_x = (WIDTH - total_w) / 2
            for word, col in pieces:
                draw.text((cur_x, HEIGHT - 100), word, font=FONTS["body"], fill=col)
                cur_x += draw.textlength(word, font=FONTS["body"])
            break

# ----------------- SCENE RENDERERS -----------------

def render_server_scene(frame: int, total_frames: int, scene_data: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = draw_header(img, scene_data["tag"], scene_data["title"], 0)
    t = frame / max(total_frames, 1)

    # Left: 3D Server Racks with pulsing alarm lights
    for rack in range(4):
        rx = 90 + rack * 200
        draw.rounded_rectangle((rx, 220, rx + 165, 880), radius=12, fill=(18, 25, 36), outline=BORDER, width=2)
        for unit in range(12):
            uy = 245 + unit * 51
            draw.rounded_rectangle((rx + 15, uy, rx + 150, uy + 38), radius=6, fill=(12, 18, 26), outline=(32, 45, 60))
            is_failing = (unit + rack + int(frame / 6)) % 4 == 0
            led_color = CRIMSON if is_failing else (40, 180, 120)
            draw.ellipse((rx + 28, uy + 14, rx + 38, uy + 24), fill=led_color)
            draw.line((rx + 50, uy + 19, rx + 135, uy + 19), fill=(40, 58, 76), width=3)

    # Right: Production Failure Terminal
    tx1, ty1, tx2, ty2 = 940, 220, 1840, 880
    draw.rounded_rectangle((tx1, ty1, tx2, ty2), radius=16, fill=(8, 12, 17), outline=CRIMSON, width=2)
    # macOS window dots
    draw.ellipse((tx1 + 24, ty1 + 20, tx1 + 38, ty1 + 34), fill=CRIMSON)
    draw.ellipse((tx1 + 48, ty1 + 20, tx1 + 62, ty1 + 34), fill=AMBER)
    draw.ellipse((tx1 + 72, ty1 + 20, tx1 + 86, ty1 + 34), fill=GREEN)
    draw.text((tx1 + 110, ty1 + 18), "production-k8s-us-east-1 · uvicorn.log", font=FONTS["code_sm"], fill=MUTED)
    draw.line((tx1, ty1 + 54, tx2, ty1 + 54), fill=BORDER, width=1)

    terminal_lines = [
        ("03:14:02.108  [INFO]   127.0.0.1:48201 - POST /api/v1/order HTTP/1.1", MUTED),
        ("03:14:02.112  [CRIT]   Traceback (most recent call last):", CRIMSON),
        ("               File 'target_app/app.py', line 38, in create_order", MUTED),
        ("               raise ValueError('CRITICAL_ORDER_PANIC: Negative quantity!')", CRIMSON),
        ("03:14:02.115  [ERROR]  HTTP 500 INTERNAL SERVER ERROR", CRIMSON),
        ("", WHITE),
        ("03:14:03.420  [PAGER]  🚨 SEV-1 OUTAGE: Order Ledger Crash Rate > 95%", AMBER),
        ("03:14:04.890  [NET]    Upstream latency drift detected: 6,242ms (SLA 1500ms)", AMBER),
        ("03:14:05.100  [HEALTH] /health failed · Container status: UNHEALTHY", CRIMSON),
    ]
    cur_y = ty1 + 80
    visible_count = min(len(terminal_lines), int(t * len(terminal_lines) * 1.5) + 3)
    for i in range(visible_count):
        text, col = terminal_lines[i]
        draw.text((tx1 + 30, cur_y), text, font=FONTS["code"], fill=col)
        if i == visible_count - 1 and (frame // 10) % 2 == 0:
            tw = draw.textlength(text, font=FONTS["code"])
            draw.text((tx1 + 30 + tw + 6, cur_y), "▋", font=FONTS["code"], fill=GREEN)
        cur_y += 38

    draw_subtitles(draw, frame / FPS, scene_data["subtitles"])
    return img

def render_arch_scene(frame: int, total_frames: int, scene_data: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = draw_header(img, scene_data["tag"], scene_data["title"], 1)

    nodes = [
        (350, 520, "RED SWARM", "Chaos Injection", CRIMSON),
        (960, 520, "ARBITER", "Closed-Loop Replay", WHITE),
        (1570, 520, "BLUE SWARM", "LibCST Healer", CYAN),
    ]

    # Draw Connecting conduits
    draw.line((350, 520, 960, 520), fill=CRIMSON, width=4)
    draw.line((960, 520, 1570, 520), fill=CYAN, width=4)

    # Animated Energy pulses along conduits
    phase = (frame % 45) / 45
    pulse_x1 = int(350 + (960 - 350) * phase)
    draw.ellipse((pulse_x1 - 12, 508, pulse_x1 + 12, 532), fill=CRIMSON)
    pulse_x2 = int(960 + (1570 - 960) * phase)
    draw.ellipse((pulse_x2 - 12, 508, pulse_x2 + 12, 532), fill=CYAN)

    # Draw Nodes
    pulse = (math.sin(frame / 6) + 1) / 2
    for nx, ny, name, role, col in nodes:
        glow_r = int(140 + pulse * 14)
        draw.ellipse((nx - glow_r, ny - glow_r, nx + glow_r, ny + glow_r), outline=col, width=2)
        draw.ellipse((nx - 120, ny - 120, nx + 120, ny + 120), fill=PANEL_BG, outline=col, width=4)
        tw = draw.textlength(name, font=FONTS["h2"])
        draw.text((nx - tw / 2, ny - 24), name, font=FONTS["h2"], fill=col)
        rw = draw.textlength(role, font=FONTS["caption"])
        draw.text((nx - rw / 2, ny + 16), role, font=FONTS["caption"], fill=MUTED)

    # Bottom Vault / Storage node
    vx, vy = 960, 800
    draw.line((960, 640, 960, 740), fill=GREEN, width=4)
    draw.rounded_rectangle((vx - 260, vy - 60, vx + 260, vy + 60), radius=16, fill=CARD_BG, outline=GREEN, width=3)
    draw.text((vx - 220, vy - 35), "IMMUNE MEMORY VAULT", font=FONTS["h3"], fill=GREEN)
    draw.text((vx - 220, vy + 4), "HMAC-SHA256 Cryptographic Signatures · O(1) Vaccination", font=FONTS["caption"], fill=MUTED)

    draw_subtitles(draw, frame / FPS, scene_data["subtitles"])
    return img

def render_chaos_scene(frame: int, total_frames: int, scene_data: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = draw_header(img, scene_data["tag"], scene_data["title"], 2)

    # Left: Terminal Execution of Chaos
    bx1, by1, bx2, by2 = 80, 220, 1080, 880
    draw.rounded_rectangle((bx1, by1, bx2, by2), radius=16, fill=(8, 12, 17), outline=CRIMSON, width=2)
    draw.text((bx1 + 30, by1 + 20), "OmniImmune Red Swarm Orchestrator · Attack Wave #01", font=FONTS["code_sm"], fill=CRIMSON)
    draw.line((bx1, by1 + 54, bx2, by1 + 54), fill=BORDER, width=1)

    lines = [
        ("$ docker compose up --build -d", CYAN),
        ("✓ Target container [omniimmune-target:8000] healthy", GREEN),
        ("✓ Upstream payment gateway [omniimmune-upstream:9000] healthy", GREEN),
        ("", WHITE),
        (">>> DEPLOYING CHAOS VECTOR 1: FuzzNet", CRIMSON),
        ("    Payload: {'item_id': 'item-1', 'quantity': -5}", MUTED),
        ("    → 500 INTERNAL SERVER ERROR (Panic in target_app/app.py:38)", CRIMSON),
        ("    → Cryptographic AttackDNA: dna-e1cd4cfc677c (SHA-256 registered)", CYAN),
        ("", WHITE),
        (">>> DEPLOYING CHAOS VECTOR 2: StormLoad", AMBER),
        ("    Workload: 20 concurrent tasks against /api/v1/inventory/reserve", MUTED),
        ("    → Concurrency starvation: p99 latency 6,242ms > SLA 1,500ms", AMBER),
        ("", WHITE),
        (">>> DEPLOYING CHAOS VECTOR 3: LatencyDriftProxy", CRIMSON),
        ("    Injecting 5.0s upstream network jitter and packet drop", MUTED),
        ("    → Uncapped client timeout breach", CRIMSON),
    ]
    cy = by1 + 75
    for text, col in lines:
        draw.text((bx1 + 30, cy), text, font=FONTS["code_sm"], fill=col)
        cy += 34

    # Right: Attack Radar & Metrics
    rx1, ry1, rx2, ry2 = 1130, 220, 1840, 880
    draw.rounded_rectangle((rx1, ry1, rx2, ry2), radius=16, fill=PANEL_BG, outline=BORDER, width=2)
    draw.text((rx1 + 40, ry1 + 30), "CHAOS TELEMETRY & VULNERABILITY RADAR", font=FONTS["h3"], fill=WHITE)

    # Dynamic sweeping radar HUD in top right
    cx, cy = rx2 - 75, ry1 + 50
    draw.ellipse((cx - 36, cy - 36, cx + 36, cy + 36), outline=(32, 65, 85), width=2)
    draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline=(24, 50, 70), width=1)
    draw.line((cx - 36, cy, cx + 36, cy), fill=(24, 50, 70), width=1)
    draw.line((cx, cy - 36, cx, cy + 36), fill=(24, 50, 70), width=1)
    radar_angle = (frame * 0.12) % (2 * math.pi)
    draw.line((cx, cy, cx + int(34 * math.cos(radar_angle)), cy + int(34 * math.sin(radar_angle))), fill=CYAN, width=2)
    draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=CRIMSON)

    vectors = [
        ("FuzzNet Boundary Panic", 1.0, CRIMSON, "Ledger crash (500)"),
        ("StormLoad Lock Starvation", 0.92, AMBER, "p99: 6,242ms (SLA Breach)"),
        ("LatencyDrift Timeout", 0.98, CRIMSON, "Connection Hang (500)"),
    ]
    for i, (name, val, col, status) in enumerate(vectors):
        vy = ry1 + 100 + i * 140
        draw.text((rx1 + 40, vy), name, font=FONTS["body"], fill=WHITE)
        draw.text((rx2 - 320, vy), status, font=FONTS["caption"], fill=col)
        draw.rounded_rectangle((rx1 + 40, vy + 40, rx2 - 40, vy + 68), radius=10, fill=(12, 18, 26), outline=BORDER)
        fill_w = int((rx2 - rx1 - 80) * val)
        draw.rounded_rectangle((rx1 + 40, vy + 40, rx1 + 40 + fill_w, vy + 68), radius=10, fill=col)

    # AttackDNA Box
    draw.rounded_rectangle((rx1 + 40, ry1 + 540, rx2 - 40, ry2 - 30), radius=12, fill=CARD_BG, outline=CYAN, width=2)
    draw.text((rx1 + 60, ry1 + 560), "CRYPTOGRAPHIC ATTACK DNA", font=FONTS["caption"], fill=CYAN)
    draw.text((rx1 + 60, ry1 + 595), "DNA-ID:  dna-e1cd4cfc677c", font=FONTS["code"], fill=WHITE)
    draw.text((rx1 + 60, ry1 + 630), "SHA256:  e1cd4cfc677c1af53b3396efe47ade05...", font=FONTS["code_sm"], fill=MUTED)

    draw_subtitles(draw, frame / FPS, scene_data["subtitles"])
    return img

def render_heal_scene(frame: int, total_frames: int, scene_data: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = draw_header(img, scene_data["tag"], scene_data["title"], 3)

    # Left: LibCST Syntax Diff Inspector
    dx1, dy1, dx2, dy2 = 80, 220, 1020, 880
    draw.rounded_rectangle((dx1, dy1, dx2, dy2), radius=16, fill=(8, 12, 17), outline=CYAN, width=2)
    draw.text((dx1 + 30, dy1 + 20), "LibCST AST Transformer · target_app/app.py", font=FONTS["code_sm"], fill=CYAN)
    draw.line((dx1, dy1 + 54, dx2, dy1 + 54), fill=BORDER, width=1)

    diff_code = [
        ("@@ -36,8 +36,11 @@ def create_order(order: OrderRequest):", MUTED),
        ("-    if order.quantity < 0:", CRIMSON),
        ("-        raise ValueError('CRITICAL_ORDER_PANIC: Negative quantity!')", CRIMSON),
        ("+    if order.quantity <= 0:", GREEN),
        ("+        return JSONResponse(", GREEN),
        ("+            status_code=400,", GREEN),
        ("+            content={'status': 'rejected', 'error': 'invalid_quantity'},", GREEN),
        ("+        )", GREEN),
        ("     ledger.append(order)", WHITE),
        ("     return {'order_id': order.id, 'status': 'created'}", WHITE),
        ("", WHITE),
        ("✓ LibCST AST parsed and patched cleanly in-memory", CYAN),
        ("✓ Comments and docstrings preserved without formatting rot", CYAN),
        ("✓ Hot container reload verified via Docker socket", GREEN),
    ]
    dy = dy1 + 75
    for line, col in diff_code:
        draw.text((dx1 + 30, dy), line, font=FONTS["code_sm"], fill=col)
        dy += 34

    # Right: Adversarial Escalation Round
    ex1, ey1, ex2, ey2 = 1070, 220, 1840, 880
    draw.rounded_rectangle((ex1, ey1, ex2, ey2), radius=16, fill=PANEL_BG, outline=GREEN, width=2)
    draw.text((ex1 + 40, ey1 + 30), "ADVERSARIAL ESCALATION ROUND", font=FONTS["h3"], fill=GREEN)
    draw.text((ex1 + 40, ey1 + 65), "Certifying Generalized Immunity (No Overfitting)", font=FONTS["caption"], fill=MUTED)

    mutations = [
        ("Mutation 01: Numeric Boundary", "quantity: 1,000,000,000,000,000,000", "SURVIVED (HTTP 200)", GREEN),
        ("Mutation 02: Unicode Injection", "item_id: '\\u0000\\x7f<script>'", "REJECTED SAFELY (422)", GREEN),
        ("Mutation 03: Type Confusion", "payload: {'quantity': {'nested': -1}}", "VALIDATED SAFELY (422)", GREEN),
    ]
    for i, (m_title, m_desc, m_res, col) in enumerate(mutations):
        my = ey1 + 120 + i * 140
        draw.rounded_rectangle((ex1 + 35, my, ex2 - 35, my + 115), radius=12, fill=CARD_BG, outline=BORDER)
        draw.text((ex1 + 55, my + 18), m_title, font=FONTS["body"], fill=WHITE)
        draw.text((ex1 + 55, my + 52), m_desc, font=FONTS["code_sm"], fill=MUTED)
        draw.text((ex2 - 340, my + 18), m_res, font=FONTS["caption"], fill=col)

    # Certification Banner
    draw.rounded_rectangle((ex1 + 35, ey1 + 570, ex2 - 35, ey2 - 30), radius=12, fill=(12, 38, 24), outline=GREEN, width=2)
    draw.text((ex1 + 60, ey1 + 595), "GENERALIZED IMMUNITY CERTIFIED", font=FONTS["h3"], fill=GREEN)
    draw.text((ex1 + 60, ey1 + 630), "Zero regression against golden endpoints /health & /catalog", font=FONTS["caption"], fill=WHITE)

    draw_subtitles(draw, frame / FPS, scene_data["subtitles"])
    return img

def render_git_scene(frame: int, total_frames: int, scene_data: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = draw_header(img, scene_data["tag"], scene_data["title"], 4)

    # Left: Git Branch DAG
    gx1, gy1, gx2, gy2 = 80, 220, 1020, 880
    draw.rounded_rectangle((gx1, gy1, gx2, gy2), radius=16, fill=PANEL_BG, outline=BORDER, width=2)
    draw.text((gx1 + 40, gy1 + 30), "ISOLATED GIT PERSISTENCE GRAPH", font=FONTS["h3"], fill=WHITE)

    # Draw Main branch
    draw.line((180, 560, 920, 560), fill=WHITE, width=6)
    draw.ellipse((168, 548, 192, 572), fill=WHITE)
    draw.text((120, 595), "main", font=FONTS["body"], fill=WHITE)

    # Forked Heal Branches
    branches = [
        (380, 560, 680, 400, "heal/sig-6303d35c7823", CYAN),
        (380, 560, 680, 720, "heal/sig-5f5826418cbd", GREEN),
    ]
    for x1, y1, x2, y2, bname, col in branches:
        draw.line((x1, y1, x2, y2), fill=col, width=5)
        draw.ellipse((x2 - 14, y2 - 14, x2 + 14, y2 + 14), fill=col)
        draw.text((x2 + 25, y2 - 15), bname, font=FONTS["code_sm"], fill=col)

    # Right: Audit Commit & Zero-Trust Vault
    rx1, ry1, rx2, ry2 = 1070, 220, 1840, 880
    draw.rounded_rectangle((rx1, ry1, rx2, ry2), radius=16, fill=(8, 12, 17), outline=GREEN, width=2)
    draw.text((rx1 + 35, ry1 + 25), "CRYPTOGRAPHIC AUDIT COMMIT ON REPAIR BRANCH", font=FONTS["code_sm"], fill=GREEN)
    draw.line((rx1, ry1 + 54, rx2, ry1 + 54), fill=BORDER, width=1)

    commit_lines = [
        ("commit  10e389e8dbd85295764fd6da539a03648b204171", CYAN),
        ("Author:  potlasrisharan <potlasrisharan@omniimmune>", MUTED),
        ("Branch:  heal/sig-5f5826418cbd (isolated, non-checked-out)", WHITE),
        ("", WHITE),
        ("    fix(resilience): auto-heal sig-5f5826418cbd using asyncio_lock", GREEN),
        ("", WHITE),
        ("    - Signature ID:            sig-5f5826418cbd", MUTED),
        ("    - Triggering DNA SHA-256:  2faefb65f10a51d5f9f497f8...", MUTED),
        ("    - Pre-heal Error Rate:     100.0%", CRIMSON),
        ("    - Post-heal Survival Rate: 100.0%", GREEN),
        ("    - Latency p99:             574.1ms (reduced from 6,242ms)", GREEN),
        ("    - Baseline Verification:   PASSED", GREEN),
        ("    - Escalation Verification: PASSED", GREEN),
        ("    - Golden Regression:       PASSED", GREEN),
        ("    - Vault HMAC Signature:    VERIFIED ✓", CYAN),
    ]
    cy = ry1 + 80
    for text, col in commit_lines:
        draw.text((rx1 + 35, cy), text, font=FONTS["code_sm"], fill=col)
        cy += 34

    draw_subtitles(draw, frame / FPS, scene_data["subtitles"])
    return img

def render_summary_scene(frame: int, total_frames: int, scene_data: dict) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = draw_header(img, scene_data["tag"], scene_data["title"], 5)

    # Huge Score Meter
    sx1, sy1, sx2, sy2 = 80, 220, 720, 880
    draw.rounded_rectangle((sx1, sy1, sx2, sy2), radius=20, fill=PANEL_BG, outline=GREEN, width=4)
    draw.text((sx1 + 50, sy1 + 50), "IMMUNITY SCORE", font=FONTS["h2"], fill=MUTED)
    draw.text((sx1 + 40, sy1 + 120), "100", font=FONTS["score"], fill=GREEN)
    draw.text((sx1 + 380, sy1 + 220), "/ 100", font=FONTS["h1"], fill=MUTED)
    draw.text((sx1 + 50, sy1 + 340), "ALL KNOWN PRODUCTION THREATS NEUTRALIZED", font=FONTS["caption"], fill=WHITE)
    draw.text((sx1 + 50, sy1 + 380), "Zero-downtime LibCST inoculation verified.", font=FONTS["caption"], fill=MUTED)

    # Right Side: Verified Metric Badges
    cards = [
        ("95 / 95", "OFFLINE SUITE PASSING", "tests/ unit & integration", GREEN),
        ("3 / 3", "INCIDENTS REPAIRED", "Panic, Deadlock & Timeout", CYAN),
        ("0", "REGRESSIONS DETECTED", "Golden endpoints 100% intact", GREEN),
        ("O(1)", "IMMUNE MEMORY HIT", "Zero-trust HMAC signed vault", AMBER),
    ]
    for i, (val, title, subtitle_text, col) in enumerate(cards):
        col_idx = i % 2
        row_idx = i // 2
        cx1 = 760 + col_idx * 550
        cy1 = 220 + row_idx * 220
        cx2 = cx1 + 510
        cy2 = cy1 + 190
        draw.rounded_rectangle((cx1, cy1, cx2, cy2), radius=16, fill=PANEL_BG, outline=col, width=2)
        draw.text((cx1 + 35, cy1 + 25), val, font=FONTS["display"], fill=col)
        draw.text((cx1 + 35, cy1 + 110), title, font=FONTS["h3"], fill=WHITE)
        draw.text((cx1 + 35, cy1 + 145), subtitle_text, font=FONTS["caption"], fill=MUTED)

    # Bottom Banner
    bx1, by1, bx2, by2 = 760, 680, 1820, 880
    draw.rounded_rectangle((bx1, by1, bx2, by2), radius=16, fill=CARD_BG, outline=CYAN, width=2)
    draw.text((bx1 + 40, by1 + 30), "GITHUB REPOSITORY", font=FONTS["caption"], fill=CYAN)
    draw.text((bx1 + 40, by1 + 65), "github.com/potlasrisharan/ominirush-Immune", font=FONTS["h2"], fill=WHITE)
    draw.text((bx1 + 40, by1 + 125), "Single command demo:  docker compose up --build", font=FONTS["code_sm"], fill=AMBER)

    draw_subtitles(draw, frame / FPS, scene_data["subtitles"])
    return img

SCENE_RENDERERS = {
    "server": render_server_scene,
    "architecture": render_arch_scene,
    "chaos": render_chaos_scene,
    "heal": render_heal_scene,
    "git": render_git_scene,
    "summary": render_summary_scene,
}

async def generate_voiceovers(work: Path) -> list[tuple[Path, float]]:
    results = []
    for i, sc in enumerate(SCENE_DEFS):
        chunk_path = work / f"voice_{i:02d}.mp3"
        comm = edge_tts.Communicate(sc["voice"], "en-US-ChristopherNeural", rate="+4%")
        await comm.save(str(chunk_path))
        # Get duration via ffprobe
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(chunk_path)
        ]
        dur = float(subprocess.check_output(cmd).decode().strip())
        results.append((chunk_path, dur))
        print(f"Generated Voiceover Chunk {i+1}/6 ({dur:.2f}s)")
    return results

def make_ambient_soundtrack(work: Path, total_duration: float) -> Path:
    synth_path = work / "ambient_synth.wav"
    # Harmonic progression with A-minor / F-maj sub-bass pads and soft lowpass
    graph = (
        "[0:a]volume=0.035[pad1];[1:a]volume=0.022[pad2];[2:a]volume=0.015[pad3];"
        "[pad1][pad2][pad3]amix=inputs=3:duration=longest,"
        "lowpass=f=450,aecho=0.8:0.9:800:0.35,afade=t=in:st=0:d=4,afade=t=out:st=114:d=6"
    )
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=110:duration={total_duration}",
        "-f", "lavfi", "-i", f"sine=frequency=164.81:duration={total_duration}",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={total_duration}",
        "-filter_complex", graph, "-ar", "44100", "-ac", "2", str(synth_path),
    ], check=True, cwd=ROOT)
    return synth_path

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Render 1080p Full HD OmniImmune demo video")
    parser.add_argument("--voiceover", help="Path to custom ElevenLabs voiceover audio file (MP3 or WAV)")
    args = parser.parse_args()

    print("=== Starting Pro Demo Video Generation (1080p, Enhanced Visuals) ===")
    with tempfile.TemporaryDirectory(prefix="omniimmune-pro-") as temp_dir:
        work = Path(temp_dir)
        
        full_voice = work / "voice_full.wav"
        if args.voiceover and Path(args.voiceover).exists():
            custom_voice = Path(args.voiceover).resolve()
            print(f"Using custom ElevenLabs voiceover: {custom_voice}")
            subprocess.run([
                "ffmpeg", "-y", "-i", str(custom_voice),
                "-ar", "44100", "-ac", "2", str(full_voice)
            ], check=True, cwd=ROOT)
            dur_cmd = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(full_voice)
            ]
            total_audio = float(subprocess.check_output(dur_cmd).decode().strip())
            video_duration = math.ceil(total_audio)
            # Distribute scene durations based on standard proportions
            proportions = [20.5, 18.7, 24.5, 23.9, 16.7, 16.7]
            prop_sum = sum(proportions)
            scene_durations = [(p / prop_sum) * video_duration for p in proportions]
        else:
            voice_chunks = await generate_voiceovers(work)
            total_audio = sum(dur for _, dur in voice_chunks)
            video_duration = 120.0
            padding_per_scene = (video_duration - total_audio) / len(voice_chunks)
            scene_durations = [dur + padding_per_scene for _, dur in voice_chunks]

            concat_file = work / "voice_concat.txt"
            with open(concat_file, "w") as f:
                for p, _ in voice_chunks:
                    f.write(f"file '{p.resolve()}'\n")
            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-ar", "44100", "-ac", "2", str(full_voice)
            ], check=True, cwd=ROOT)

        frames_dir = work / "frames"
        frames_dir.mkdir()
        
        global_frame = 0
        for i, (sc, dur) in enumerate(zip(SCENE_DEFS, scene_durations)):
            scene_frames = int(round(dur * FPS))
            print(f"Rendering Scene {i+1}/6: {sc['tag']} ({scene_frames} frames)...")
            renderer = SCENE_RENDERERS[sc["key"]]
            for local_f in range(scene_frames):
                img = renderer(local_f, scene_frames, sc)
                img.save(frames_dir / f"frame_{global_frame:05d}.png", quality=95)
                global_frame += 1

        print(f"Rendered {global_frame} total Full HD frames.")
        silent_video = work / "silent.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(FPS),
            "-i", str(frames_dir / "frame_%05d.png"),
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-r", str(FPS),
            str(silent_video)
        ], check=True, cwd=ROOT)

        synth_music = make_ambient_soundtrack(work, video_duration)

        # Mux Video + Voice + Ambient Audio Bed
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        print("Muxing final 1080p video with AAC stereo audio...")
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(silent_video),
            "-i", str(full_voice),
            "-i", str(synth_music),
            "-filter_complex",
            f"[1:a]apad=pad_dur={video_duration},atrim=duration={video_duration},volume=1.3[voice];"
            f"[2:a]atrim=duration={video_duration},volume=0.3[music];"
            "[voice][music]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-t", str(video_duration),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "256k",
            "-movflags", "+faststart",
            str(OUTPUT)
        ], check=True, cwd=ROOT)

        print(f"SUCCESS: High-quality demo video created at {OUTPUT}")
        print(f"Size: {OUTPUT.stat().st_size / 1024 / 1024:.2f} MB")

if __name__ == "__main__":
    asyncio.run(main())
