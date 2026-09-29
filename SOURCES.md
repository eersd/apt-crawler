# Apartment sources — Berlin

## Automated by this crawler

State-owned / municipal housing companies. These tend to have below-market, rent-capped
units — the best €/m² deals in Berlin — but high demand and often a WBS requirement.

| Source | URL | Notes |
|---|---|---|
| Inberlinwohnen (aggregator) | inberlinwohnen.de/wohnungsfinder | Aggregates listings across most state-owned companies |
| WBM | wbm.de/wohnungen-berlin/angebote | |
| Gewobag | gewobag.de | |
| Berlinovo | berlinovo.de/de/wohnungen/suche | Mostly temporary/serviced apartments |
| Degewo | degewo.de/immosuche | |

## Not yet automated — same category (state-owned)

Worth adding to the crawler eventually since they're the same low-rent, low-€/m² pool:

- **HOWOGE** — howoge.de
- **GESOBAU** — gesobau.de
- **Stadt und Land** — stadtundland.de
- **Deutsche Wohnen** — deutsche-wohnen.com (large regulated-rent portfolio, not fully state-owned but similar rent profile)

## General market aggregators (manual check)

Bigger pool, more listings, but mostly market-rate — good deals exist but you have to
filter by €/m² yourself since these platforms rarely enforce rent caps.

- **ImmobilienScout24** — immobilienscout24.de — largest German portal, has price/m² filter
- **Immowelt** — immowelt.de — merged with Immonet, second-largest portal
- **eBay Kleinanzeigen (Immobilien)** — kleinanzeigen.de — private landlords, often below-portal-average rent since it skips agency fees
- **WG-Gesucht** — wg-gesucht.de — mostly shared flats but has a "1-Zimmer-Wohnung"/whole-apartment filter
- **Wohnungsboerse.net** — wohnungsboerse.net
- **Neubaukompass** — neubaukompass.de — new-build only

## Alert / notification services

Instead of manually refreshing, these watch multiple portals and notify you — same idea
as this crawler, but covering the market-rate portals above.

- **Wohnalert** — wohnalert.com — the one you found; aggregates across ImmoScout24, Immowelt, Kleinanzeigen etc. and alerts on new matches. Good complement to this crawler for market-rate deals.
- ImmoScout24's own saved-search email alerts (free, built into the site)

## Reference for judging "good deal" (€/m²)

To tell if a listing is actually cheap, compare its Kaltmiete/m² against the district
average:

- **Berlin Mietspiegel** (official rent index) — berlin.de/mieterberatung/mietspiegel — published every 2 years, gives the legally-recognized average rent per district/building age/condition
- Portal-published district averages (ImmoScout24 and Immowelt both publish live "Mietspiegel"/market-report pages per district)

A rough sanity check: anything meaningfully below the current Mietspiegel value for the
district + Baujahr is a strong signal, independent of how "cheap" it looks in absolute €.
