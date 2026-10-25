#!/usr/bin/env python3
"""Build a Tabletop Simulator deck from a CSV.

CSV columns: Name, Effect, Starting count  (tab, comma or semicolon separated)

  make_deck.py cards/resource.csv            # render, push to GitHub, write Saved Object
  make_deck.py cards/resource.csv --no-push  # local file:// URLs, for testing alone
  make_deck.py --watch cards/                # rebuild whenever a CSV in the folder changes

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


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def darker(rgb, f=0.55):
    return tuple(int(c * f) for c in rgb)


# --- CSV ---------------------------------------------------------------------

def read_cards(path):
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters="\t,;")
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    cards = []
    for i, row in enumerate(rows, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = row.get("name", "")
        if not name:
            continue
        count = row.get("starting count", "1") or "1"
        try:
            count = int(count)
        except ValueError:
            sys.exit(f"{path.name} line {i}: 'Starting count' is not a number: {count!r}")
        cards.append({"name": name, "effect": row.get("effect", ""), "count": count})
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

    # Effect: largest size whose wrapped text fits the body area
    body_top, body_bot = 165, CARD_H - m - 30
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
        "Nickname": card["name"], "Description": card["effect"], "GMNotes": "",
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

def build(csv_path, out_dir, color, do_push):
    stem = csv_path.stem
    title = stem.replace("_", " ").replace("-", " ").strip().capitalize()
    cards = read_cards(csv_path)

    back = save_hashed(render_back(title, color), f"{stem}-back")
    sheets = [(save_hashed(im, f"{stem}-face{n}"), cols, rows, chunk)
              for n, (im, cols, rows, chunk) in enumerate(render_sheets(cards, title, color))]
    paths = [back] + [s[0] for s in sheets]

    if do_push:
        push(paths, title)
        base = raw_base()
        url = lambda p: base + str(p.relative_to(REPO))
    else:
        url = lambda p: p.as_uri()

    save = build_object(title, [(url(p), c, r, ch) for p, c, r, ch in sheets], url(back))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem}.json"
    out.write_text(json.dumps(save, indent=2))
    Image.open(back).resize((256, 366)).save(out_dir / f"{stem}.png")   # Saved Objects thumbnail

    total = sum(c["count"] for c in cards)
    print(f"{csv_path.name}: {len(cards)} unique / {total} cards -> {out}")


def watch(folder, out_dir, color, do_push):
    seen = {}
    print(f"Watching {folder} for CSV changes (Ctrl+C to stop)")
    while True:
        for p in folder.glob("*.csv"):
            mtime = p.stat().st_mtime
            if seen.get(p) != mtime:
                seen[p] = mtime
                try:
                    build(p, out_dir, color, do_push)
                except (SystemExit, Exception) as e:
                    print(f"{p.name}: failed: {e}")
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="CSV file, or a folder with --watch")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Saved Objects folder (default: {DEFAULT_OUT})")
    ap.add_argument("--color", default="#6e8b3d", help="card border/back colour (default: %(default)s)")
    ap.add_argument("--no-push", action="store_true", help="use local file:// URLs instead of GitHub")
    ap.add_argument("--watch", action="store_true", help="watch a folder and rebuild changed CSVs")
    a = ap.parse_args()
    color = hex_rgb(a.color)
    if a.watch:
        watch(a.path, a.out, color, not a.no_push)
    else:
        build(a.path, a.out, color, not a.no_push)


if __name__ == "__main__":
    main()
