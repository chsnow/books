# CLAUDE.md — how to use this repo

This repo holds one tool: `check_books.py`. It answers "can the user borrow this book for
free, and from where?" for the user's three free sources:

1. **Kindle Unlimited** (the user is a subscriber)
2. **Libby — San Jose Public Library** (`sanjose`)
3. **Libby — Santa Clara County Library** (`santaclara`)

## The workflow when the user asks for book recommendations

1. Pick your recommendations first (as many as makes sense; 5–10 is typical).
2. Run the checker on ALL of them in one call, always with the author:

   ```bash
   python3 check_books.py "Title One | Author One" "Title Two | Author Two" ...
   ```

   (Or write them to a file and use `--file`. Use `--json` if you want to post-process.)
3. Present the recommendations with the availability next to each one. For every book say:
   * whether it is on **Kindle Unlimited** (this is the user's zero-wait option),
   * for each library, ebook and audiobook: **available now**, **available via Lucky Day**
     (borrow right now, but usually a shorter loan and cannot be renewed), or the **wait**
     (OverDrive's estimated days plus "N holds on M copies"), or **not owned**,
   * include the OverDrive/Amazon links the tool prints so the user can tap through.
4. Rank/flag by friction: KU or "available now" > short wait > long wait > not available.
   If something great is unavailable everywhere, still recommend it, just say so.

## Interpreting the output

* `available now (Lucky Day)` — the regular copies are all out, but a non-holdable Lucky Day
  copy is in. Tell the user to grab it soon; those go fast.
* `no copies (N holds)` — the library once licensed it but the license lapsed. Not borrowable
  today; holds still queue.
* `Kindle Unlimited: UNKNOWN — Amazon blocked…` — Amazon rate-limited us. Re-run once with
  `--no-cache`; if it persists, say KU status could not be verified rather than guessing.
* `Kindle-compatible` on a Libby ebook means it can be delivered to the user's Kindle.
* The tool is strict about titles: give the real title, not a nickname, and always pass the
  author. If a result is surprisingly "not owned", retry with the exact catalog title
  (e.g. the first book of a series often carries the series name).

## Practical notes

* Zero dependencies; Python 3.8+. Nothing to install.
* Amazon calls are sequential with a ~1–2 s polite delay and cached for 6 h in `.cache/`
  (git-ignored). 10 books ≈ 20–30 s. Libby calls are parallel and fast.
* Libraries are configured in the `LIBRARIES` list at the top of `check_books.py`.
* Quick self-test that exercises every code path (KU hit, KU miss, Lucky Day, long wait, not owned):

  ```bash
  python3 check_books.py "Project Hail Mary | Andy Weir" "Dungeon Crawler Carl | Matt Dinniman" "Piranesi | Susanna Clarke"
  ```
