#!/usr/bin/env python3
"""
check_books.py -- Where can I borrow this book for free?

Checks a list of books against:
  * Kindle Unlimited (Amazon search with the KU filter, strict title/author match)
  * Libby / OverDrive at San Jose Public Library and Santa Clara County Library
    (ebook + audiobook, with live hold-queue / wait-time info)

Zero third-party dependencies (Python 3.8+, stdlib only).

Usage:
    python3 check_books.py "Project Hail Mary | Andy Weir" "Dungeon Crawler Carl | Matt Dinniman"
    python3 check_books.py --file books.txt          # one "Title | Author" per line
    echo "The Martian | Andy Weir" | python3 check_books.py -
    python3 check_books.py --json "Piranesi | Susanna Clarke"

The separator between title and author may be "|", " -- ", " — ", or " by ".
Author is optional but strongly recommended (it prevents false matches).
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import html as htmlmod
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

LIBRARIES = [
    # (overdrive key, short label, long name)
    ("sanjose", "SJPL", "San Jose Public Library"),
    ("santaclara", "SCCLD", "Santa Clara County Library"),
]

OVERDRIVE_API = "https://thunder.api.overdrive.com/v2/libraries/{key}/media"
AMAZON_SEARCH = "https://www.amazon.com/s"
# Amazon's "Kindle Unlimited eligible" refinement for the Kindle store.
KU_REFINEMENT = "p_n_feature_nineteen_browse-bin:9045887011"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_TTL_SECONDS = 6 * 3600  # availability changes; don't cache long

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def norm(s):
    """Normalize a title/author for fuzzy-but-strict comparison."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_title_noise(title):
    """Drop subtitles / series parentheticals: 'Foo: A Novel (Bar Book 2)' -> 'Foo'."""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)  # parentheticals
    t = re.split(r"\s*[:–—]\s+|\s+-\s+", t)[0]  # subtitle after ':' or dash
    return t.strip()


def title_matches(want, got):
    """want = user's title, got = catalog title. Strict enough to avoid false positives.

    Matches exact titles, or titles that differ only by a subtitle / series tag
    ("Dungeon Crawler Carl: Book 1", "Children of Time (Children of Time, Book 1)").
    Does NOT match extensions of the title ("The Martian" vs "The Martian Chronicles").
    """
    w_full = norm(want)
    w_short = norm(strip_title_noise(want))
    g_full = norm(got)
    g_short = norm(strip_title_noise(got))
    if not w_full or not g_full:
        return False
    return w_full in (g_full, g_short) or w_short in (g_full, g_short)


def author_matches(want, got):
    """Match on surname (and, if present, first-name initial) to survive 'J.K.' vs 'J. K.'."""
    if not want:
        return True  # no author given -> accept
    if not got:
        return False
    w = norm(want).split()
    g = norm(got)
    if not w:
        return True
    surname = w[-1]
    if len(surname) <= 2 and len(w) >= 2:  # e.g. "Le Guin" -> use last two tokens
        surname = " ".join(w[-2:])
    if surname not in g.split() and surname not in g:
        return False
    if len(w) >= 2:
        first_initial = w[0][0]
        # every listed author's first letter; accept if any author's first name starts the same way
        return any(tok.startswith(first_initial) for tok in g.split())
    return True


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return resp.status, raw.decode("utf-8", errors="replace")


def cache_path(key):
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")


def cache_get(key):
    p = cache_path(key)
    try:
        if time.time() - os.path.getmtime(p) < CACHE_TTL_SECONDS:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    return None


def cache_put(key, value):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path(key), "w", encoding="utf-8") as f:
            json.dump(value, f)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Libby / OverDrive
# --------------------------------------------------------------------------- #


def overdrive_search(key, query, per_page=60, fmt=None):
    params = {"query": query, "perPage": per_page, "page": 1}
    if fmt:
        params["format"] = fmt
    url = OVERDRIVE_API.format(key=key) + "?" + urllib.parse.urlencode(params)
    status, body = http_get(url, headers={"Accept": "application/json",
                                          "User-Agent": BROWSER_HEADERS["User-Agent"]})
    return json.loads(body).get("items", [])


