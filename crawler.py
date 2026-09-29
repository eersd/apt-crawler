#!/opt/homebrew/bin/python3.11
"""
Berlin state-owned apartment crawler.
Sources: inberlinwohnen.de (aggregator), WBM, Gewobag, Berlinovo, Degewo.
"""

import json
import os
import re
import smtplib
import sqlite3
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.table import Table

load_dotenv()
console = Console()

DB_PATH = Path("seen_listings.db")
REQUEST_DELAY = 1.5


@dataclass
class Listing:
    id: str           # source-prefixed, e.g. "wbm:some-slug"
    source: str       # "ibw", "wbm", "gewobag", "berlinovo"
    title: str
    address: str
    district: str
    zip_code: str
    rooms: float
    sqm: float
    cold_rent: float
    total_rent: float
    available_from: str
    wbs_required: bool
    detail_url: str

    @property
    def dedup_key(self) -> str:
        """Normalized key for cross-source duplicate detection: street number + rounded rent."""
        street = self.address.lower()
        # Normalize German umlauts and ß so "strasse" == "straße", "Muhlendamm" == "Mühlendamm"
        street = street.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        # Normalize street type abbreviations: "str." -> "strasse", "str " -> "strasse "
        street = re.sub(r"\bstr\.\s*", "strasse ", street)
        street = re.sub(r"\bstr\b", "strasse", street)
        # Remove remaining punctuation and collapse spaces
        street = re.sub(r"[^\w\s]", "", street)
        street = re.sub(r"\s+", " ", street).strip()
        rent = round(self.total_rent / 10) * 10  # round to nearest 10€
        return f"{street}|{rent}"

    def format_email_html(self) -> str:
        wbs = "WBS erforderlich" if self.wbs_required else "kein WBS"
        source_label = {"ibw": "inberlinwohnen.de", "wbm": "WBM", "gewobag": "Gewobag", "berlinovo": "Berlinovo", "degewo": "Degewo"}.get(self.source, self.source)
        return f"""
<html><body style="font-family:sans-serif;max-width:520px;margin:auto;color:#222">
  <h2 style="color:#1a5276">🏠 Neue Wohnung gefunden</h2>
  <p style="color:#888;font-size:13px;margin-top:-10px">Quelle: {source_label}</p>
  <table style="width:100%;border-collapse:collapse">
    <tr><td style="padding:6px 0;font-weight:bold;width:40%">Adresse</td>
        <td>{self.address}, {self.zip_code} {self.district}</td></tr>
    <tr style="background:#f4f6f7"><td style="padding:6px 0;font-weight:bold">Zimmer</td>
        <td>{self.rooms:.1f}</td></tr>
    <tr><td style="padding:6px 0;font-weight:bold">Wohnfläche</td>
        <td>{self.sqm:.1f} m²</td></tr>
    <tr style="background:#f4f6f7"><td style="padding:6px 0;font-weight:bold">Kaltmiete</td>
        <td>{self.cold_rent:.2f} €</td></tr>
    <tr><td style="padding:6px 0;font-weight:bold">Gesamtmiete</td>
        <td>{self.total_rent:.2f} €</td></tr>
    <tr style="background:#f4f6f7"><td style="padding:6px 0;font-weight:bold">Frei ab</td>
        <td>{self.available_from}</td></tr>
    <tr><td style="padding:6px 0;font-weight:bold">WBS</td>
        <td>{wbs}</td></tr>
  </table>
  <br>
  <a href="{self.detail_url}"
     style="background:#1a5276;color:white;padding:12px 24px;text-decoration:none;
            border-radius:4px;display:inline-block;font-size:16px">
    Zur Wohnung →
  </a>
  <p style="color:#888;font-size:12px;margin-top:24px">Berlin Apartment Crawler</p>
</body></html>"""

    def format_console(self) -> str:
        wbs = "WBS" if self.wbs_required else "   "
        return (
            f"[{self.source.upper():8s}] {self.rooms:.1f}Z | {self.sqm:.0f}m² | "
            f"{self.cold_rent:.0f}€ kalt | {wbs} | "
            f"{self.address}, {self.district}"
        )


def parse_float_de(s) -> float:
    if s is None:
        return 0.0
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        pass
    s = re.sub(r"[^\d,]", "", s).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Source: inberlinwohnen.de (Livewire)
# ---------------------------------------------------------------------------

IBW_URL = "https://www.inberlinwohnen.de/wohnungsfinder/"


def ibw_fetch_all(client: httpx.Client) -> List[Listing]:
    resp = client.get(IBW_URL)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Extract master ID list
    all_ids = []
    for el in soup.select("[wire\\:snapshot]"):
        try:
            snap = json.loads(el["wire:snapshot"])
            if "itemIds" in snap.get("data", {}):
                raw = snap["data"]["itemIds"]
                all_ids = [int(i) for i in (raw[0] if isinstance(raw, list) and raw else [])]
                break
        except (json.JSONDecodeError, KeyError):
            pass

    # Parse pre-rendered cards (first 10)
    rendered: Dict[int, Listing] = {}
    for card in soup.select("[id^='apartment-']"):
        try:
            snap = json.loads(card["wire:snapshot"])
            listing = _ibw_listing_from_snap(snap["data"])
            if listing:
                rendered[int(listing.id.split(":")[-1])] = listing
        except (KeyError, json.JSONDecodeError):
            pass

    # Return rendered listings; caller uses all_ids for change detection
    return all_ids, rendered


