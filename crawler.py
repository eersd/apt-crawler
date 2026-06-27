"""
Berlin state-owned apartment crawler for inberlinwohnen.de.

The site uses Laravel Livewire. On page load:
  - A master component embeds all apartment IDs in wire:snapshot["data"]["itemIds"]
  - The first 10 items are pre-rendered with full structured JSON in their own wire:snapshot

Strategy:
  1. Extract all IDs from the master snapshot (instant, no pagination needed)
  2. Diff against DB to find new IDs
  3. For pre-rendered items: parse details directly from wire:snapshot
  4. Send email alert for any new apartments matching the filter
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
from typing import Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()
console = Console()

BASE_URL = "https://www.inberlinwohnen.de/wohnungsfinder/"
DB_PATH = Path("seen_listings.db")


@dataclass
class Listing:
    id: int
    title: str
    address: str
    district: str
    zip_code: str
    rooms: float
    sqm: float
    cold_rent: float
    extra_costs: float
    total_rent: float
    available_from: str
    wbs_required: bool
    company: str
    detail_url: str

    def format_email_html(self) -> str:
        wbs = "WBS erforderlich" if self.wbs_required else "kein WBS"
        return f"""
<html><body style="font-family:sans-serif;max-width:520px;margin:auto;color:#222">
  <h2 style="color:#1a5276">🏠 Neue Wohnung gefunden</h2>
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
    <tr style="background:#f4f6f7"><td style="padding:6px 0;font-weight:bold">Anbieter</td>
        <td>{self.company}</td></tr>
  </table>
  <br>
  <a href="{self.detail_url}"
     style="background:#1a5276;color:white;padding:12px 24px;text-decoration:none;
            border-radius:4px;display:inline-block;font-size:16px">
    Zur Wohnung →
  </a>
  <p style="color:#888;font-size:12px;margin-top:24px">
    inberlinwohnen.de Crawler
  </p>
</body></html>"""

    def format_console(self) -> str:
        wbs = "WBS" if self.wbs_required else "   "
        return (
            f"{self.rooms:.1f}Z | {self.sqm:.1f}m² | "
            f"{self.cold_rent:.0f}€ kalt | {wbs} | "
            f"{self.address}, {self.district} | {self.company}"
        )


def parse_float_de(s: str) -> float:
    """Parse German-locale float string like '42,96' or '751.92'."""
    if s is None:
        return 0.0
    s = str(s).strip()
    # If already a proper float (dot decimal, like rentGross)
    try:
        return float(s)
    except ValueError:
        pass
    # German format: remove dots (thousands sep), replace comma with dot
    s = re.sub(r"[^\d,]", "", s).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def listing_from_item_data(data: dict) -> Optional[Listing]:
    """Build a Listing from the structured JSON in a wire:snapshot item card."""
    try:
        item = data["item"][0] if isinstance(data["item"], list) else data["item"]

        # Address
        addr_raw = item.get("address", [])
        addr = addr_raw[0] if isinstance(addr_raw, list) and addr_raw else {}
        street = f"{addr.get('street', '')} {addr.get('number', '')}".strip()
        district = addr.get("district", "")
        zip_code = addr.get("zipCode", "")

        # Company
        company_raw = item.get("company", [])
        company = company_raw[0] if isinstance(company_raw, list) and company_raw else {}
        company_name = company.get("name", "unknown") if isinstance(company, dict) else str(company)

        # WBS detection from the details tree: find {"label": "WBS", "value": "..."}
        title = item.get("title", "")
        wbs_required = False
        details_raw = item.get("details", [])
        details_str = json.dumps(details_raw, ensure_ascii=False)
        # Extract the value of the WBS detail entry
        wbs_match = re.search(
            r'"label"\s*:\s*"WBS"[^}]*"value"\s*:\s*"([^"]*)"', details_str
        )
        if wbs_match:
            wbs_value = wbs_match.group(1).lower()
            wbs_required = "nicht erforderlich" not in wbs_value
        else:
            # Fallback: if no WBS field found at all, assume required to be safe
            wbs_required = True

        return Listing(
            id=item["id"],
            title=title,
            address=street,
            district=district,
            zip_code=zip_code,
            rooms=parse_float_de(item.get("rooms", "0")),
            sqm=parse_float_de(item.get("area", "0")),
            cold_rent=parse_float_de(item.get("rentNet", "0")),
            extra_costs=parse_float_de(item.get("extraCosts", "0")),
            total_rent=parse_float_de(item.get("rentGross", "0")),
            available_from=item.get("occupationDate", ""),
            wbs_required=wbs_required,
            company=company_name,
            detail_url=item.get("deeplink", ""),
        )
    except Exception as e:
        console.print(f"[yellow]Warning: failed to parse item data: {e}[/yellow]")
        return None


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_listings (
                id INTEGER PRIMARY KEY,
                detail_url TEXT,
                address TEXT,
                district TEXT,
                rooms REAL,
                sqm REAL,
                cold_rent REAL,
                total_rent REAL,
                company TEXT,
                wbs_required INTEGER,
                available_from TEXT,
                first_seen_at INTEGER
            )
        """)
        self.conn.commit()

    def is_new(self, listing_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM seen_listings WHERE id = ?", (listing_id,)
        ).fetchone() is None

    def mark_seen(self, listing: Listing) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO seen_listings
               (id, detail_url, address, district, rooms, sqm, cold_rent, total_rent,
                company, wbs_required, available_from, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                listing.id, listing.detail_url, listing.address, listing.district,
                listing.rooms, listing.sqm, listing.cold_rent, listing.total_rent,
                listing.company, int(listing.wbs_required), listing.available_from,
                int(time.time()),
            ),
        )
        self.conn.commit()

    def mark_seen_id_only(self, listing_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_listings (id, first_seen_at) VALUES (?, ?)",
            (listing_id, int(time.time())),
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]