def summarize_overdrive_item(key, it):
    fmts = [f.get("id", "") for f in it.get("formats", [])]
    langs = [l.get("id") for l in it.get("languages", [])]
    asin = None
    for f in it.get("formats", []):
        for ident in f.get("identifiers", []):
            if ident.get("type") == "ASIN":
                asin = ident.get("value")
    owned = it.get("ownedCopies") or 0
    avail = it.get("availableCopies") or 0
    holds = it.get("holdsCount") or 0
    lucky = it.get("luckyDayAvailableCopies") or 0
    always = str(it.get("availabilityType", "")).lower().startswith("always")
    lucky_owned = it.get("luckyDayOwnedCopies") or 0
    # Lucky Day copies are non-holdable, first-come copies: if one is in, you can borrow it right now.
    available_now = bool(it.get("isAvailable")) or avail > 0 or always or lucky > 0
    return {
        "library": key,
        "id": it.get("id"),
        "title": it.get("title"),
        "subtitle": it.get("subtitle"),
        "author": it.get("firstCreatorName"),
        "type": (it.get("type") or {}).get("id"),  # ebook | audiobook | magazine
        "languages": langs,
        "kindle_format": "ebook-kindle" in fmts,
        "asin": asin,
        "available_now": available_now,
        "always_available": always,
        "available_copies": avail,
        "owned_copies": owned,
        "holds": holds,
        "lucky_day_copies": lucky,
        "lucky_day_owned": lucky_owned,
        "holds_ratio": it.get("holdsRatio"),
        "estimated_wait_days": None if available_now else it.get("estimatedWaitDays"),
        "holds_per_copy": (round(holds / owned, 1) if owned else None),
        "url": "https://{key}.overdrive.com/media/{id}".format(key=key, id=it.get("id")),
    }


def check_libby(key, title, author, want_types=("ebook", "audiobook")):
    """Return {'ebook': [...], 'audiobook': [...]} of matching items (best first), or {'error': ...}."""
    query = " ".join(x for x in (title, author) if x)
    FORMAT_FILTERS = {
        "ebook": "ebook-kindle,ebook-overdrive,ebook-epub-adobe,ebook-epub-open,ebook-pdf-adobe,ebook-kobo",
        "audiobook": "audiobook-overdrive,audiobook-mp3",
    }
    out = {t: [] for t in want_types}
    seen = set()

    def absorb(items):
        for it in items:
            typ = (it.get("type") or {}).get("id")
            if typ not in out or it.get("id") in seen:
                continue
            if not title_matches(title, it.get("title") or ""):
                continue
            if not author_matches(author, it.get("firstCreatorName") or ""):
                continue
            seen.add(it.get("id"))
            out[typ].append(summarize_overdrive_item(key, it))

    try:
        absorb(overdrive_search(key, query))
        # Popular series can push a format past the first page; re-query per missing format.
        for t in want_types:
            if not out[t] and t in FORMAT_FILTERS:
                absorb(overdrive_search(key, query, fmt=FORMAT_FILTERS[t]))
                if not out[t] and author:
                    absorb(overdrive_search(key, title, fmt=FORMAT_FILTERS[t]))
    except Exception as e:  # noqa: BLE001
        return {"error": "OverDrive request failed: %s" % e}

    # English first, then available-now first, then shortest wait
    def rank(s):
        return (
            0 if ("en" in s["languages"] or not s["languages"]) else 1,
            0 if s["available_now"] else 1,
            1 if s["owned_copies"] == 0 else 0,
            s["estimated_wait_days"] if s["estimated_wait_days"] is not None else 0,
        )

    for t in out:
        english = [x for x in out[t] if "en" in x["languages"] or not x["languages"]]
        if english:
            out[t] = english
        out[t].sort(key=rank)
    return out


# --------------------------------------------------------------------------- #
# Kindle Unlimited (Amazon search with KU refinement)
# --------------------------------------------------------------------------- #

_RESULT_SPLIT = re.compile(r'<div[^>]*data-component-type="s-search-result"[^>]*>')
_ASIN_RE = re.compile(r'data-asin="([A-Z0-9]{10})"')
_H2_RE = re.compile(r'<h2[^>]*aria-label="([^"]*)"', re.S)
_H2_TEXT_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_AUTHOR_RE = re.compile(r">by\s*</span>(.*?)(?:<span class=\"a-size-base a-color-secondary\">\s*\|)", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment):
    return htmlmod.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", fragment))).strip()


def parse_amazon_results(page_html):
    results = []
    parts = _RESULT_SPLIT.split(page_html)
    heads = _RESULT_SPLIT.findall(page_html)
    for head, body in zip(heads, parts[1:]):
        m = _ASIN_RE.search(head)
        if not m:
            continue
        asin = m.group(1)
        title = None
        mh = _H2_RE.search(body)
        if mh:
            title = htmlmod.unescape(mh.group(1))
        else:
            mh = _H2_TEXT_RE.search(body)
            if mh:
                title = _text(mh.group(1))
        author = None
        ma = _AUTHOR_RE.search(body)
        if ma:
            author = _text(ma.group(1))
        else:
            # fallback: text right after "by"
            i = body.find(">by </span>")
            if i != -1:
                author = _text(body[i + 11 : i + 600]).split(" | ")[0]
        ku_badge = (
            "apex-kindle-program-badge" in body
            or 'alt="Kindle Unlimited"' in body
            or "Kindle Unlimited membership" in body
        )
        sponsored = "Sponsored" in body[:4000] and "s-sponsored-label" in body
        results.append(
            {"asin": asin, "title": title, "author": author, "ku": ku_badge, "sponsored": sponsored}
        )
    return results