def _ibw_listing_from_snap(data: dict) -> Optional[Listing]:
    try:
        item = data["item"][0] if isinstance(data["item"], list) else data["item"]
        addr_raw = item.get("address", [])
        addr = addr_raw[0] if isinstance(addr_raw, list) and addr_raw else {}
        street = f"{addr.get('street', '')} {addr.get('number', '')}".strip()
        company_raw = item.get("company", [])
        company = company_raw[0] if isinstance(company_raw, list) and company_raw else {}

        details_str = json.dumps(item.get("details", []), ensure_ascii=False)
        wbs_match = re.search(r'"label"\s*:\s*"WBS"[^}]*"value"\s*:\s*"([^"]*)"', details_str)
        wbs_required = True
        if wbs_match:
            wbs_required = "nicht erforderlich" not in wbs_match.group(1).lower()

        return Listing(
            id=f"ibw:{item['id']}",
            source="ibw",
            title=item.get("title", ""),
            address=street,
            district=addr.get("district", ""),
            zip_code=addr.get("zipCode", ""),
            rooms=parse_float_de(item.get("rooms", "0")),
            sqm=parse_float_de(item.get("area", "0")),
            cold_rent=parse_float_de(item.get("rentNet", "0")),
            total_rent=parse_float_de(item.get("rentGross", "0")),
            available_from=item.get("occupationDate", ""),
            wbs_required=wbs_required,
            detail_url=item.get("deeplink", ""),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Source: WBM (Playwright — site has a JS bot challenge)
# ---------------------------------------------------------------------------

WBM_URL = "https://www.wbm.de/wohnungen-berlin/angebote/"


def _wbm_get_page_html(page) -> str:
    """Navigate to WBM listings, wait for cards to render, return HTML."""
    page.goto(WBM_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector("article.immo-element", timeout=15000)
    return page.content()


def wbm_fetch_all(page) -> Iterator[Listing]:
    html = _wbm_get_page_html(page)
    soup = BeautifulSoup(html, "lxml")
    data_cards = soup.select("article.immo-element:not(.teaserBox)")
    for card in data_cards:
        listing = _wbm_listing_from_card(card)
        if listing:
            yield listing


def _wbm_listing_from_card(card: BeautifulSoup) -> Optional[Listing]:
    try:
        link = card.select_one("a[href*='/wohnungen-berlin/angebote/details/']")
        if not link:
            return None
        slug = link["href"].rstrip("/").split("/")[-1]
        detail_url = f"https://www.wbm.de{link['href']}"

        title = card.select_one(".imageTitle, h2, h3")
        title_text = title.get_text(strip=True) if title else ""

        address_el = card.select_one(".address")
        address_text = address_el.get_text(strip=True) if address_el else ""
        zip_match = re.search(r"(\d{5})\s+Berlin", address_text)
        zip_code = zip_match.group(1) if zip_match else ""
        street = address_text.split(",")[0].strip() if address_text else ""

        district_match = re.search(r"\bin\s+([A-ZÄÖÜ][a-zäöüA-ZÄÖÜ\-]+(?:\s[A-ZÄÖÜ][a-zäöüA-ZÄÖÜ\-]+)*)\s*$", title_text)
        district = district_match.group(1) if district_match else ""

        rent_el = card.select_one(".main-property-rent")
        rent_text = rent_el.get_text(strip=True) if rent_el else ""
        rooms_el = card.select_one(".main-property-rooms")
        rooms_text = rooms_el.get_text(strip=True) if rooms_el else ""
        area_el = card.select_one(".main-property-size, .main-property-area")
        area_text = area_el.get_text(strip=True) if area_el else ""

        card_text = card.get_text(" ", strip=True).lower()
        wbs_required = "wbs" in card_text and "ohne wbs" not in card_text and "kein wbs" not in card_text and "nicht erforderlich" not in card_text

        return Listing(
            id=f"wbm:{slug}",
            source="wbm",
            title=title_text,
            address=street,
            district=district,
            zip_code=zip_code,
            rooms=parse_float_de(rooms_text),
            sqm=parse_float_de(area_text),
            cold_rent=0.0,
            total_rent=parse_float_de(rent_text),
            available_from="",
            wbs_required=wbs_required,
            detail_url=detail_url,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Source: Gewobag (WordPress REST API)
# ---------------------------------------------------------------------------

GEWOBAG_API = "https://www.gewobag.de/wp-json/wp/v2/immobilien"
# Bezirke term IDs for Friedrichshain-Kreuzberg and Mitte (all sub-districts)
GEWOBAG_TARGET_BEZIRKE = {
    1163, 289,   # Friedrichshain, Friedrichshain-Kreuzberg
    494,         # Kreuzberg
    634, 635, 636,  # Mitte sub-districts
    1166, 1170,  # Gesundbrunnen, Hansaviertel (Mitte)
    # Main Mitte ID — fetched dynamically
}


def _gewobag_get_target_bezirke_ids(client: httpx.Client) -> set:
    """Fetch bezirke term IDs whose slug contains 'mitte' or 'friedrichshain' or 'kreuzberg'."""
    try:
        r = client.get(
            "https://www.gewobag.de/wp-json/wp/v2/bezirke",
            params={"per_page": 100, "_fields": "id,slug"},
        )
        return {t["id"] for t in r.json()
                if any(k in t["slug"] for k in ("mitte", "friedrichshain", "kreuzberg"))}
    except Exception:
        return GEWOBAG_TARGET_BEZIRKE


def gewobag_fetch_all(client: httpx.Client) -> Iterator[dict]:
    """Yield raw API items (no detail fetch). Caller decides when to fetch details."""
    target_ids = _gewobag_get_target_bezirke_ids(client)
    page = 1
    while True:
        resp = client.get(GEWOBAG_API, params={
            "per_page": 100,
            "page": page,
            "objekttyp": 1220,  # wohnung only, skip Stellplätze
            "_fields": "id,slug,link,title,bezirke,objekttyp",
        })
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        for item in items:
            bezirke = set(item.get("bezirke", []))
            if target_ids and not bezirke.intersection(target_ids):
                continue
            yield item
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)


def _gewobag_fetch_detail(client: httpx.Client, api_item: dict) -> Optional[Listing]:
    """Fetch the listing detail page and parse structured fields."""
    try:
        detail_url = api_item.get("link", "")
        if not detail_url:
            return None
        resp = client.get(detail_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        text = soup.get_text(separator=" | ", strip=True)

        def between(label: str, end_labels: List[str] = []) -> str:
            pattern = re.escape(label) + r"\s*\|?\s*([\d.,\w\s/äöüÄÖÜß-]+?)(?:\s*\||\s*€|$)"
            m = re.search(pattern, text, re.I)
            return m.group(1).strip() if m else ""

        # Address: "Anschrift | Straße Nr, PLZ Berlin"
        addr_match = re.search(r"Anschrift\s*\|?\s*([^\|]+?,\s*\d{5}\s*Berlin)", text, re.I)
        full_addr = addr_match.group(1).strip() if addr_match else ""
        zip_match = re.search(r"\b(1\d{4})\b", full_addr)
        zip_code = zip_match.group(1) if zip_match else ""
        street = full_addr.split(",")[0].strip() if full_addr else ""

        district_match = re.search(r"Bezirk/Ortsteil\s*\|?\s*([^\|]+?)(?:\s*\|)", text, re.I)
        district_raw = district_match.group(1).strip() if district_match else ""
        district = district_raw.split("/")[0].strip() if district_raw else ""

        rooms_match = re.search(r"Anzahl Zimmer\s*\|?\s*([\d,]+)", text, re.I)
        rooms = parse_float_de(rooms_match.group(1)) if rooms_match else 0.0

        area_match = re.search(r"Fläche in m²\s*\|?\s*([\d,.]+)", text, re.I)
        sqm = parse_float_de(area_match.group(1)) if area_match else 0.0

        rent_match = re.search(r"Grundmiete\s*\|?\s*([\d.,]+)\s*Euro", text, re.I)
        cold_rent = parse_float_de(rent_match.group(1)) if rent_match else 0.0

        total_match = re.search(r"Gesamtmiete\s*\|?\s*([\d.,]+)\s*Euro", text, re.I)
        total_rent = parse_float_de(total_match.group(1)) if total_match else 0.0

        avail_match = re.search(r"(?:Frei ab|Verfügbar|Bezugsfrei)\s*\|?\s*([\d.]+)", text, re.I)
        available = avail_match.group(1) if avail_match else ""

        title_text = api_item.get("title", {}).get("rendered", "")
        wbs_required = bool(re.search(r"\bWBS\b", title_text + " " + text))
        if re.search(r"ohne\s+WBS|kein\s+WBS|WBS\s+nicht\s+erforderlich|kein\s+Wohnberechtigungs", title_text + " " + text, re.I):
            wbs_required = False

        return Listing(
            id=f"gewobag:{api_item['slug']}",
            source="gewobag",
            title=title_text,
            address=street,
            district=district,
            zip_code=zip_code,
            rooms=rooms,
            sqm=sqm,
            cold_rent=cold_rent,
            total_rent=total_rent,
            available_from=available,
            wbs_required=wbs_required,
            detail_url=detail_url,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Source: Berlinovo (Drupal server-rendered HTML)
# ---------------------------------------------------------------------------

BERLINOVO_URL = "https://www.berlinovo.de/de/wohnungen/suche"


def berlinovo_fetch_all(client: httpx.Client) -> Iterator[Listing]:
    page = 0
    while True:
        params = {"page": page} if page > 0 else {}
        resp = client.get(BERLINOVO_URL, params=params)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        rows = [r for r in soup.select(".views-row")
                if r.find("article", attrs={"about": True})]
        if not rows:
            break
        for row in rows:
            listing = _berlinovo_listing_from_row(row)
            if listing:
                yield listing

        # Check for next page
        next_link = soup.select_one("a[rel='next'], .pager__item--next a")
        if not next_link:
            break
        page += 1
        time.sleep(REQUEST_DELAY)


def _berlinovo_listing_from_row(row: BeautifulSoup) -> Optional[Listing]:
    try:
        article = row.find("article", attrs={"about": True})
        if not article:
            return None
        about = article["about"]  # e.g. "/wohnung-id/3011-2203-202"
        apt_id = about.split("/wohnung-id/")[-1]
        detail_url = f"https://www.berlinovo.de{about}"

        def field(name: str) -> str:
            el = article.select_one(f"[class*=field--name-field-{name}] .field__item")
            return el.get_text(strip=True) if el else ""

        title_el = article.select_one(".field--name-title a, [class*=field--name-title]")
        title_text = title_el.get_text(strip=True) if title_el else ""

        # Address fields
        street_el = article.select_one(".address-line1")
        street = street_el.get_text(strip=True) if street_el else ""
        zip_el = article.select_one(".postal-code")
        zip_code = zip_el.get_text(strip=True) if zip_el else ""
        locality_el = article.select_one(".locality")
        locality = locality_el.get_text(strip=True) if locality_el else ""

        # WBS: field-wbs boolean — "0" = not required, "1" = required
        wbs_el = article.select_one(".null-as-empty [class*=field-wbs] .field__item")
        wbs_val = wbs_el.get_text(strip=True) if wbs_el else "1"
        wbs_required = wbs_val.strip() not in ("0", "")

        # Rent/rooms/date
        rent_el = article.select_one("[class*=field-total-rent] .field__item")
        rent_text = rent_el.get_text(strip=True) if rent_el else ""
        rooms_el = article.select_one("[class*=field-rooms] .field__item[content]")
        rooms_val = rooms_el.get("content", "0") if rooms_el else "0"
        date_el = article.select_one("[class*=field-available-date] time")
        available = date_el.get_text(strip=True) if date_el else ""

        # District from title or locality area context
        district_hints = {
            "friedrichshain": "Friedrichshain-Kreuzberg",
            "kreuzberg": "Friedrichshain-Kreuzberg",
            "mitte": "Mitte",
            "prenzlauer": "Pankow",
            "pankow": "Pankow",
            "spandau": "Spandau",
            "lichtenberg": "Lichtenberg",
            "neukölln": "Neukölln",
            "charlottenburg": "Charlottenburg-Wilmersdorf",
            "marzahn": "Marzahn-Hellersdorf",
            "reinickendorf": "Reinickendorf",
            "tempelhof": "Tempelhof-Schöneberg",
            "treptow": "Treptow-Köpenick",
            "köpenick": "Treptow-Köpenick",
            "steglitz": "Steglitz-Zehlendorf",
        }
        combined = (title_text + " " + street + " " + locality).lower()
        district = ""
        for key, full in district_hints.items():
            if key in combined:
                district = full
                break

        return Listing(
            id=f"berlinovo:{apt_id}",
            source="berlinovo",
            title=title_text,
            address=street,
            district=district,
            zip_code=zip_code,
            rooms=parse_float_de(rooms_val),
            sqm=0.0,
            cold_rent=0.0,
            total_rent=parse_float_de(rent_text),
            available_from=available,
            wbs_required=wbs_required,
            detail_url=detail_url,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Source: Degewo (TYPO3 server-rendered HTML)
# ---------------------------------------------------------------------------

DEGEWO_URL = "https://www.degewo.de/immosuche/"


def degewo_fetch_all(client: httpx.Client) -> Iterator[Listing]:
    page = 0
    while True:
        params = {"tx_openimmo_immobilie[page]": page} if page > 0 else {}
        resp = client.get(DEGEWO_URL, params=params)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        cards = soup.select("article.immo-element:not(.teaserBox)")
        if not cards:
            break
        for card in cards:
            listing = _degewo_listing_from_card(card)
            if listing:
                yield listing

        next_link = soup.select_one(
            "a[rel='next'], .pagination a[aria-label*='next'], "
            ".tx-openimmo-immobilie-pager a[class*='next'], "
            "[class*=pagination] li.next a, [class*=pager] .next a"
        )
        if not next_link:
            # Also try numeric pagination: find current page link then check for page+1
            current_page_link = soup.select_one("[class*=pagination] .active a, [class*=pager] .active a")
            if current_page_link:
                next_page_href = None
                # Find sibling "next" anchor by looking one page ahead
                all_page_links = soup.select("[class*=pagination] a, [class*=pager] a")
                found_current = False
                for a in all_page_links:
                    if found_current:
                        next_page_href = a.get("href")
                        break
                    if a == current_page_link:
                        found_current = True
                if not next_page_href:
                    break
            else:
                break
        page += 1
        time.sleep(REQUEST_DELAY)


def _degewo_listing_from_card(card: BeautifulSoup) -> Optional[Listing]:
    try:
        link = card.select_one("a[href*='/immosuche/details/']")
        if not link:
            return None
        href = link["href"]
        slug = href.rstrip("/").split("/")[-1]
        detail_url = f"https://www.degewo.de{href}" if href.startswith("/") else href

        title_el = card.select_one(".imageTitle, h2, h3")
        title_text = title_el.get_text(strip=True) if title_el else ""

        address_el = card.select_one(".address")
        address_text = address_el.get_text(strip=True) if address_el else ""
        zip_match = re.search(r"(\d{5})\s+Berlin", address_text)
        zip_code = zip_match.group(1) if zip_match else ""
        street = address_text.split(",")[0].strip() if address_text else ""

        # District from title e.g. "2-Zimmer-Wohnung in Friedrichshain"
        district_match = re.search(
            r"\bin\s+([A-ZÄÖÜ][a-zäöüA-ZÄÖÜ\-]+(?:\s[A-ZÄÖÜ][a-zäöüA-ZÄÖÜ\-]+)*)\s*$",
            title_text,
        )
        district = district_match.group(1) if district_match else ""

        # If no district in title, check district badge (e.g. .area element)
        if not district:
            area_badge = card.select_one(".area, [class*=district], [class*=bezirk]")
            if area_badge:
                district = area_badge.get_text(strip=True)

        rent_el = card.select_one(".main-property-rent, [class*=rent]")
        rent_text = rent_el.get_text(strip=True) if rent_el else ""

        rooms_el = card.select_one(".main-property-rooms, [class*=rooms]")
        rooms_text = rooms_el.get_text(strip=True) if rooms_el else ""

        area_el = card.select_one(".main-property-area, [class*=area]")
        area_text = area_el.get_text(strip=True) if area_el else ""

        card_text = card.get_text(" ", strip=True).lower()
        wbs_required = "wbs" in card_text and "ohne wbs" not in card_text and "kein wbs" not in card_text and "nicht erforderlich" not in card_text

        return Listing(
            id=f"degewo:{slug}",
            source="degewo",
            title=title_text,
            address=street,
            district=district,
            zip_code=zip_code,
            rooms=parse_float_de(rooms_text),
            sqm=parse_float_de(area_text),
            cold_rent=0.0,
            total_rent=parse_float_de(rent_text),
            available_from="",
            wbs_required=wbs_required,
            detail_url=detail_url,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    def __init__(self, path: Path = DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_listings (
                id TEXT PRIMARY KEY,
                source TEXT,
                detail_url TEXT,
                address TEXT,
                district TEXT,
                rooms REAL,
                total_rent REAL,
                wbs_required INTEGER,
                dedup_key TEXT,
                first_seen_at INTEGER
            )
        """)
        # Add dedup_key column if upgrading from older DB
        try:
            self.conn.execute("ALTER TABLE seen_listings ADD COLUMN dedup_key TEXT")
        except sqlite3.OperationalError:
            pass
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_key ON seen_listings(dedup_key)")
        try:
            self.conn.execute("ALTER TABLE seen_listings ADD COLUMN wbm_applied INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def is_new(self, listing_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM seen_listings WHERE id = ?", (listing_id,)
        ).fetchone() is None

    def is_duplicate(self, listing: "Listing") -> bool:
        """True if another source already recorded a listing with the same address+rent."""
        if not listing.address or not listing.total_rent:
            return False
        row = self.conn.execute(
            "SELECT source FROM seen_listings WHERE dedup_key = ? AND id != ?",
            (listing.dedup_key, listing.id),
        ).fetchone()
        if row:
            console.print(f"  [dim]Dedup: skipping {listing.source} '{listing.address}' (already seen via {row[0]})[/dim]")
            return True
        return False

    def mark_seen(self, listing: "Listing") -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO seen_listings
               (id, source, detail_url, address, district, rooms, total_rent,
                wbs_required, dedup_key, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (listing.id, listing.source, listing.detail_url, listing.address,
             listing.district, listing.rooms, listing.total_rent,
             int(listing.wbs_required), listing.dedup_key, int(time.time())),
        )
        self.conn.commit()

    def mark_seen_id(self, listing_id: str, source: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_listings (id, source, first_seen_at) VALUES (?, ?, ?)",
            (listing_id, source, int(time.time())),
        )
        self.conn.commit()

    def mark_applied(self, listing_id: str) -> None:
        self.conn.execute(
            "UPDATE seen_listings SET wbm_applied = 1 WHERE id = ?", (listing_id,)
        )
        self.conn.commit()

    def is_applied(self, listing_id: str) -> bool:
        row = self.conn.execute(
            "SELECT wbm_applied FROM seen_listings WHERE id = ?", (listing_id,)
        ).fetchone()
        return bool(row and row[0])

    def is_applied_for_dedup(self, listing: "Listing") -> bool:
        """True if any listing with the same address+rent dedup key was already applied."""
        if not listing.dedup_key:
            return False
        row = self.conn.execute(
            "SELECT id FROM seen_listings WHERE dedup_key = ? AND wbm_applied = 1",
            (listing.dedup_key,),
        ).fetchone()
        return bool(row)

    def count(self, source: str = "") -> int:
        if source:
            return self.conn.execute(
                "SELECT COUNT(*) FROM seen_listings WHERE source = ?", (source,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]


# ---------------------------------------------------------------------------
# WBM auto-apply
# ---------------------------------------------------------------------------

def wbm_apply(page, listing: "Listing") -> bool:
    """Fill and submit the contact form on a WBM detail page using Playwright."""
    try:
        page.goto(listing.detail_url, wait_until="domcontentloaded", timeout=30000)
        # Wait for the form — this naturally handles redirects from IBW query-param URLs
        try:
            page.wait_for_selector("form.powermail_form", timeout=15000)
        except Exception:
            final_url = page.url
            page_text = page.inner_text("body")[:300].replace("\n", " ")
            console.print(f"  [yellow]WBM apply: no form found[/yellow] (landed on: {final_url})")
            console.print(f"  [dim]Page snippet: {page_text}[/dim]")
            return False

        final_url = page.url

        # Dismiss cookie banner and wait for it to be gone before interacting with form
        try:
            cookie = page.locator(".cm-btn-danger, .cn-decline").first
            cookie.click(timeout=3000)
            page.locator(".cm-btn-danger").wait_for(state="hidden", timeout=5000)
        except Exception:
            pass

        form = page.locator("form.powermail_form")

        def fill(name_fragment: str, value: str) -> None:
            try:
                form.locator(f"[name*='{name_fragment}']").fill(value, timeout=10000)
            except Exception:
                pass

        def select_opt(name_fragment: str, value: str) -> None:
            try:
                form.locator(f"[name*='{name_fragment}']").select_option(value=value, timeout=5000)
            except Exception:
                pass

        def click_label_for(input_selector: str) -> None:
            el = page.query_selector(input_selector)
            if el:
                el_id = el.get_attribute("id")
                label = page.query_selector(f"label[for='{el_id}']") if el_id else None
                if label:
                    label.click()
                else:
                    el.click(force=True)

        # No WBS — click label for value=0 (radio is CSS-hidden)
        click_label_for("[name*=wbsvorhanden][value='0']")

        select_opt("anrede", os.getenv("WBM_ANREDE", "Herr"))
        fill("[vorname]", os.getenv("WBM_VORNAME", "Erick"))
        fill("][name]", os.getenv("WBM_NAME", "Ersada"))
        fill("strasse", os.getenv("WBM_STRASSE", "Güntzelstr. 40"))
        fill("plz", os.getenv("WBM_PLZ", "10717"))
        fill("][ort]", os.getenv("WBM_ORT", "Berlin"))
        fill("e_mail", os.getenv("WBM_EMAIL", "boveri.asea.jedi@gmail.com"))
        fill("telefon", os.getenv("WBM_TELEFON", "017617870144"))

        # Datenschutz checkbox — input is hidden, click its label
        click_label_for("[name*=datenschutz][type=checkbox]")

        form.locator("[type=submit]").click()
        page.wait_for_load_state("networkidle", timeout=15000)

        content = page.content().lower()
        if any(kw in content for kw in ("danke", "bestätigung", "ihre anfrage", "powermail_confirmation", "erfolgreich")):
            console.print(f"  [bold green]WBM applied:[/bold green] {final_url}")
            return True
        else:
            console.print(f"  [yellow]WBM apply: unexpected response (may have succeeded) for {final_url}[/yellow]")
            return False
    except Exception as e:
        console.print(f"  [red]WBM apply error: {e}[/red]")
        return False


# ---------------------------------------------------------------------------
# Email notifier
# ---------------------------------------------------------------------------

class EmailNotifier:
    def __init__(self):
        self.email_from = os.getenv("EMAIL_FROM", "")
        self.email_password = os.getenv("EMAIL_PASSWORD", "")
        raw_to = os.getenv("EMAIL_TO", self.email_from)
        self.email_to = [e.strip() for e in raw_to.split(",") if e.strip()]
        self.enabled = bool(self.email_from and self.email_password)
        if not self.enabled:
            console.print("[yellow]Email not configured — set EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO in .env[/yellow]")

    def send(self, listing: Listing) -> None:
        console.print(f"\n[bold green]NEW MATCH:[/bold green] {listing.format_console()}")
        console.print(f"           {listing.detail_url}")
        if not self.enabled:
            return
        subject = (
            f"🏠 [{listing.source.upper()}] Neue Wohnung: {listing.rooms:.1f}Z, "
            f"{listing.total_rent:.0f}€ — {listing.district or listing.address[:20]}"
        )
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.email_from
        msg["To"] = ", ".join(self.email_to)
        msg.attach(MIMEText(listing.format_email_html(), "html", "utf-8"))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(self.email_from, self.email_password)
                smtp.sendmail(self.email_from, self.email_to, msg.as_string())
            console.print(f"[green]Email sent to {', '.join(self.email_to)}[/green]")
        except Exception as e:
            console.print(f"[red]Email send failed: {e}[/red]")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class Filters:
    def __init__(self):
        self.min_rooms = float(os.getenv("MIN_ROOMS", "2"))
        self.max_rent = float(os.getenv("MAX_RENT", "2500"))
        raw_districts = os.getenv("DISTRICTS", "")
        self.districts = [d.strip().lower() for d in raw_districts.split(",") if d.strip()]
        wbs_pref = os.getenv("WBS", "any").lower()
        self.wbs: Optional[bool] = None
        if wbs_pref == "required":
            self.wbs = True
        elif wbs_pref == "not_required":
            self.wbs = False

    def matches(self, listing: Listing) -> bool:
        if listing.rooms > 0 and listing.rooms < self.min_rooms:
            return False
        rent = listing.total_rent if listing.total_rent > 0 else listing.cold_rent
        if rent > 0 and rent > self.max_rent:
            return False
        if self.districts:
            haystack = (listing.district + " " + listing.address + " " + listing.zip_code + " " + listing.title).lower()
            if not any(d in haystack for d in self.districts):
                return False
        if self.wbs is not None and listing.wbs_required != self.wbs:
            return False
        return True

    def describe(self) -> str:
        parts = [f"≥{self.min_rooms:.0f} Zimmer", f"≤{self.max_rent:.0f}€"]
        if self.districts:
            parts.append(f"Bezirke: {', '.join(self.districts)}")
        if self.wbs is True:
            parts.append("WBS erforderlich")
        elif self.wbs is False:
            parts.append("kein WBS")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------

def run_once(db: Database, filters: Filters, notifier: EmailNotifier, seed: bool = False) -> None:
    client = httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                 "Accept-Language": "de-DE,de;q=0.9"},
        follow_redirects=True,
        timeout=20,
    )
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = "[bold yellow]SEEDING[/bold yellow]" if seed else "[bold]Crawling all sources...[/bold]"
    console.print(f"\n{label} ({filters.describe()}) — [dim]{timestamp}[/dim]")

    total_new = 0
    # Direct sources first so IBW duplicates are caught on the same run
    # WBM uses its own Playwright browser; other sources use httpx client
    for name, fn in [
        ("WBM",               lambda: _run_wbm(db, filters, notifier)),
        ("Gewobag",           lambda: _run_gewobag(client, db, filters, notifier, seed_only=seed)),
        ("Berlinovo",         lambda: _run_berlinovo(client, db, filters, notifier)),
        ("Degewo",            lambda: _run_degewo(client, db, filters, notifier)),
        ("inberlinwohnen.de", lambda: _run_ibw(client, db, filters, notifier)),
    ]:
        try:
            new = fn()
            console.print(f"  {name}: [green]{new} new matches[/green] | DB: {db.count()}")
            total_new += new
        except Exception as e:
            console.print(f"  [red]{name} error: {e}[/red]")
        time.sleep(REQUEST_DELAY)

    console.print(f"\nTotal new matches: [bold green]{total_new}[/bold green] | Total in DB: {db.count()}")
    client.close()


def _run_ibw(client, db, filters, notifier) -> int:
    all_ids, rendered = ibw_fetch_all(client)
    new_matches = 0
    wbm_listings_to_apply = []
    for apt_id in all_ids:
        full_id = f"ibw:{apt_id}"
        if not db.is_new(full_id):
            continue
        listing = rendered.get(apt_id)
        if listing:
            db.mark_seen(listing)
            if filters.matches(listing) and not db.is_duplicate(listing):
                notifier.send(listing)
                new_matches += 1
                if "wbm.de" in listing.detail_url:
                    wbm_listings_to_apply.append((full_id, listing))
        else:
            db.mark_seen_id(full_id, "ibw")

    if wbm_listings_to_apply:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                for full_id, listing in wbm_listings_to_apply:
                    applied = wbm_apply(page, listing)
                    if applied:
                        db.mark_applied(full_id)
                        console.print(f"  [bold green]WBM applied (via IBW):[/bold green] {listing.address}")
                    else:
                        console.print(f"  [yellow]WBM apply failed (via IBW):[/yellow] {listing.address}")
            finally:
                browser.close()

    return new_matches


def _run_wbm(db, filters, notifier) -> int:
    new_matches = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for listing in wbm_fetch_all(page):
                if not db.is_new(listing.id):
                    applied_status = "applied" if db.is_applied(listing.id) else "not applied"
                    console.print(f"  [dim]WBM already seen ({applied_status}): {listing.address}[/dim]")
                    continue
                db.mark_seen(listing)
                if not filters.matches(listing):
                    continue
                is_dup = db.is_duplicate(listing)
                if not is_dup:
                    notifier.send(listing)
                    new_matches += 1
                if db.is_applied_for_dedup(listing):
                    console.print(f"  [dim]WBM apply skipped (already applied via IBW): {listing.address}[/dim]")
                    db.mark_applied(listing.id)
                else:
                    applied = wbm_apply(page, listing)
                    if applied:
                        db.mark_applied(listing.id)
                        if is_dup:
                            console.print(f"  [bold green]WBM applied (cross-source dup):[/bold green] {listing.address}")
                    else:
                        console.print(f"  [yellow]WBM apply failed or no form:[/yellow] {listing.address}")
        finally:
            browser.close()
    return new_matches


def _run_gewobag(client, db, filters, notifier, seed_only: bool = False) -> int:
    new_matches = 0
    for api_item in gewobag_fetch_all(client):
        full_id = f"gewobag:{api_item['slug']}"
        if not db.is_new(full_id):
            continue
        if seed_only:
            db.mark_seen_id(full_id, "gewobag")
            continue
        time.sleep(REQUEST_DELAY)
        listing = _gewobag_fetch_detail(client, api_item)
        if listing:
            db.mark_seen(listing)
            if filters.matches(listing) and not db.is_duplicate(listing):
                notifier.send(listing)
                new_matches += 1
        else:
            db.mark_seen_id(full_id, "gewobag")
    return new_matches


def _run_berlinovo(client, db, filters, notifier) -> int:
    new_matches = 0
    for listing in berlinovo_fetch_all(client):
        if not db.is_new(listing.id):
            continue
        db.mark_seen(listing)
        if filters.matches(listing) and not db.is_duplicate(listing):
            notifier.send(listing)
            new_matches += 1
    return new_matches


def _run_degewo(client, db, filters, notifier) -> int:
    new_matches = 0
    for listing in degewo_fetch_all(client):
        if not db.is_new(listing.id):
            continue
        db.mark_seen(listing)
        if filters.matches(listing) and not db.is_duplicate(listing):
            notifier.send(listing)
            new_matches += 1
    return new_matches


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def dry_run(filters: Filters) -> None:
    client = httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        follow_redirects=True, timeout=20,
    )
    console.print(f"[bold yellow]DRY RUN[/bold yellow] — filter: {filters.describe()}\n")

    all_listings: List[Listing] = []

    console.print("[bold]inberlinwohnen.de[/bold]")
    try:
        all_ids, rendered = ibw_fetch_all(client)
        console.print(f"  Total IDs: {len(all_ids)} | Pre-rendered: {len(rendered)}")
        all_listings.extend(rendered.values())
    except Exception as e:
        console.print(f"  [red]Error: {e}[/red]")

    console.print("[bold]WBM[/bold]")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            items = list(wbm_fetch_all(page))
            browser.close()
        console.print(f"  Found: {len(items)}")
        all_listings.extend(items)
    except Exception as e:
        console.print(f"  [red]Error: {e}[/red]")

    for name, fn in [("Berlinovo", berlinovo_fetch_all), ("Degewo", degewo_fetch_all)]:
        console.print(f"[bold]{name}[/bold]")
        try:
            items = list(fn(client))
            console.print(f"  Found: {len(items)}")
            all_listings.extend(items)
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

    console.print("[bold]Gewobag[/bold] (district-filtered, fetching details...)")
    try:
        api_items = list(gewobag_fetch_all(client))
        console.print(f"  API items in target districts: {len(api_items)}")
        for api_item in api_items:
            listing = _gewobag_fetch_detail(client, api_item)
            if listing:
                all_listings.append(listing)
            time.sleep(REQUEST_DELAY)
        console.print(f"  Detail pages fetched: {len([l for l in all_listings if l.source == 'gewobag'])}")
    except Exception as e:
        console.print(f"  [red]Error: {e}[/red]")

    table = Table(title=f"\nAll listings — matching filter: {filters.describe()}")
    table.add_column("Source", style="dim")
    table.add_column("Address", max_width=30)
    table.add_column("Rooms")
    table.add_column("€ gesamt")
    table.add_column("WBS")
    table.add_column("Match", style="bold green")

    matching = 0
    for l in all_listings:
        m = filters.matches(l)
        if m:
            matching += 1
        table.add_row(
            l.source,
            f"{l.address}, {l.district}"[:30],
            str(l.rooms),
            f"{l.total_rent:.0f}" if l.total_rent else "?",
            "ja" if l.wbs_required else "nein",
            "✓" if m else "",
        )
    console.print(table)
    console.print(f"\nMatching: {matching}/{len(all_listings)}")
    client.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Berlin apartment crawler")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--seed", action="store_true", help="Seed DB with current listings (no alerts)")
    parser.add_argument("--interval", type=int, default=10, help="Poll interval in minutes")
    parser.add_argument("--dry-run", action="store_true", help="Show results without writing to DB")
    args = parser.parse_args()

    filters = Filters()
    notifier = EmailNotifier()

    try:
        if args.dry_run:
            dry_run(filters)
            return
        db = Database()
        if args.seed:
            run_once(db, filters, notifier, seed=True)
            console.print("\n[bold green]DB seeded. Run without --seed to start monitoring.[/bold green]")
        elif args.once:
            run_once(db, filters, notifier)
        else:
            console.print(f"[bold]Starting — polling every {args.interval} minutes[/bold] | Ctrl+C to stop\n")
            while True:
                try:
                    run_once(db, filters, notifier)
                except Exception as e:
                    console.print(f"[red]Run error: {e}[/red]")
                console.print(f"Sleeping {args.interval}m...\n")
                time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        console.print("\n[bold]Stopped.[/bold]")


if __name__ == "__main__":
    main()
