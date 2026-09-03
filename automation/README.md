# Hosted event-image lane

This lane is deliberately separate from event acquisition and page generation.
It reads `Master` and `Candidates_Web`, performs deterministic metadata image
discovery, writes small WebP derivatives to Cloudflare R2, and records provenance
in `manifests/third-castle/current.json`. It uses no AI API.

The normal `generate_events_pages.py` command dispatches this workflow in
`hosted` mode, so no extra weekly action is required and source images never
land on the operator's laptop. A failed run leaves the preceding manifest in
place and cannot prevent event generation.

## One-time account setup

1. Create a **private**, Standard-class R2 bucket named
   `third-castle-event-images` in the Cloudflare account that manages
   `thirdcastle.ie`. Do not enable `r2.dev` or a public bucket domain.
2. Deploy `worker/src/index.mjs` on the Workers Free plan, bind its
   `EVENT_IMAGES` variable to that bucket, and connect the Worker custom domain
   `media.events.thirdcastle.ie`. Keep the route fail-closed. The Free plan's
   100,000-request daily ceiling then fails with an error instead of allowing
   public traffic to create unbounded R2 reads.
3. Create a bucket-scoped R2 token with **Object Read & Write** only. The
   permanent lifecycle rule is configured once in the dashboard, so the weekly
   process never receives bucket-administration or deletion rights.
4. Add these GitHub Actions repository secrets:
   `GOOGLE_SERVICE_ACCOUNT_JSON`, `THIRD_CASTLE_SHEET_ID`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, and `R2_PUBLIC_BASE_URL`.
5. Run `Refresh event images` once manually and verify its diagnostic artifact
   and the R2 manifest before enabling hosted dispatch by default.

The dashboard-managed `events/` 90-day lifecycle rule retains manifests while
derived event images expire. Successful records are reused for 30 days and
unsuccessful discovery attempts for 7 days.

The runner refuses more than 500 events, 600 object writes, or 250 MB of uploads
in one run, and GitHub cancels concurrent duplicates after 15 minutes. The
gateway strips query strings from its cache key, rejects unknown object paths
before touching R2, and permits only one R2 operation per accepted request.

`venue-images.json` is intentionally empty at launch. Add only approved venue
fallbacks as `{ "Venue name": "https://..." }`; if neither an event image nor
an approved venue image is available, the UI renders no preview.