def amazon_ku_search(query):
    """Fetch the KU-filtered Kindle store search page. Returns (status, html)."""
    params = {"k": query, "i": "digital-text", "rh": KU_REFINEMENT}
    url = AMAZON_SEARCH + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(3):
        try:
            status, body = http_get(url, headers=BROWSER_HEADERS)
            if status == 200 and "s-search-result" in body:
                return status, body
            last_err = "HTTP %s (%s)" % (status, "captcha" if "captcha" in body.lower() else "no results block")
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(1.5 * (attempt + 1) + random.random())
    raise RuntimeError(last_err or "unknown error")


def check_ku(title, author):
    query = " ".join(x for x in (title, author) if x)
    ck = "amazon:" + query
    cached = cache_get(ck)
    if cached is not None:
        results = cached
    else:
        try:
            _, page = amazon_ku_search(query)
        except Exception as e:  # noqa: BLE001
            return {"status": "unknown", "error": "Amazon blocked/failed: %s" % e}
        results = parse_amazon_results(page)
        cache_put(ck, results)
        time.sleep(1.0 + random.random())  # be polite between Amazon calls

    matches = [
        r for r in results
        if r["ku"] and r["title"] and title_matches(title, r["title"]) and author_matches(author, r["author"])
    ]
    if matches:
        best = matches[0]
        return {
            "status": "in_ku",
            "asin": best["asin"],
            "title": best["title"],
            "author": best["author"],
            "url": "https://www.amazon.com/dp/%s" % best["asin"],
        }
    # Title matched but author didn't -> tell the caller (could be a different edition/author spelling)
    near = [r for r in results if r["ku"] and r["title"] and title_matches(title, r["title"])]
    if near and author:
        return {
            "status": "not_in_ku",
            "note": "KU has a same-titled book by %s (not matched to %s)" % (near[0]["author"], author),
        }
    return {"status": "not_in_ku", "results_seen": len(results)}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def parse_book_spec(spec):
    """'Title | Author' (preferred), 'Title -- Author', 'Title — Author', or 'Title by Author'."""
    spec = spec.strip()
    if not spec or spec.startswith("#"):
        return None
    for sep in ("|", " -- ", " — ", " – "):
        if sep in spec:
            title, author = spec.split(sep, 1)
            return {"title": title.strip(), "author": author.strip()}
    m = re.search(r"^(.*)\s+by\s+([^|]+)$", spec, re.I)  # last ' by ' wins: "Stand by Me by Stephen King"
    if m:
        return {"title": m.group(1).strip(), "author": m.group(2).strip()}
    return {"title": spec, "author": ""}


def check_book(book, do_ku=True, do_libby=True, types=("ebook", "audiobook")):
    res = {"title": book["title"], "author": book["author"], "libby": {}, "ku": None}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {}
        if do_libby:
            for key, _, _ in LIBRARIES:
                futs[ex.submit(check_libby, key, book["title"], book["author"], types)] = ("libby", key)
        if do_ku:
            futs[ex.submit(check_ku, book["title"], book["author"])] = ("ku", None)
        for fut in concurrent.futures.as_completed(futs):
            kind, key = futs[fut]
            try:
                val = fut.result()
            except Exception as e:  # noqa: BLE001
                val = {"error": str(e)}
            if kind == "libby":
                res["libby"][key] = val
            else:
                res["ku"] = val
    return res


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def fmt_libby_item(s):
    bits = []
    if s["available_now"]:
        if s["always_available"]:
            bits.append("AVAILABLE NOW (always available)")
        elif s["available_copies"] > 0:
            bits.append("AVAILABLE NOW (%d of %d copies in)" % (s["available_copies"], s["owned_copies"]))
        else:
            bits.append("AVAILABLE NOW via Lucky Day only (%d of %d Lucky Day copies in; regular copies: %d holds on %d)"
                        % (s["lucky_day_copies"], s["lucky_day_owned"], s["holds"], s["owned_copies"]))
    else:
        wait = s["estimated_wait_days"]
        if s["owned_copies"] == 0:
            bits.append("NO COPIES right now (%d holds; license likely expired, may be repurchased)" % s["holds"])
        elif wait is None:
            bits.append("WAIT (%d holds on %d copies)" % (s["holds"], s["owned_copies"]))
        else:
            bits.append("WAIT ~%d days (%d holds on %d copies)" % (wait, s["holds"], s["owned_copies"]))
    if s["lucky_day_copies"] and s["available_copies"] > 0:
        bits.append("+%d Lucky Day copies in" % s["lucky_day_copies"])
    if s["type"] == "ebook" and s["kindle_format"]:
        bits.append("Kindle-compatible")
    if s["languages"] and "en" not in s["languages"]:
        bits.append("language: %s" % ",".join(s["languages"]))
    return " · ".join(bits)


