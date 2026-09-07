import io
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparch_event_images import (  # noqa: E402
    AuditResult,
    DomainPacer,
    audit_page,
    canonical_event_key,
    discover_candidates,
    enrich_events,
    normalise_image,
    R2Storage,
    safe_https_url,
    title_relevance,
)


class ImageEnrichmentTests(unittest.TestCase):
    def test_safe_url_rejects_local_and_non_https(self):
        self.assertTrue(safe_https_url("https://venue.example/events/show"))
        self.assertFalse(safe_https_url("http://venue.example/events/show"))
        self.assertFalse(safe_https_url("https://localhost/image.jpg"))
        self.assertFalse(safe_https_url("https://127.0.0.1/image.jpg"))

    def test_metadata_discovery_prefers_structured_event_image(self):
        html = """
        <html><head>
          <title>Wallis Bird at The Grand Social</title>
          <meta property="og:image" content="/generic.jpg">
          <script type="application/ld+json">
          {"@type":"Event","name":"Wallis Bird","image":"https://img.example/wallis.jpg"}
          </script>
        </head></html>
        """
        candidates, page_title = discover_candidates(html, "https://venue.example/wallis", "Wallis Bird")
        self.assertEqual(candidates[0], ("event-jsonld", "https://img.example/wallis.jpg"))
        self.assertIn(("og", "https://venue.example/generic.jpg"), candidates)
        self.assertGreaterEqual(title_relevance("Wallis Bird", page_title), 0.5)

    def test_title_matched_alt_fallback(self):
        html = '<img alt="Wallis Bird live" data-src="/wallis.webp">'
        candidates, _ = discover_candidates(html, "https://venue.example/show", "Wallis Bird")
        self.assertEqual(candidates, [("matching-alt", "https://venue.example/wallis.webp")])

    def test_image_is_resized_and_stripped_to_webp(self):
        source = io.BytesIO()
        Image.new("RGB", (1600, 900), "#c080b8").save(source, format="PNG")
        webp, width, height, source_format = normalise_image(source.getvalue())
        self.assertEqual((width, height, source_format), (1600, 900, "png"))
        with Image.open(io.BytesIO(webp)) as result:
            self.assertEqual(result.format, "WEBP")
            self.assertEqual(result.size, (800, 450))

    def test_upstream_image_avoids_fetching_a_blocked_event_page(self):
        group = {
            "url": "https://ticket.example/event/1",
            "event_ids": ["1"],
            "title": "Wallis Bird",
            "venue": "The Grand Social",
            "image_url": "https://images.example/wallis.jpg",
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "sparch_event_images.fetch_candidate_image",
            return_value=("https://images.example/wallis.jpg", b"webp", 1200, 675, "jpeg", 20),
        ), patch("sparch_event_images.requests.get") as page_get:
            result = audit_page(group, DomainPacer(0), 1, Path(tmp))
        self.assertEqual(result.status, "usable-candidate")
        self.assertEqual(result.discovery, "upstream")
        self.assertEqual(result.asset_kind, "event")
        page_get.assert_not_called()

    def test_event_key_is_stable_and_date_sensitive(self):
        event = {"title": "Wallis Bird", "date": "2026-09-04", "venue": "The Grand Social"}
        self.assertEqual(canonical_event_key(event), canonical_event_key(dict(event)))
        changed = dict(event, date="2026-09-05")
        self.assertNotEqual(canonical_event_key(event), canonical_event_key(changed))

    def test_manifest_flags_too_many_editorial_features_without_raising(self):
        events = [
            {"title": f"Event {index}", "date": "2026-09-04", "venue": "V",
             "url": f"https://example.com/{index}", "editorial_feature": True}
            for index in range(5)
        ]

        def fake_audit(group, _pacer, _timeout, thumbnail_dir):
            asset = f"{group['title'].replace(' ', '-')}.webp"
            (thumbnail_dir / asset).write_bytes(b"webp")
            return AuditResult(
                url=group["url"], event_ids=group["event_ids"], title=group["title"],
                venue=group["venue"], status="usable-candidate", asset_name=asset,
                asset_kind="event", discovery="test", image_url="https://images.example/a.jpg",
            )

        with patch("sparch_event_images.audit_page", side_effect=fake_audit):
            manifest = enrich_events(events, workers=1)
        self.assertFalse(manifest["featured_selection"]["valid"])
        self.assertEqual(manifest["featured_selection"]["count"], 5)

    def test_shared_url_keeps_event_metadata_and_feature_state_separate(self):
        events = [
            {
                "title": "Featured event", "date": "2026-09-11", "venue": "Venue A",
                "url": "https://example.com/whats-on", "editorial_feature": True,
            },
            {
                "title": "Regular event", "date": "2026-09-12", "venue": "Venue B",
                "url": "https://example.com/whats-on", "editorial_feature": False,
            },
        ]

        def fake_audit(group, _pacer, _timeout, thumbnail_dir):
            asset = "shared.webp"
            (thumbnail_dir / asset).write_bytes(b"webp")
            return AuditResult(
                url=group["url"], event_ids=group["event_ids"], title=group["title"],
                venue=group["venue"], status="usable-candidate", asset_name=asset,
                asset_kind="event", discovery="test", image_url="https://images.example/a.jpg",
            )

        with patch("sparch_event_images.audit_page", side_effect=fake_audit):
            manifest = enrich_events(events, workers=1)

        self.assertEqual(manifest["unique_urls_checked"], 1)
        self.assertEqual(manifest["featured_selection"]["count"], 1)
        self.assertTrue(manifest["featured_selection"]["valid"])
        self.assertEqual(
            [(record["title"], record["venue"], record["editorial_feature"])
             for record in manifest["records"]],
            [("Featured event", "Venue A", True), ("Regular event", "Venue B", False)],
        )

    def test_recent_manifest_reuses_image_without_network_fetch(self):
        event = {
            "title": "Wallis Bird", "date": "2026-09-04", "venue": "The Grand Social",
            "url": "https://example.com/wallis",
        }
        prior = {"records": [{
            "event_key": canonical_event_key(event),
            "title": event["title"], "venue": event["venue"], "page_url": event["url"],
            "status": "usable-candidate", "asset_url": "https://media.example/wallis.webp",
            "asset_key": "events/third-castle/wallis.webp", "asset_kind": "event",
            "source_image_url": "https://source.example/wallis.jpg", "discovery": "og",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "expires_at": "2026-12-01T00:00:00+00:00",
        }]}
        with patch("sparch_event_images.audit_page") as audit:
            manifest = enrich_events([event], workers=1, prior_manifest=prior)
        audit.assert_not_called()
        self.assertEqual(manifest["unique_urls_reused"], 1)
        self.assertEqual(manifest["unique_urls_fetched"], 0)
        self.assertEqual(manifest["records"][0]["asset_url"], "https://media.example/wallis.webp")

    def test_r2_adapter_refuses_uploads_beyond_run_caps(self):
        client = Mock()
        boto3 = Mock()
        boto3.client.return_value = client
        with patch.dict("sys.modules", {"boto3": boto3}):
            storage = R2Storage(
                account_id="account",
                access_key_id="key",
                secret_access_key="secret",
                bucket="bucket",
                public_base_url="https://media.example",
                max_upload_bytes=5,
                max_object_writes=1,
            )

        self.assertEqual(
            storage.put_bytes("a", b"12345", "image/webp", "public"),
            "https://media.example/a",
        )
        with self.assertRaisesRegex(RuntimeError, "upload safety limit"):
            storage.put_bytes("b", b"1", "image/webp", "public")
        self.assertEqual(client.put_object.call_count, 1)

        storage.max_upload_bytes = 10
        with self.assertRaisesRegex(RuntimeError, "write safety limit"):
            storage.put_bytes("b", b"1", "image/webp", "public")
        self.assertEqual(client.put_object.call_count, 1)


if __name__ == "__main__":
    unittest.main()
