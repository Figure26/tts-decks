#!/usr/bin/env python3
"""Build a Tabletop Simulator deck from a CSV.

CSV columns: Name, Effect, Starting count  (tab, comma or semicolon separated)

  make_deck.py cards/resource.csv            # render, push to GitHub, write Saved Object
  make_deck.py cards/resource.csv --no-push  # local file:// URLs, for testing alone
  make_deck.py --watch cards/                # build every CSV dropped into the folder

A CSV that built successfully is moved into a done/ folder next to it (--keep to
leave it in place), so a watched folder only ever holds CSVs that still need
building. A failed CSV stays put and is retried once it is edited.

The card faces are tiled into 10x7 sheets, committed under images/ in this repo and
referenced through raw.githubusercontent.com, so every player in a multiplayer game
can load them. File names contain a content hash: TTS caches images by URL, so an
edited CSV must produce a new URL.
"""
import argparse
import csv
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent
IMAGES = REPO / "images"
DEFAULT_OUT = Path.home() / ".local/share/Tabletop Simulator/Saves/Saved Objects/test"

CARD_W, CARD_H = 409, 585          # one cell; a full 10x7 sheet is ~4090x4095
COLS, ROWS = 10, 7
PER_SHEET = COLS * ROWS - 1        # TTS reserves the last slot for the hidden face

FONT_DIR = Path("/usr/share/fonts/noto")
SERIF_BOLD = FONT_DIR / "NotoSerif-Bold.ttf"
SANS = FONT_DIR / "NotoSans-Regular.ttf"
SANS_BOLD = FONT_DIR / "NotoSans-Bold.ttf"

CREAM = (247, 241, 227)
INK = (38, 34, 28)

# Mid/dark tones so the cream text on the back stays readable
PALETTE = {
    "green": "#6e8b3d", "blue": "#3d6a8b", "red": "#8b3d3d", "purple": "#6a3d8b",
    "orange": "#b8742a", "teal": "#2f7f7a", "brown": "#7a5a3a", "gold": "#a88a2a",
    "pink": "#a3486e", "navy": "#2c3e66", "gray": "#5f6368", "black": "#2b2b2b",
}
DEFAULT_COLOR = "green"
COLORS_FILE = REPO / "colors.json"   # deck name -> chosen colour, so each deck is asked only once


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def darker(rgb, f=0.55):
    return tuple(int(c * f) for c in rgb)


def parse_color(s):
    """Palette name, palette number (1-based) or #rrggbb; None if not recognised."""
    s = s.strip().lower()
    names = list(PALETTE)
    if s in PALETTE:
        return s
    if s.isdigit() and 1 <= int(s) <= len(names):
        return names[int(s) - 1]
    hexpart = s.lstrip("#")
    if len(hexpart) == 6 and all(c in "0123456789abcdef" for c in hexpart):
        return "#" + hexpart
    return None


def ask_color(deck_name):
    if not sys.stdin.isatty():
        print(f"New deck '{deck_name}': no terminal to ask, using {DEFAULT_COLOR}")
        return DEFAULT_COLOR
    print(f"\nNew deck detected: {deck_name}")
    for i, name in enumerate(PALETTE, start=1):
        r, g, b = hex_rgb(PALETTE[name])
        print(f"  {i:2}. \x1b[48;2;{r};{g};{b}m    \x1b[0m {name}")
    while True:
        answer = input(f"Color? (name, number or #hex, Enter = {DEFAULT_COLOR}) > ")
        if not answer.strip():
            return DEFAULT_COLOR
        color = parse_color(answer)
        if color:
            return color
        print(f"  '{answer}' is not a colour from the list or a #rrggbb hex code")


def resolve_color(deck_name, override):
    """Colour from --color, else the one remembered for this deck, else ask."""
    saved = json.loads(COLORS_FILE.read_text()) if COLORS_FILE.exists() else {}
    if override:
        color = parse_color(override)
        if not color:
            sys.exit(f"--color: unknown colour {override!r} (use one of {', '.join(PALETTE)} or #rrggbb)")
    elif deck_name in saved:
        color = saved[deck_name]
    else:
        color = ask_color(deck_name)
    if saved.get(deck_name) != color:
        saved[deck_name] = color
        COLORS_FILE.write_text(json.dumps(saved, indent=2, sort_keys=True) + "\n")
    return hex_rgb(PALETTE.get(color, color))


