"""
guest_probe.py  —  Throwaway diagnostic. Does NOT touch your app.

Dumps every card the guest search returns for a query, flags reposts, shows the
raw time text, and reports whether our parser would keep or skip each one.
Set KEYWORDS/LOCATION below to match what you typed in the app.

Run:  python guest_probe.py
"""

import re
import requests
from bs4 import BeautifulSoup

# ── set these to match your app search ──
KEYWORDS = "Product Owner"
LOCATION = "Lisbon"
PAGES    = 6          # how many pages of 10 to scan
# Optional: paste a specific job id you expect to find, to check if it's in results
EXPECT_ID = ""        # e.g. "4429544090"
# ─────────────────────────────────────────

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def main():
    seen = set()
    rows = []
    for p in range(PAGES):
        url = (f"{SEARCH}?keywords={requests.utils.quote(KEYWORDS)}"
               f"&location={requests.utils.quote(LOCATION)}&start={p*10}")
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200 or not r.text.strip():
            print(f"page {p}: HTTP {r.status_code} (stopping)")
            break
        soup  = BeautifulSoup(r.text, "html.parser")
        cards = soup.find_all("div", class_="base-card")
        if not cards:
            break
        for c in cards:
            urn  = c.get("data-entity-urn", "")
            jid  = urn.split("jobPosting:")[-1].strip() if "jobPosting:" in urn else None
            if jid in seen:
                continue
            seen.add(jid)
            title   = (c.find("h3", class_="base-search-card__title") or {})
            title   = title.get_text(strip=True) if hasattr(title, "get_text") else "??"
            company = (c.find("h4", class_="base-search-card__subtitle") or {})
            company = company.get_text(strip=True) if hasattr(company, "get_text") else "??"
            time_el = c.find("time")
            ttext   = time_el.get_text(strip=True) if time_el else ""
            kept    = bool(jid and jid.isdigit())
            rows.append((jid, kept, "REPOST" if "repost" in ttext.lower() else "", ttext, company, title))

    print(f"\n{len(rows)} cards for '{KEYWORDS}' in '{LOCATION}' ({PAGES} pages)\n")
    for jid, kept, rep, ttext, company, title in rows:
        flag = "KEEP " if kept else "SKIP*"
        print(f"  {flag} {rep:6} {ttext:18} | {company[:22]:22} | {title[:40]}")
    skipped = [r for r in rows if not r[1]]
    print(f"\n  parsed/keep: {len(rows)-len(skipped)}   skipped (no valid id): {len(skipped)}")
    if EXPECT_ID:
        print(f"  expected id {EXPECT_ID} present: {EXPECT_ID in seen}")


if __name__ == "__main__":
    main()
