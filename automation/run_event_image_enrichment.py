#!/usr/bin/env python3
"""Hosted Third Castle image lane.

Reads the two source tabs read-only, performs deterministic no-AI enrichment,
and publishes only validated derivatives and their provenance manifest to R2.
It never edits the Sheet or generates the events page.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

from sparch_event_images import R2Storage, enrich_events


# These are cost-safety ceilings, not expected operating levels. The current
# Third Castle run contains 225 events and produces less than 4 MB of WebPs.
MAX_EVENTS_PER_RUN = 500
MAX_UPLOAD_BYTES_PER_RUN = 250 * 1024 * 1024
MAX_OBJECT_WRITES_PER_RUN = 600


def checkbox(value) -> bool:
    return str(value or "").strip().casefold() in {"true", "yes", "y", "1", "x", "checked"}


def join_key(title, date, venue) -> str:
    return "\x1f".join(str(value or "").strip().casefold() for value in (title, date, venue))


def has_event_data(row: dict) -> bool:
    """Ignore rows made non-empty only by an unchecked checkbox.

    Google Sheets returns an unchecked checkbox as the string ``FALSE``. The
    Master tab intentionally carries checkbox validation beyond the populated
    event rows, so row truthiness alone is not evidence that an event exists.
    """
    return any(
        str(row.get(field, "") or "").strip()
        for field in ("Title", "Date", "Venue", "URL")
    )


def rows_as_dicts(service, sheet_id: str, tab_range: str) -> list[dict]:
    response = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=tab_range,
    ).execute()
    values = response.get("values", [])
    if len(values) < 2:
        return []
    headers = [str(value).strip() for value in values[0]]
    return [
        {header: str(row[index]).strip() if index < len(row) else ""
         for index, header in enumerate(headers)}
        for row in values[1:] if row
    ]


def load_events(service, sheet_id: str, venue_manifest: Path) -> list[dict]:
    master = rows_as_dicts(service, sheet_id, "Master!A1:Q3000")
    candidates = rows_as_dicts(service, sheet_id, "Candidates_Web!A1:Z5000")

    by_url = {}
    by_event = {}
    for row in candidates:
        image_url = row.get("image_url", "")
        if not image_url.startswith("https://"):
            continue
        for field in ("clean_url", "source_url"):
            url = row.get(field, "")
            if url.startswith("https://"):
                by_url.setdefault(url.rstrip("/"), image_url)
        by_event.setdefault(
            join_key(row.get("title"), row.get("date"), row.get("location")),
            image_url,
        )

    venue_payload = json.loads(venue_manifest.read_text(encoding="utf-8"))
    venue_images = {
        str(name).strip().casefold(): str(url).strip()
        for name, url in venue_payload.get("venues", {}).items()
        if str(url).strip().startswith("https://")
    }

    events = []
    for row in master:
        if not has_event_data(row):
            continue
        event = {
            "title": row.get("Title", ""),
            "date": row.get("Date", ""),
            "venue": row.get("Venue", ""),
            "url": row.get("URL", ""),
            "editorial_feature": checkbox(row.get("Editorial Feature")),
        }
        key = join_key(event["title"], event["date"], event["venue"])
        event_url = event["url"].rstrip("/")
        event["image_url"] = by_url.get(event_url) or by_event.get(key) or ""
        event["venue_image_url"] = venue_images.get(event["venue"].strip().casefold(), "")
        events.append(event)
    return events


def sheets_service():
    credentials = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_SHEETS_KEY_FILE"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("event-image-manifest.json"))
    parser.add_argument(
        "--venue-manifest", type=Path,
        default=Path(__file__).with_name("venue-images.json"),
    )
    args = parser.parse_args()

    events = load_events(
        sheets_service(),
        os.environ["THIRD_CASTLE_SHEET_ID"],
        args.venue_manifest,
    )
    if len(events) > MAX_EVENTS_PER_RUN:
        raise SystemExit(
            f"Refusing image enrichment for {len(events)} events; "
            f"safety maximum is {MAX_EVENTS_PER_RUN}"
        )
    selected = sum(event["editorial_feature"] for event in events)
    if selected > 4:
        raise SystemExit(f"Editorial Feature has {selected} selections; maximum is four")

    storage = R2Storage.from_environment(
        max_upload_bytes=MAX_UPLOAD_BYTES_PER_RUN,
        max_object_writes=MAX_OBJECT_WRITES_PER_RUN,
    ) if args.publish else None
    manifest = enrich_events(events, storage=storage)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "mode": manifest["mode"],
        "events": manifest["event_count"],
        "fetched": manifest["unique_urls_fetched"],
        "reused": manifest["unique_urls_reused"],
        "featured": manifest["featured_selection"]["count"],
        "summary": manifest["summary"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