def render_markdown(results, types):
    lines = []
    lines.append("## Book availability")
    lines.append("")
    # summary table
    hdr = ["Book", "Kindle Unlimited"]
    for _, label, _ in LIBRARIES:
        for t in types:
            hdr.append("%s %s" % (label, t))
    lines.append("| " + " | ".join(hdr) + " |")
    lines.append("|" + "---|" * len(hdr))
    for r in results:
        row = ["%s — %s" % (r["title"], r["author"]) if r["author"] else r["title"]]
        ku = r["ku"]
        if ku is None:
            row.append("—")
        elif ku["status"] == "in_ku":
            row.append("YES")
        elif ku["status"] == "not_in_ku":
            row.append("no")
        else:
            row.append("unknown")
        for key, _, _ in LIBRARIES:
            lib = r["libby"].get(key) or {}
            for t in types:
                if "error" in lib:
                    row.append("error")
                    continue
                items = lib.get(t) or []
                if not items:
                    row.append("not owned")
                else:
                    s = items[0]
                    if s["available_now"] and s["available_copies"] == 0 and s["lucky_day_copies"] > 0:
                        row.append("available now (Lucky Day)")
                    elif s["available_now"]:
                        row.append("available now")
                    elif s["owned_copies"] == 0:
                        row.append("no copies (%d holds)" % s["holds"])
                    elif s["estimated_wait_days"] is not None:
                        row.append("~%dd wait (%d holds/%d copies)" % (s["estimated_wait_days"], s["holds"], s["owned_copies"]))
                    else:
                        row.append("wait (%d holds/%d copies)" % (s["holds"], s["owned_copies"]))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # details
    for r in results:
        head = "### %s" % r["title"]
        if r["author"]:
            head += " — " + r["author"]
        lines.append(head)
        ku = r["ku"]
        if ku is not None:
            if ku["status"] == "in_ku":
                lines.append("- **Kindle Unlimited: YES** — %s by %s · %s" % (ku["title"], ku["author"], ku["url"]))
            elif ku["status"] == "not_in_ku":
                extra = (" (%s)" % ku["note"]) if ku.get("note") else ""
                lines.append("- Kindle Unlimited: no%s" % extra)
            else:
                lines.append("- Kindle Unlimited: UNKNOWN — %s" % ku.get("error", ""))
        for key, label, longname in LIBRARIES:
            lib = r["libby"].get(key)
            if lib is None:
                continue
            if "error" in lib:
                lines.append("- Libby / %s: ERROR — %s" % (longname, lib["error"]))
                continue
            any_item = False
            for t in types:
                for s in (lib.get(t) or [])[:2]:  # show at most 2 editions per format
                    any_item = True
                    lines.append("- Libby / %s %s: %s · %s" % (label, t, fmt_libby_item(s), s["url"]))
            if not any_item:
                lines.append("- Libby / %s: not in catalog" % label)
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("books", nargs="*", help='"Title | Author" specs (or "-" to read from stdin)')
    ap.add_argument("--file", "-f", help="file with one 'Title | Author' per line")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--no-ku", action="store_true", help="skip the Kindle Unlimited check")
    ap.add_argument("--no-libby", action="store_true", help="skip the Libby checks")
    ap.add_argument("--formats", default="ebook,audiobook", help="comma list of ebook,audiobook (default both)")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached Amazon responses")
    args = ap.parse_args(argv)

    specs = []
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            specs.extend(f.read().splitlines())
    if "-" in args.books or (not args.books and not args.file):
        specs.extend(sys.stdin.read().splitlines())
    specs.extend(b for b in args.books if b != "-")
    books = [b for b in (parse_book_spec(s) for s in specs) if b]
    if not books:
        ap.error("no books given")

    if args.no_cache:
        global CACHE_TTL_SECONDS
        CACHE_TTL_SECONDS = 0

    types = tuple(t.strip() for t in args.formats.split(",") if t.strip())
    results = []
    for i, b in enumerate(books, 1):
        log("[%d/%d] %s%s" % (i, len(books), b["title"], (" — " + b["author"]) if b["author"] else ""))
        results.append(check_book(b, do_ku=not args.no_ku, do_libby=not args.no_libby, types=types))

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(results, types))


if __name__ == "__main__":
    main()
