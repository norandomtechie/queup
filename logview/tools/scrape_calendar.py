#!/usr/bin/env python3
"""Scrape Purdue's registrar academic calendars into ../semesters.json.

Purdue's term dates follow a fixed pattern that lvlib/analyze.py computes, so
the logview needs no calendar data.  This script checks that pattern against
the registrar's published calendars and writes ../semesters.json containing
only the terms that DIFFER (the pattern's exceptions, e.g. COVID-era terms),
which analyze.py then uses as overrides.  Run it on your own machine (it needs
internet; the logview itself never does) now and then, e.g. when a new year is
published, and copy semesters.json next to index.cgi:

    python3 tools/scrape_calendar.py

Sources (plain HTML tables of "Date | Event"):
  https://www.purdue.edu/registrar/calendars/academic/           current + projected years
  https://www.purdue.edu/registrar/calendars/academic-archive/   past academic years

For every Spring/Summer/Fall term it reads:
  start        "<Term> term classes begin"
  classes_end  "<Term> term classes end"            (Summer has none)
  end          "<Term> term ends" / "Summer term classes/term ends"  (after finals)
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser

URLS = ["https://www.purdue.edu/registrar/calendars/academic/",
        "https://www.purdue.edu/registrar/calendars/academic-archive/"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "semesters.json")
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
          "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def norm(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ").replace("&nbsp;", " ")).strip()


class Cal(HTMLParser):
    """Yields (context, date text, event text) for every table row."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ctx = None          # ("year", 2026) or ("span", 2024)
        self.rows = []
        self._tag = None
        self._buf = ""
        self._cells = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "button"):
            self._tag, self._buf = tag, ""
        elif tag == "tr":
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._buf = ""
            self._tag = tag

    def handle_data(self, data):
        if self._tag:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == self._tag and tag in ("h1", "h2", "h3", "h4", "button"):
            t = norm(self._buf)
            m = re.fullmatch(r"(\d{4})(?:-(\d{2}))?", t)
            if m:
                self.ctx = ("span" if m.group(2) else "year", int(m.group(1)))
            self._tag = None
        elif tag in ("td", "th") and self._cells is not None:
            self._cells.append(norm(self._buf))
            self._tag = None
        elif tag == "tr" and self._cells is not None:
            if len(self._cells) == 2 and self._cells[0].lower() != "date":
                self.rows.append((self.ctx, self._cells[0], self._cells[1]))
            self._cells = None


def parse_rows(rows):
    """-> list of (event text, date) with years resolved."""
    out, span_year, last_month = [], None, 0
    prev_ctx = None
    for ctx, dtxt, ev in rows:
        m = re.match(r"([A-Za-z]+)\.?\s*(\d{1,2})", dtxt)
        if not ctx or not m or m.group(1)[:3].lower() not in MONTHS:
            continue
        mon, day = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
        if ctx != prev_ctx:
            prev_ctx, span_year, last_month = ctx, ctx[1], 0
        if ctx[0] == "span" and mon < last_month:  # Dec -> Jan: next calendar year
            if last_month >= 11 and mon <= 2:
                span_year += 1
        last_month = mon
        year = ctx[1] if ctx[0] == "year" else span_year
        out.append((ev, date(year, mon, day)))
    return out


def extract(events):
    terms = {}
    for ev, d in events:
        e = ev.lower()
        m = re.match(r"(spring|summer|fall) term (classes begin|classes end|ends|classes/term ends)", e)
        if not m:
            continue
        name, what = m.group(1).capitalize(), m.group(2)
        if what == "classes begin":
            terms[(name, d.year)] = {"start": d.isoformat()}
            continue
        # attach to the most recent term of that name that started before d
        cands = [k for k in terms if k[0] == name and date.fromisoformat(terms[k]["start"]) <= d]
        if not cands:
            continue
        t = terms[max(cands, key=lambda k: k[1])]
        if what == "classes end":
            t["classes_end"] = d.isoformat()
        else:
            t["end"] = d.isoformat()
    return terms


def main():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from lvlib import analyze
    analyze.OVERRIDES = {}  # compare against the pure pattern, not earlier overrides

    allterms = {}
    for url in URLS:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (queup logview calendar scraper)"})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        p = Cal()
        p.feed(html)
        t = extract(parse_rows(p.rows))
        print("%s: %d terms" % (url, len(t)))
        for k, v in t.items():
            allterms.setdefault(k, v)

    exceptions, ok = {}, 0
    for (name, year), v in sorted(allterms.items(), key=lambda kv: kv[1].get("start", "")):
        if "start" not in v or "end" not in v:
            print("  warning: incomplete term %s %d: %s" % (name, year, v), file=sys.stderr)
            continue
        st, en = analyze.term_dates(name, year)
        if (st.isoformat(), en.isoformat()) == (v["start"], v["end"]):
            ok += 1
        else:
            exceptions["%s %d" % (name, year)] = {"start": v["start"], "end": v["end"]}
            print("  differs from pattern: %s %d registrar %s..%s, pattern %s..%s"
                  % (name, year, v["start"], v["end"], st, en))
    print("%d terms follow the pattern, %d exceptions" % (ok, len(exceptions)))
    with open(OUT, "w") as f:
        json.dump({"generated": datetime.now().isoformat(timespec="seconds"), "sources": URLS,
                   "note": "Only terms that differ from the pattern in lvlib/analyze.py are listed; "
                           "they override it. Re-generate with tools/scrape_calendar.py.",
                   "terms": exceptions}, f, indent=1)
        f.write("\n")
    print("wrote", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
