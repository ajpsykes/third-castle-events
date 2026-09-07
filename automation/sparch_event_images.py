#!/usr/bin/env python3
"""Deterministic event-image enrichment for Third Castle.

No AI service is involved. The module discovers explicit event metadata,
validates the response, strips metadata while resizing to a WebP derivative,
and can publish those small derivatives plus a provenance manifest to R2.

The CLI remains a local audit tool. Production publishing is opt-in through
``enrich_events`` and a supplied storage adapter; callers can always fall back
to an empty manifest without interrupting page generation.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, ImageFile, UnidentifiedImageError


Image.MAX_IMAGE_PIXELS = 50_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = False

USER_AGENT = "Mozilla/5.0 (compatible; Sparch event image enrichment/1.0; +https://www.thirdcastle.ie)"
PAGE_LIMIT = 3_000_000
IMAGE_LIMIT = 8_000_000
MIN_WIDTH = 480
MIN_HEIGHT = 270
MAX_EDGE = 800
WEBP_QUALITY = 78
GENERIC_IMAGE_WORDS = {"logo", "icon", "avatar", "favicon", "placeholder", "default", "sprite"}
STOP_WORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "live", "of", "on", "or",
    "presents", "the", "to", "with", "2026", "dublin", "event", "events",
}


def tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 2 and token not in STOP_WORDS
    }


def safe_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
            return False
        try:
            address = ipaddress.ip_address(parsed.hostname)
            if not address.is_global:
                return False
        except ValueError:
            pass
        return True
    except (TypeError, ValueError):
        return False


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.images: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
        self.json_ld_blocks: list[str] = []
        self._json_ld_parts: list[str] = []
        self._in_title = False
        self._in_json_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key and values.get("content") and key not in self.meta:
                self.meta[key] = values["content"].strip()
        elif tag.lower() == "img":
            source = values.get("src") or values.get("data-src") or values.get("data-lazy-src")
            if source:
                self.images.append((values.get("alt", ""), source.strip()))
        elif tag.lower() == "title":
            self._in_title = True
        elif tag.lower() == "script" and "ld+json" in values.get("type", "").lower():
            self._in_json_ld = True
            self._json_ld_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        elif tag.lower() == "script" and self._in_json_ld:
            block = "".join(self._json_ld_parts).strip()
            if block:
                self.json_ld_blocks.append(block)
            self._in_json_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_json_ld:
            self._json_ld_parts.append(data)


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def image_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        output = []
        for child in value:
            output.extend(image_values(child))
        return output
    if isinstance(value, dict):
        return image_values(value.get("url") or value.get("contentUrl") or "")
    return []


def discover_candidates(html: str, page_url: str, event_title: str) -> tuple[list[tuple[str, str]], str]:
    parser = MetadataParser()
    parser.feed(html)
    candidates: list[tuple[str, str]] = []
    page_title = parser.meta.get("og:title") or " ".join(parser.title_parts).strip()

    for raw_json in parser.json_ld_blocks:
        try:
            payload = json.loads(raw_json)
            for item in walk_json(payload):
                item_type = item.get("@type", "")
                kinds = {str(part).lower() for part in (item_type if isinstance(item_type, list) else [item_type])}
                if "event" not in kinds:
                    continue
                item_name = str(item.get("name") or "")
                if title_relevance(event_title, item_name) < 1:
                    continue
                for value in image_values(item.get("image")):
                    candidates.append(("event-jsonld", urljoin(page_url, value)))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    for key, kind in (("og:image:secure_url", "og"), ("og:image", "og"), ("twitter:image", "twitter")):
        if parser.meta.get(key):
            candidates.append((kind, urljoin(page_url, parser.meta[key])))

    event_words = tokens(event_title)
    for alt, source in parser.images:
        if event_words and len(event_words & tokens(alt)) >= min(2, len(event_words)):
            candidates.append(("matching-alt", urljoin(page_url, source)))

    unique = []
    seen = set()
    for kind, url in candidates:
        if url not in seen and safe_https_url(url):
            seen.add(url)
            unique.append((kind, url))
    return unique, page_title


def title_relevance(event_title: str, page_title: str) -> float:
    event_words = tokens(event_title)
    page_words = tokens(page_title)
    if not event_words or not page_words:
        return 0
    return len(event_words & page_words) / min(len(event_words), 4)


def read_limited(response: requests.Response, limit: int) -> bytes:
    length = response.headers.get("content-length")
    if length and int(length) > limit:
        raise ValueError(f"content length exceeds {limit} bytes")
    chunks = []
    size = 0
    for chunk in response.iter_content(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise ValueError(f"download exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def normalise_image(data: bytes) -> tuple[bytes, int, int, str]:
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        width, height = source.size
        source_format = str(source.format or "unknown").lower()
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            raise ValueError(f"image too small ({width}x{height})")
        ratio = width / height
        if ratio < 0.45 or ratio > 3.2:
            raise ValueError(f"extreme aspect ratio ({ratio:.2f})")
        image = source.convert("RGB")
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=WEBP_QUALITY, method=4, optimize=True)
        return output.getvalue(), width, height, source_format


def canonical_event_key(event: dict) -> str:
    """Stable, non-secret key for joining a manifest back to generated rows."""
    value = "\x1f".join(
        str(event.get(field) or "").strip().casefold()
        for field in ("title", "date", "venue")
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def fetch_candidate_image(
    image_url: str,
    referer: str,
    pacer: "DomainPacer",
    timeout: float,
) -> tuple[str, bytes, int, int, str, int]:
    """Fetch, validate and normalise one image without persisting it."""
    if not safe_https_url(image_url):
        raise ValueError("not a safe HTTPS image URL")
    pacer.wait(image_url)
    response = requests.get(
        image_url,
        headers={"User-Agent": USER_AGENT, "Accept": "image/*", "Referer": referer},
        timeout=timeout,
        stream=True,
        allow_redirects=True,
    )
    response.raise_for_status()
    if not safe_https_url(response.url):
        raise ValueError("image redirected outside safe HTTPS")
    image_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if not image_type.startswith("image/") or image_type == "image/svg+xml":
        raise ValueError(f"unsupported image type {image_type or 'unknown'}")
    source = read_limited(response, IMAGE_LIMIT)
    webp, width, height, source_format = normalise_image(source)
    return response.url, webp, width, height, source_format, len(source)


@dataclass
class AuditResult:
    url: str
    event_ids: list[str]
    title: str
    venue: str
    status: str
    reason: str = ""
    image_url: str = ""
    discovery: str = ""
    page_title: str = ""
    relevance: float = 0
    width: int = 0
    height: int = 0
    source_format: str = ""
    source_bytes: int = 0
    webp_bytes: int = 0
    asset_name: str = ""
    asset_url: str = ""
    asset_kind: str = ""
    checked_at: str = ""
    expires_at: str = ""
    cached: bool = False
    elapsed_ms: int = 0


class DomainPacer:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.locks: dict[str, threading.Lock] = {}
        self.last_request: dict[str, float] = {}
        self.guard = threading.Lock()

    def wait(self, url: str) -> None:
        domain = urlparse(url).netloc.lower()
        with self.guard:
            lock = self.locks.setdefault(domain, threading.Lock())
        with lock:
            delay = self.interval - (time.monotonic() - self.last_request.get(domain, 0))
            if delay > 0:
                time.sleep(delay)
            self.last_request[domain] = time.monotonic()


def audit_page(group: dict, pacer: DomainPacer, timeout: float, thumbnail_dir: Path | None) -> AuditResult:
    started = time.monotonic()
    page_url = group["url"]
    result = AuditResult(
        url=page_url,
        event_ids=group["event_ids"],
        title=group["title"],
        venue=group["venue"],
        status="failed",
    )

    def use_venue_fallback() -> bool:
        venue_image = str(group.get("venue_image_url") or "").strip()
        if not venue_image:
            return False
        try:
            final_url, webp, width, height, source_format, source_bytes = fetch_candidate_image(
                venue_image, page_url, pacer, timeout
            )
            digest = hashlib.sha256(webp).hexdigest()[:20]
            result.image_url = final_url
            result.discovery = "venue-manifest"
            result.asset_kind = "venue"
            result.width = width
            result.height = height
            result.source_format = source_format
            result.source_bytes = source_bytes
            result.webp_bytes = len(webp)
            result.asset_name = f"{digest}.webp"
            if thumbnail_dir:
                thumbnail_dir.mkdir(parents=True, exist_ok=True)
                (thumbnail_dir / result.asset_name).write_bytes(webp)
            result.status = "usable-candidate"
            result.reason = "approved venue fallback passed mechanical checks"
            result.relevance = 1
            return True
        except (requests.RequestException, OSError, ValueError, UnidentifiedImageError):
            return False

    if not safe_https_url(page_url):
        result.reason = "not a safe HTTPS URL"
        return result

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        # Ticketmaster already supplies a first-party image_url in
        # Candidates_Web. Prefer it because its event pages reject automated
        # reads, and there is no benefit in rediscovering the same asset.
        upstream_image = str(group.get("image_url") or "").strip()
        if upstream_image:
            try:
                final_url, webp, width, height, source_format, source_bytes = fetch_candidate_image(
                    upstream_image, page_url, pacer, timeout
                )
                digest = hashlib.sha256(webp).hexdigest()[:20]
                result.image_url = final_url
                result.discovery = "upstream"
                result.asset_kind = "event"
                result.width = width
                result.height = height
                result.source_format = source_format
                result.source_bytes = source_bytes
                result.webp_bytes = len(webp)
                result.asset_name = f"{digest}.webp"
                if thumbnail_dir:
                    thumbnail_dir.mkdir(parents=True, exist_ok=True)
                    (thumbnail_dir / result.asset_name).write_bytes(webp)
                result.status = "usable-candidate"
                result.reason = "upstream event image passed mechanical checks"
                result.relevance = 1
                return result
            except (requests.RequestException, OSError, ValueError, UnidentifiedImageError):
                # Continue through page metadata; upstream image failure is not
                # fatal and should not remove other possible discovery paths.
                pass

        pacer.wait(page_url)
        page = requests.get(page_url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
        page.raise_for_status()
        if not safe_https_url(page.url):
            raise ValueError("page redirected outside safe HTTPS")
        content_type = page.headers.get("content-type", "").split(";", 1)[0].lower()
        if "html" not in content_type:
            raise ValueError(f"page returned {content_type or 'unknown content type'}")
        html = read_limited(page, PAGE_LIMIT).decode(page.encoding or "utf-8", errors="replace")
        candidates, page_title = discover_candidates(html, page.url, group["title"])
        result.page_title = page_title[:300]
        result.relevance = round(title_relevance(group["title"], page_title), 3)
        if not candidates:
            if use_venue_fallback():
                return result
            result.status = "no-image"
            result.reason = "no structured, social or title-matched image"
            return result

        failures = []
        for discovery, image_url in candidates[:6]:
            if any(word in image_url.lower() for word in GENERIC_IMAGE_WORDS):
                failures.append("generic-looking image URL")
                continue
            try:
                pacer.wait(image_url)
                image_response = requests.get(
                    image_url,
                    headers={"User-Agent": USER_AGENT, "Accept": "image/*", "Referer": page.url},
                    timeout=timeout,
                    stream=True,
                    allow_redirects=True,
                )
                image_response.raise_for_status()
                if not safe_https_url(image_response.url):
                    raise ValueError("image redirected outside safe HTTPS")
                image_type = image_response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not image_type.startswith("image/") or image_type == "image/svg+xml":
                    raise ValueError(f"unsupported image type {image_type or 'unknown'}")
                source = read_limited(image_response, IMAGE_LIMIT)
                webp, width, height, source_format = normalise_image(source)
                digest = hashlib.sha256(webp).hexdigest()[:20]
                asset_name = f"{digest}.webp"
                if thumbnail_dir:
                    thumbnail_dir.mkdir(parents=True, exist_ok=True)
                    (thumbnail_dir / asset_name).write_bytes(webp)
                result.image_url = image_response.url
                result.discovery = discovery
                result.width = width
                result.height = height
                result.source_format = source_format
                result.source_bytes = len(source)
                result.webp_bytes = len(webp)
                result.asset_name = asset_name
                result.asset_kind = "event"
                if result.relevance >= 0.5 or discovery == "event-jsonld":
                    result.status = "usable-candidate"
                    result.reason = "metadata image passed mechanical checks"
                else:
                    result.status = "needs-review"
                    result.reason = "valid image but page-title relevance is weak"
                return result
            except (requests.RequestException, OSError, ValueError, UnidentifiedImageError) as error:
                failures.append(str(error))

        if use_venue_fallback():
            return result
        result.status = "no-image"
        result.reason = "; ".join(failures[:3]) or "candidate images failed validation"
        return result
    except (requests.RequestException, OSError, ValueError) as error:
        if use_venue_fallback():
            return result
        result.reason = str(error)
        return result
    finally:
        result.elapsed_ms = round((time.monotonic() - started) * 1000)


def group_events(events: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for event in events:
        url = str(event.get("url") or "").strip()
        if not url:
            continue
        group = groups.setdefault(url, {
            "url": url,
            "event_ids": [],
            "events": [],
            "title": str(event.get("title") or ""),
            "venue": str(event.get("venue") or ""),
            "image_url": str(event.get("image_url") or ""),
            "venue_image_url": str(event.get("venue_image_url") or ""),
        })
        group["event_ids"].append(str(event.get("id") or ""))
        group["events"].append({
            "event_key": canonical_event_key(event),
            "title": str(event.get("title") or ""),
            "venue": str(event.get("venue") or ""),
            "editorial_feature": bool(event.get("editorial_feature")),
        })
        if not group["image_url"] and event.get("image_url"):
            group["image_url"] = str(event["image_url"])
        if not group["venue_image_url"] and event.get("venue_image_url"):
            group["venue_image_url"] = str(event["venue_image_url"])
    return list(groups.values())


class R2Storage:
    """Small S3-compatible adapter; boto3 is imported only in publish mode."""

    def __init__(self, *, account_id: str, access_key_id: str,
                 secret_access_key: str, bucket: str, public_base_url: str,
                 max_upload_bytes: int = 250 * 1024 * 1024,
                 max_object_writes: int = 600) -> None:
        try:
            import boto3
        except ImportError as error:  # pragma: no cover - exercised in hosted runner
            raise RuntimeError("R2 publish mode requires boto3") from error
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self.max_upload_bytes = max_upload_bytes
        self.max_object_writes = max_object_writes
        self.uploaded_bytes = 0
        self.object_writes = 0
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    @classmethod
    def from_environment(cls, **safety_limits) -> "R2Storage":
        required = (
            "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET", "R2_PUBLIC_BASE_URL",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"Missing R2 configuration: {', '.join(missing)}")
        return cls(
            account_id=os.environ["R2_ACCOUNT_ID"],
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=os.environ["R2_BUCKET"],
            public_base_url=os.environ["R2_PUBLIC_BASE_URL"],
            **safety_limits,
        )

    def put_bytes(self, key: str, data: bytes, content_type: str,
                  cache_control: str) -> str:
        next_bytes = self.uploaded_bytes + len(data)
        next_writes = self.object_writes + 1
        if next_bytes > self.max_upload_bytes:
            raise RuntimeError(
                f"R2 upload safety limit exceeded: {next_bytes} bytes would exceed "
                f"{self.max_upload_bytes} bytes per run"
            )
        if next_writes > self.max_object_writes:
            raise RuntimeError(
                f"R2 write safety limit exceeded: {next_writes} objects would exceed "
                f"{self.max_object_writes} per run"
            )
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=cache_control,
        )
        self.uploaded_bytes = next_bytes
        self.object_writes = next_writes
        return f"{self.public_base_url}/{key}"

    def put_json(self, key: str, payload: dict) -> str:
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        return self.put_bytes(key, data, "application/json; charset=utf-8", "no-cache")

    def get_json(self, key: str):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return json.loads(response["Body"].read().decode("utf-8"))
        except Exception as error:  # boto's exception classes remain optional locally
            details = getattr(error, "response", {}) or {}
            code = str(details.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise

    def ensure_lifecycle(self) -> None:
        """Idempotently keep derived event assets for 90 days."""
        self.client.put_bucket_lifecycle_configuration(
            Bucket=self.bucket,
            LifecycleConfiguration={"Rules": [{
                "ID": "expire-derived-event-images-after-90-days",
                "Status": "Enabled",
                "Filter": {"Prefix": "events/"},
                "Expiration": {"Days": 90},
            }]},
        )


def cached_result(group: dict, record: dict, now: datetime) -> AuditResult | None:
    """Return a reusable result while its deliberately short audit TTL holds."""
    try:
        checked = datetime.fromisoformat(str(record.get("checked_at") or "").replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    status = str(record.get("status") or "")
    ttl_days = 30 if status == "usable-candidate" else 7
    if now - checked > timedelta(days=ttl_days):
        return None
    if status == "usable-candidate" and not record.get("asset_url"):
        return None
    return AuditResult(
        url=group["url"],
        event_ids=group.get("event_ids", []),
        title=group.get("title", ""),
        venue=group.get("venue", ""),
        status=status,
        reason=str(record.get("reason") or ""),
        image_url=str(record.get("source_image_url") or ""),
        discovery=str(record.get("discovery") or ""),
        relevance=float(record.get("relevance") or 0),
        width=int(record.get("source_width") or 0),
        height=int(record.get("source_height") or 0),
        source_bytes=int(record.get("source_bytes") or 0),
        webp_bytes=int(record.get("webp_bytes") or 0),
        asset_name=Path(str(record.get("asset_key") or "")).name,
        asset_url=str(record.get("asset_url") or ""),
        asset_kind=str(record.get("asset_kind") or ""),
        checked_at=checked.isoformat(timespec="seconds"),
        expires_at=str(record.get("expires_at") or ""),
        cached=True,
    )


def enrich_events(
    events: list[dict],
    *,
    storage=None,
    workers: int = 4,
    timeout: float = 14,
    per_domain_delay: float = 0.35,
    asset_prefix: str = "events/third-castle",
    manifest_key: str = "manifests/third-castle/current.json",
    prior_manifest: dict | None = None,
) -> dict:
    """Discover images and optionally publish derivatives plus provenance.

    When ``storage`` is None this performs the full network/validation pass but
    writes nothing outside an ephemeral temporary directory. The returned
    manifest is therefore suitable for CI dry runs and generator fallbacks.
    """
    groups = group_events(events)
    pacer = DomainPacer(max(0, per_domain_delay))
    started = time.monotonic()
    checked_at = datetime.now(timezone.utc)
    results: list[AuditResult] = []

    if prior_manifest is None and storage and hasattr(storage, "get_json"):
        prior_manifest = storage.get_json(manifest_key)
    prior_by_url = {}
    if isinstance(prior_manifest, dict):
        for record in prior_manifest.get("records", []):
            page_url = str(record.get("page_url") or "")
            if page_url and page_url not in prior_by_url:
                prior_by_url[page_url] = record

    groups_to_check = []
    for group in groups:
        reused = cached_result(group, prior_by_url.get(group["url"], {}), checked_at)
        if reused:
            results.append(reused)
        else:
            groups_to_check.append(group)

    with tempfile.TemporaryDirectory(prefix="sparch-event-images-") as tmp:
        asset_dir = Path(tmp)
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
            futures = {
                executor.submit(audit_page, group, pacer, timeout, asset_dir): group
                for group in groups_to_check
            }
            for future in as_completed(futures):
                results.append(future.result())

        group_by_url = {group["url"]: group for group in groups}
        records = []
        uploaded_assets: dict[str, str] = {}
        for result in sorted(results, key=lambda item: item.url):
            group = group_by_url[result.url]
            asset_key = ""
            asset_url = result.asset_url
            if result.status == "usable-candidate" and result.asset_name:
                asset_key = f"{asset_prefix}/{result.asset_name}"
                if storage and not asset_url:
                    if result.asset_name not in uploaded_assets:
                        uploaded_assets[result.asset_name] = storage.put_bytes(
                            asset_key,
                            (asset_dir / result.asset_name).read_bytes(),
                            "image/webp",
                            "public, max-age=31536000, immutable",
                        )
                    asset_url = uploaded_assets[result.asset_name]

            grouped_events = group.get("events") or [{
                "event_key": "",
                "title": result.title,
                "venue": result.venue,
                "editorial_feature": False,
            }]
            for event in grouped_events:
                records.append({
                    "event_key": event["event_key"],
                    "title": event["title"],
                    "venue": event["venue"],
                    "page_url": result.url,
                    "status": result.status,
                    "reason": result.reason,
                    "discovery": result.discovery,
                    "relevance": result.relevance,
                    "asset_kind": result.asset_kind,
                    "source_image_url": result.image_url,
                    "asset_key": asset_key,
                    "asset_url": asset_url,
                    "source_width": result.width,
                    "source_height": result.height,
                    "source_bytes": result.source_bytes,
                    "webp_bytes": result.webp_bytes,
                    "cached": result.cached,
                    "editorial_feature": bool(event["editorial_feature"]),
                    "checked_at": result.checked_at or checked_at.isoformat(timespec="seconds"),
                    "expires_at": result.expires_at or (checked_at + timedelta(days=90)).isoformat(timespec="seconds"),
                })

        selected = [record for record in records if record["editorial_feature"]]
        manifest = {
            "schema_version": 1,
            "generated_at": checked_at.isoformat(timespec="seconds"),
            "mode": "publish" if storage else "dry-run",
            "event_count": len(events),
            "unique_urls_checked": len(groups),
            "unique_urls_fetched": len(groups_to_check),
            "unique_urls_reused": len(groups) - len(groups_to_check),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "featured_selection": {
                "count": len(selected),
                "valid": len(selected) <= 4,
                "message": "" if len(selected) <= 4 else "Select no more than four Editorial Feature events",
            },
            "summary": {
                status: sum(result.status == status for result in results)
                for status in ("usable-candidate", "needs-review", "no-image", "failed")
            },
            "records": records,
        }
        if storage:
            storage.put_json(manifest_key, manifest)
        return manifest
