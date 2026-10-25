# tts-decks

CSV -> Tabletop Simulator deck. Images in `images/` are served to TTS via raw.githubusercontent.com.

    ./make_deck.py cards/<name>.csv
    ./make_deck.py --watch cards/

CSV columns: `Name`, `Effect`, `Starting count`. The file name becomes the deck name and card back text.