# --- CSV ---------------------------------------------------------------------

STANDARD_COLUMNS = {"name", "effect", "starting count"}


def deck_title(csv_path):
    """'CS decks - Season deck.csv' (a Google Sheets export) -> 'Season deck'."""
    stem = csv_path.stem.rsplit(" - ", 1)[-1]
    title = " ".join(stem.replace("_", " ").split())
    return title[:1].upper() + title[1:]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "deck"

def read_cards(path):
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters="\t,;")
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    cards = []
    for i, raw in enumerate(rows, start=2):
        raw = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        row = {k.lower(): v for k, v in raw.items()}
        name = row.get("name", "")
        if not name:
            continue
        count = row.get("starting count", "1") or "1"
        try:
            count = int(count)
        except ValueError:
            sys.exit(f"{path.name} line {i}: 'Starting count' is not a number: {count!r}")
        # Any other column is printed on the card as "Header: value"
        extra = [(k, v) for k, v in raw.items() if k and v and k.lower() not in STANDARD_COLUMNS]
        cards.append({"name": name, "effect": row.get("effect", ""), "count": count, "extra": extra})
    if not cards:
        sys.exit(f"{path.name}: no cards found (expected columns Name, Effect, Starting count)")
    return cards


# --- rendering -----------------------------------------------------------------

def fit_font(draw, text, font_path, size, max_w, min_size=18):
    while size > min_size:
        font = ImageFont.truetype(str(font_path), size)
        if draw.textlength(text, font=font) <= max_w:
            return font
        size -= 2
    return ImageFont.truetype(str(font_path), min_size)


def wrap_lines(draw, text, font, max_w):
    lines = []
    for para in text.split("\n"):
        words, line = para.split(), ""
        for w in words:
            trial = f"{line} {w}".strip()
            if draw.textlength(trial, font=font) <= max_w:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = w
        lines.append(line)
    return lines


def render_face(card, category, color):
    im = Image.new("RGB", (CARD_W, CARD_H), color)
    d = ImageDraw.Draw(im)
    m = 16
    d.rounded_rectangle((m, m, CARD_W - m, CARD_H - m), radius=14, fill=CREAM, outline=darker(color, 0.4), width=2)
    inner = CARD_W - 2 * m - 40
    cx = CARD_W // 2

    d.text((cx, 44), category.upper(), font=ImageFont.truetype(str(SANS_BOLD), 15), fill=darker(color), anchor="mm")
    title = fit_font(d, card["name"], SERIF_BOLD, 40, inner)
    d.text((cx, 98), card["name"], font=title, fill=INK, anchor="mm")
    d.line((cx - 100, 143, cx + 100, 143), fill=darker(color), width=2)

    # Extra columns sit at the bottom, above a thin rule
    body_top, body_bot = 165, CARD_H - m - 30
    if card["extra"]:
        y = CARD_H - m - 28
        for key, value in reversed(card["extra"]):
            d.text((cx, y), f"{key}: {value}", font=fit_font(d, f"{key}: {value}", SANS_BOLD, 20, inner, 12),
                   fill=darker(color), anchor="mm")
            y -= 30
        d.line((cx - 100, y + 6, cx + 100, y + 6), fill=darker(color), width=1)
        body_bot = y - 10

    # Effect: largest size whose wrapped text fits the body area
    for size in range(42, 15, -2):
        font = ImageFont.truetype(str(SANS), size)
        lines = wrap_lines(d, card["effect"], font, inner)
        line_h = int(size * 1.3)
        if line_h * len(lines) <= body_bot - body_top:
            break
    y = (body_top + body_bot) // 2 - line_h * len(lines) // 2 + line_h // 2
    for line in lines:
        d.text((cx, y), line, font=font, fill=INK, anchor="mm")
        y += line_h
    return im


