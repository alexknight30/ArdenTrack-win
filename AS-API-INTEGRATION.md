# AS-API integration (Render classify service)

ArdenTrack’s classify loop can send an optional shared secret header so only your clients can call **`POST /api/classify`**. This is **not** user login or Supabase—it is a single environment variable compared per request (stateless).

## Do you need to change AS-API?

| Goal | Action |
|------|--------|
| **Leave classify open** (anyone with the URL can POST) | **No code changes.** Do **not** set `ARDEN_AS_API_SECRET` on Render. Do **not** set `ARDEN_AS_API_SECRET` in ArdenTrack’s environment. |
| **Lock the endpoint** (recommended for production) | Ensure AS-API implements the check below (may already be present), then set the **same** secret on Render and on ArdenTrack. |

## Code change (only if missing in your AS-API repo)

Open the Flask handler for **`/api/classify`** and add this block **immediately after** `try:` and **before** reading `request.get_json()`:

```python
expected = os.getenv("ARDEN_AS_API_SECRET", "").strip()
if expected:
    got = (request.headers.get("X-Arden-Secret") or "").strip()
    if got != expected:
        return jsonify({"error": "Unauthorized"}), 401
```

Behavior:

- If **`ARDEN_AS_API_SECRET`** is **unset or empty** on the server → this block does nothing; behavior matches a fully open endpoint.
- If it **is** set → requests must include header **`X-Arden-Secret`** with the exact same string or they get **401**.

Reference copy on this machine (already patched): `Desktop/AS-API/app.py` at the top of `classify()`.

## Deploy (Render)

1. If locking the API: in the Render service dashboard, add environment variable **`ARDEN_AS_API_SECRET`** (use a long random string).
2. Redeploy AS-API after changing env vars.

## ArdenTrack client (this repo)

[`ardentrack/classifier/classify.py`](ardentrack/classifier/classify.py) sends **`X-Arden-Secret`** only when **`ARDEN_AS_API_SECRET`** is set in the process environment (same value as Render).

For PyInstaller / Electron builds, inject that env at build or runtime so it matches Render.

## Checklist for a Cursor session in the AS-API repo

1. Confirm whether `classify()` already contains the `ARDEN_AS_API_SECRET` / `X-Arden-Secret` block.
2. If not, add it as above; run tests / manual `curl` with and without the header.
3. Decide open vs locked; if locked, set `ARDEN_AS_API_SECRET` on Render and document the value for whoever configures ArdenTrack builds (do not commit secrets).

## Quick curl test (after locking)

```bash
# Should return 401 without header (when secret is set on server)
curl -X POST https://as-api.onrender.com/api/classify -H "Content-Type: application/json" -d "{}"

# Should proceed past auth (may fail later for other reasons)
curl -X POST https://as-api.onrender.com/api/classify \
  -H "Content-Type: application/json" \
  -H "X-Arden-Secret: YOUR_SECRET" \
  -d '{"events":[],"matters":{},"clients":{}}'
```
