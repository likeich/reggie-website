# tools/

Scheduled jobs that support reggie.bot.

This repo holds the published wasm bundle, which the release workflow in
`likeich/Reggie` overwrites on every tag. That workflow deliberately preserves
`.github/` and `tools/`, so anything here survives a republish. Do not move
these into the bundle directory.

**`sync_news.py`** — caches army.mil news into Supabase.

`api.army.mil` sits behind Akamai, which returns 403 to any request carrying a
browser User-Agent and separately rejects Cloud Run's egress range. The wasm
client therefore cannot fetch it, and routing through the Reggie API's `/proxy`
does not help either. GitHub runners are unaffected, so this job fetches the
leads and writes them to a Supabase table the client reads instead.

It lives in this repo rather than the private one because public repos get
unlimited Actions minutes.

The source of truth is `pythonProject/sync_news.py` in `likeich/Reggie`; keep
them in step if you change either.
