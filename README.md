# books — "can I borrow this for free?" checker

A small, dependency-free Python tool that takes a list of books and reports where
each one can be borrowed at no cost:

* **Kindle Unlimited** (via Amazon's Kindle-store search with the KU filter, matched strictly on title + author)
* **Libby / OverDrive** at **San Jose Public Library** (`sanjose`) and **Santa Clara County Library** (`santaclara`)
  * ebooks and audiobooks, reported separately
  * live hold queue: copies owned, copies available, number of holds, OverDrive's estimated wait in days
  * **Lucky Day** copies (non-holdable, borrow-right-now copies) are surfaced as "available now"
  * whether the Libby ebook can be sent to a Kindle

It is designed to be driven by Claude Code (see `CLAUDE.md`), but works fine by hand.

## Usage

```bash
python3 check_books.py "Project Hail Mary | Andy Weir" "Dungeon Crawler Carl | Matt Dinniman"
python3 check_books.py --file books.txt              # one "Title | Author" per line, '#' comments ok
echo "Piranesi | Susanna Clarke" | python3 check_books.py -
python3 check_books.py --json "The Martian | Andy Weir"   # machine-readable output
```

Separators accepted between title and author: `|` (preferred), ` -- `, ` — `, or ` by `.
Author is optional but strongly recommended; without it, any same-titled book matches.

Options:

| flag | effect |
|---|---|
| `--json` | JSON instead of markdown |
| `--no-ku` / `--no-libby` | skip a source |
| `--formats ebook` | only ebooks (default `ebook,audiobook`) |
| `--no-cache` | ignore the 6-hour Amazon response cache in `.cache/` |

Requirements: Python 3.8+, nothing else.

## Sample output

```
| Book | Kindle Unlimited | SJPL ebook | SJPL audiobook | SCCLD ebook | SCCLD audiobook |
|---|---|---|---|---|---|
| Project Hail Mary — Andy Weir | no | ~109d wait (364 holds/47 copies) | not owned | available now (Lucky Day) | not owned |
| Dungeon Crawler Carl — Matt Dinniman | YES | available now | not owned | available now | not owned |

### Project Hail Mary — Andy Weir
- Kindle Unlimited: no
- Libby / SJPL ebook: WAIT ~109 days (364 holds on 47 copies) · Kindle-compatible · https://sanjose.overdrive.com/media/5665700
- Libby / SCCLD ebook: AVAILABLE NOW via Lucky Day only (137 of 270 Lucky Day copies in; regular copies: 40 holds on 62) · Kindle-compatible · https://santaclara.overdrive.com/media/5665700
```

## How it works / caveats

* **Libby** uses OverDrive's public, unauthenticated catalog API
  (`thunder.api.overdrive.com/v2/libraries/<key>/media?query=…`), the same one the Libby
  web app calls. No library card is needed to read availability. Wait estimates are
  OverDrive's own (`estimatedWaitDays`).
* **Kindle Unlimited** has no API. The tool fetches the Amazon Kindle-store search page
  with the "Kindle Unlimited eligible" refinement and only reports a hit when a result
  carries the KU badge **and** its title and author match. Amazon occasionally serves a
  captcha/503; the tool retries and otherwise reports `UNKNOWN` rather than guessing.
  KU is region-specific (this checks amazon.com / US).
* Title matching is deliberately strict: it tolerates subtitles and series tags
  ("Foo: A Novel", "Foo (Series Book 1)") but not extensions ("The Martian" ≠ "The Martian Chronicles").
* `not owned` means the library has no matching record; `no copies` means a record exists
  but the license lapsed (holds still queue and libraries often re-buy).
* To add a library, append `(overdrive_key, short_label, long_name)` to `LIBRARIES` in
  `check_books.py`. Find keys with
  `https://thunder.api.overdrive.com/v2/libraries?query=<library name>`.