class EmailNotifier:
    def __init__(self):
        self.email_from = os.getenv("EMAIL_FROM", "")
        self.email_password = os.getenv("EMAIL_PASSWORD", "")
        raw_to = os.getenv("EMAIL_TO", self.email_from)
        self.email_to = [e.strip() for e in raw_to.split(",") if e.strip()]
        self.enabled = bool(self.email_from and self.email_password)
        if not self.enabled:
            console.print("[yellow]Email not configured — alerts printed to console only[/yellow]")
            console.print("[yellow]Set EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO in .env[/yellow]")

    def send(self, listing: Listing) -> None:
        console.print(f"\n[bold green]NEW MATCH:[/bold green] {listing.format_console()}")
        console.print(f"           {listing.detail_url}")

        if not self.enabled:
            return

        subject = (
            f"🏠 Neue Wohnung: {listing.rooms:.1f}Z, {listing.sqm:.1f}m², "
            f"{listing.total_rent:.0f}€ — {listing.district}"
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
        if listing.rooms < self.min_rooms:
            return False
        rent = listing.total_rent if listing.total_rent > 0 else listing.cold_rent
        if rent > self.max_rent:
            return False
        if self.districts:
            haystack = (listing.district + " " + listing.address + " " + listing.zip_code).lower()
            if not any(d in haystack for d in self.districts):
                return False
        if self.wbs is not None and listing.wbs_required != self.wbs:
            return False
        return True

    def describe(self) -> str:
        parts = [f"≥{self.min_rooms:.0f} Zimmer", f"≤{self.max_rent:.0f}€ Gesamt"]
        if self.districts:
            parts.append(f"Bezirke: {', '.join(self.districts)}")
        if self.wbs is True:
            parts.append("WBS erforderlich")
        elif self.wbs is False:
            parts.append("kein WBS")
        return " | ".join(parts)


class Crawler:
    def __init__(self):
        self.client = httpx.Client(
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            },
            follow_redirects=True,
            timeout=20,
        )

    def fetch_page(self) -> BeautifulSoup:
        resp = self.client.get(BASE_URL)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")

    def extract_all_ids(self, soup: BeautifulSoup) -> List[int]:
        """Extract the full list of apartment IDs from the master Livewire snapshot."""
        lw = soup.select_one("[wire\\:snapshot][wire\\:effects*='nextPage']")
        if not lw:
            # Fallback: find the component with itemIds
            for el in soup.select("[wire\\:snapshot]"):
                try:
                    snap = json.loads(el["wire:snapshot"])
                    if "itemIds" in snap.get("data", {}):
                        lw = el
                        break
                except (json.JSONDecodeError, KeyError):
                    pass
        if not lw:
            return []
        snap = json.loads(lw["wire:snapshot"])
        item_ids_raw = snap["data"].get("itemIds", [[]])
        ids = item_ids_raw[0] if isinstance(item_ids_raw, list) and item_ids_raw else []
        return [int(i) for i in ids]

    def extract_rendered_listings(self, soup: BeautifulSoup) -> Dict[int, Listing]:
        """Parse the pre-rendered apartment cards (first 10) from their wire:snapshots."""
        listings = {}
        for card in soup.select("[id^='apartment-']"):
            try:
                snap = json.loads(card["wire:snapshot"])
                listing = listing_from_item_data(snap["data"])
                if listing:
                    listings[listing.id] = listing
            except (KeyError, json.JSONDecodeError):
                pass
        return listings

    def run_once(self, db: "Database", filters: "Filters", notifier: "EmailNotifier") -> int:
        """One crawl cycle. Returns count of new matching listings found."""
        console.print(f"\n[bold]Crawling...[/bold] (filter: {filters.describe()})")

        soup = self.fetch_page()
        all_ids = self.extract_all_ids(soup)
        rendered = self.extract_rendered_listings(soup)

        console.print(f"Total apartments on site: {len(all_ids)} | Pre-rendered with data: {len(rendered)}")

        new_count = 0
        for apt_id in all_ids:
            if not db.is_new(apt_id):
                continue

            listing = rendered.get(apt_id)
            if listing:
                db.mark_seen(listing)
                if filters.matches(listing):
                    notifier.send(listing)
                    new_count += 1
            else:
                # ID is new but not pre-rendered — mark it seen (no details available yet)
                db.mark_seen_id_only(apt_id)

        console.print(
            f"New IDs detected: {sum(1 for i in all_ids if not db.is_new(i) is False)} | "
            f"New matching: [bold green]{new_count}[/bold green] | "
            f"Total in DB: {db.count()}"
        )
        return new_count

    def dry_run(self, filters: "Filters") -> None:
        """Show what the crawler sees without writing to DB."""
        console.print("[bold yellow]DRY RUN — no DB writes[/bold yellow]")
        soup = self.fetch_page()
        all_ids = self.extract_all_ids(soup)
        rendered = self.extract_rendered_listings(soup)

        console.print(f"Total IDs on site: {len(all_ids)}")
        console.print(f"Pre-rendered listings with data: {len(rendered)}\n")

        table = Table(title=f"Pre-rendered listings (filter: {filters.describe()})")
        table.add_column("ID", style="dim")
        table.add_column("Address", style="cyan", max_width=35)
        table.add_column("Rooms")
        table.add_column("m²")
        table.add_column("Kalt €")
        table.add_column("Gesamt €")
        table.add_column("WBS")
        table.add_column("Match", style="bold")
        table.add_column("Company", max_width=12)

        for listing in rendered.values():
            match = "✓" if filters.matches(listing) else ""
            table.add_row(
                str(listing.id),
                f"{listing.address}, {listing.district}",
                str(listing.rooms),
                str(listing.sqm),
                f"{listing.cold_rent:.0f}",
                f"{listing.total_rent:.0f}",
                "ja" if listing.wbs_required else "nein",
                match,
                listing.company[:12],
            )
        console.print(table)

        matching = [l for l in rendered.values() if filters.matches(l)]
        console.print(f"\nMatching in pre-rendered set: {len(matching)}/{len(rendered)}")
        console.print(f"All IDs (first 20): {all_ids[:20]}")

    def close(self):
        self.client.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Berlin apartment crawler — inberlinwohnen.de")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--interval", type=int, default=10, help="Poll interval in minutes (default 10)")
    parser.add_argument("--dry-run", action="store_true", help="Show results without writing to DB")
    args = parser.parse_args()

    filters = Filters()
    notifier = EmailNotifier()
    crawler = Crawler()

    try:
        if args.dry_run:
            crawler.dry_run(filters)
            return

        db = Database()
        if args.once:
            crawler.run_once(db, filters, notifier)
        else:
            console.print(f"[bold]Starting — polling every {args.interval} minutes[/bold] | Ctrl+C to stop\n")
            while True:
                try:
                    crawler.run_once(db, filters, notifier)
                except httpx.HTTPError as e:
                    console.print(f"[red]HTTP error: {e} — retrying next cycle[/red]")
                console.print(f"Sleeping {args.interval}m...\n")
                time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        console.print("\n[bold]Stopped.[/bold]")
    finally:
        crawler.close()


if __name__ == "__main__":
    main()