def render_back(title, color):
    w, h = 700, 1000
    im = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((28, 28, w - 28, h - 28), radius=26, outline=CREAM, width=4)
    d.rounded_rectangle((44, 44, w - 44, h - 44), radius=18, outline=CREAM, width=2)
    font = fit_font(d, title, SERIF_BOLD, 96, w - 160, 30)
    d.text((w // 2, h // 2), title, font=font, fill=CREAM, anchor="mm")
    return im


def render_sheets(cards, category, color):
    sheets = []
    for start in range(0, len(cards), PER_SHEET):
        chunk = cards[start:start + PER_SHEET]
        cols = min(COLS, len(chunk) + 1)          # +1 keeps a free last slot
        rows = math.ceil((len(chunk) + 1) / cols)
        sheet = Image.new("RGB", (cols * CARD_W, rows * CARD_H), color)
        for i, card in enumerate(chunk):
            sheet.paste(render_face(card, category, color), ((i % cols) * CARD_W, (i // cols) * CARD_H))
        sheets.append((sheet, cols, rows, chunk))
    return sheets


def save_hashed(im, stem):
    IMAGES.mkdir(exist_ok=True)
    data = im.tobytes()
    digest = hashlib.sha1(data + repr(im.size).encode()).hexdigest()[:10]
    path = IMAGES / f"{stem}-{digest}.png"
    if not path.exists():
        im.save(path, optimize=True)
    return path


# --- GitHub --------------------------------------------------------------------

def git(*args, capture=False):
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, text=True,
                          capture_output=capture).stdout


def raw_base():
    url = git("remote", "get-url", "origin", capture=True).strip()
    slug = url.removesuffix(".git").split("github.com")[-1].lstrip(":/")
    branch = git("rev-parse", "--abbrev-ref", "HEAD", capture=True).strip()
    return f"https://raw.githubusercontent.com/{slug}/{branch}/"


def push(paths, deck_name):
    git("add", *[str(p.relative_to(REPO)) for p in paths])
    if git("diff", "--cached", "--name-only", capture=True).strip():
        git("commit", "-q", "-m", f"Deck images: {deck_name}")
    git("push", "-q", "origin", "HEAD")


# --- TTS JSON ------------------------------------------------------------------

def guid():
    return "%06x" % random.randrange(16 ** 6)


def transform(y=1.0):
    return {"posX": 0, "posY": y, "posZ": 0, "rotX": 0, "rotY": 180, "rotZ": 180,
            "scaleX": 1, "scaleY": 1, "scaleZ": 1}


def card_obj(card, card_id, custom_deck):
    return {
        "GUID": guid(), "Name": "Card", "Transform": transform(),
        "Nickname": card["name"], "Description": "\n".join([card["effect"]] + [f"{k}: {v}" for k, v in card["extra"]]).strip(), "GMNotes": "",
        "ColorDiffuse": {"r": 0.713235259, "g": 0.713235259, "b": 0.713235259},
        "Locked": False, "Grid": True, "Snap": True, "Autoraise": True, "Sticky": True,
        "Tooltip": True, "HideWhenFaceDown": True, "Hands": True, "SidewaysCard": False,
        "CardID": card_id, "CustomDeck": custom_deck,
        "LuaScript": "", "LuaScriptState": "", "XmlUI": "",
    }


def deck_id_base(name):
    # Stable per deck name, so two generated decks in one game don't share CustomDeck ids
    return 100 + int(hashlib.sha1(name.encode()).hexdigest(), 16) % 9000


def build_object(deck_name, sheets_info, back_url):
    base = deck_id_base(deck_name)
    custom, cards, ids = {}, [], []
    for n, (face_url, cols, rows, chunk) in enumerate(sheets_info):
        did = base + n
        entry = {"FaceURL": face_url, "BackURL": back_url, "NumWidth": cols, "NumHeight": rows,
                 "BackIsHidden": True, "UniqueBack": False, "Type": 0}
        custom[str(did)] = entry
        for i, card in enumerate(chunk):
            for _ in range(card["count"]):
                cid = did * 100 + i
                ids.append(cid)
                cards.append(card_obj(card, cid, {str(did): entry}))
    if len(cards) == 1:
        obj = cards[0]
    else:
        obj = {
            "GUID": guid(), "Name": "Deck", "Transform": transform(),
            "Nickname": deck_name, "Description": "", "GMNotes": "",
            "ColorDiffuse": {"r": 0.713235259, "g": 0.713235259, "b": 0.713235259},
            "Locked": False, "Grid": True, "Snap": True, "Autoraise": True, "Sticky": True,
            "Tooltip": True, "HideWhenFaceDown": True, "Hands": False, "SidewaysCard": False,
            "DeckIDs": ids, "CustomDeck": custom, "ContainedObjects": cards,
            "LuaScript": "", "LuaScriptState": "", "XmlUI": "",
        }
    return {"SaveName": "", "Date": "", "VersionNumber": "", "GameMode": "", "GameType": "",
            "GameComplexity": "", "Tags": [], "Gravity": 0.5, "PlayArea": 0.5, "Table": "",
            "Sky": "", "Note": "", "TabStates": {}, "LuaScript": "", "LuaScriptState": "",
            "XmlUI": "", "ObjectStates": [obj]}


# --- main ----------------------------------------------------------------------

def build(csv_path, out_dir, color_override, do_push):
    title = deck_title(csv_path)
    stem = slug(title)                    # image file names must be URL-safe
    cards = read_cards(csv_path)
    color = resolve_color(title, color_override)

    back = save_hashed(render_back(title, color), f"{stem}-back")
    sheets = [(save_hashed(im, f"{stem}-face{n}"), cols, rows, chunk)
              for n, (im, cols, rows, chunk) in enumerate(render_sheets(cards, title, color))]
    paths = [back] + [s[0] for s in sheets] + [COLORS_FILE]

    if do_push:
        push(paths, title)
        base = raw_base()
        url = lambda p: base + str(p.relative_to(REPO))
    else:
        url = lambda p: p.as_uri()

    save = build_object(title, [(url(p), c, r, ch) for p, c, r, ch in sheets], url(back))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{title}.json"
    out.write_text(json.dumps(save, indent=2))
    Image.open(back).resize((256, 366)).save(out_dir / f"{title}.png")   # Saved Objects thumbnail

    total = sum(c["count"] for c in cards)
    print(f"{csv_path.name}: {len(cards)} unique / {total} cards -> {out}")


def archive(csv_path):
    if csv_path.parent.name == "done":
        return
    done = csv_path.parent / "done"
    done.mkdir(exist_ok=True)
    csv_path.replace(done / csv_path.name)   # an older copy with the same name is overwritten
    print(f"  moved to {done / csv_path.name}")


def watch(folder, out_dir, color, do_push, keep):
    failed = {}   # path -> mtime of a version that failed; retried once the file changes
    print(f"Watching {folder} for new CSVs (Ctrl+C to stop)")
    while True:
        for p in sorted(folder.glob("*.csv")):
            mtime = p.stat().st_mtime
            if failed.get(p) == mtime or time.time() - mtime < 1:   # unchanged failure, or still being written
                continue
            try:
                build(p, out_dir, color, do_push)
            except (SystemExit, Exception) as e:
                failed[p] = mtime
                print(f"{p.name}: failed: {e}")
                continue
            failed.pop(p, None)
            if keep:
                failed[p] = mtime   # don't rebuild it every loop; an edit triggers a rebuild
            else:
                archive(p)
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="CSV file, or a folder with --watch")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Saved Objects folder (default: {DEFAULT_OUT})")
    ap.add_argument("--color", help=f"card border/back colour: {', '.join(PALETTE)} or #rrggbb "
                                     "(remembered per deck; asked for new decks when omitted)")
    ap.add_argument("--no-push", action="store_true", help="use local file:// URLs instead of GitHub")
    ap.add_argument("--watch", action="store_true", help="watch a folder and build CSVs dropped into it")
    ap.add_argument("--keep", action="store_true", help="leave built CSVs in place instead of moving them to done/")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # show progress promptly when logged to a file
    if a.watch:
        watch(a.path, a.out, a.color, not a.no_push, a.keep)
    else:
        build(a.path, a.out, a.color, not a.no_push)
        if not a.keep:
            archive(a.path)


if __name__ == "__main__":
    main()
