# SoundCloud crowd discovery prototype

A local, dependency-free experiment in recommending SoundCloud tracks from
*what other people file next to your music* instead of from SoundCloud's own
recommendation engine. It exists to answer "is this signal useful?" before any
of it is built into the browser.

```
python3 scdiscover.py https://soundcloud.com/oklou93/just-level-5-cause-its-kinda-cool
python3 scdiscover.py https://soundcloud.com/someone/sets/some-playlist --holdout 0.2
python3 scdiscover.py <url> --json --limit 50 > out.json
python3 scdiscover.py --help
```

Python 3.9+ only. Responses are cached in `.cache/` next to the script, so
re-running with different scoring flags is free; delete the directory or pass
`--no-cache` to refetch. The API client ID is scraped from SoundCloud's web
bundles on first run and refreshed automatically when it stops working.

## How it works

1. **Resolve** the URL to a track (one seed) or a playlist (all its tracks are
   seeds; playlists over `--max-seeds` are sampled).
2. **Expand** through the internal `api-v2` endpoints the web player uses:
   - `tracks/{id}/playlists_without_albums` – every public set the seed is in.
     Sets outside `--min/--max-playlist-size` are skipped and the remainder are
     ordered by closeness to ~80 tracks, which is where hand-curated sets live.
   - `playlists/{id}` + `tracks?ids=` – a set returns 5 full tracks and the
     rest as id stubs; stubs are hydrated 50 per call, so a 500-track set costs
     11 requests. This is how the "hundreds of hidden tracks" become visible.
   - `tracks/{id}/likers` → `users/{id}/likes` (optional, `--likers-per-seed`)
     – a heavy liker's likes list is treated as a pseudo-playlist.
   - `tracks/{id}/related` – SoundCloud's own recommendations, used only as
     the baseline column.
3. **Score** every non-seed track:
   - Each list gets weight `seed_hits^1.5 / ln(size + 10)`. A set containing
     four of your seeds is worth far more than four sets containing one each.
   - Lists containing more than `--max-seed-fraction` of the seeds are copies
     of the input playlist and are dropped. Lists that overlap each other by
     `--dedupe-jaccard` or more are merged into one: SoundCloud playlists get
     copied wholesale, and one run found the same "Synthwave | Nightdrive
     Vibes" set under six accounts, which had pushed its 1k-play filler to the
     top of the up-and-coming list.
   - `raw` = sum of list weights → **Crowd picks**.
   - `upcoming` = `raw / plays^0.5 / followers^0.25 * recency` → **Up and
     coming**, additionally restricted to tracks that appear in lists from at
     least two *different people*, have at least 1k plays, and sit in the
     less-played / smaller-artist half of the candidate pool. For playlists of
     10+ tracks a pick must also share one list with at least two seeds
     (`--min-seed-hits`), which keeps single-seed off-genre lists out.
   - Tracks by the seed artists are excluded and each artist is capped at two
     rows (`--include-seed-artists`, `--per-artist`).
4. **Evaluate** (playlists only, `--holdout`): hide a fraction of the playlist,
   recommend from the rest, report how many hidden tracks each list recovers.

## What the first runs showed

Seed: Oklou, "just level 5 cause its cute" (single track, ~40 lists, ~90
calls). SoundCloud's related list is mostly Oklou's own tracks and remixes.
Crowd picks surface the scene around her (Eartheater, underscores, SOPHIE,
Sega Bodega, Frost Children). Up and coming surfaces sub-10k-play tracks that
two or more unrelated curators filed next to the seed, which is the discovery
signal the platform does not expose.

Playlist holdout on an eclectic 49-track set (9 tracks hidden): once copied
playlists were removed, no method placed a hidden track in its top 25. With a
3x budget (120 sets, ~6,300 candidates) crowd picks recovered 2 of 9 somewhere
in the ranking and SoundCloud's related list 0 of 9. A mixed-genre playlist is
the hardest case, since its neighbours split into unrelated clusters; run
`--holdout` on a few coherent, personally curated playlists before drawing
conclusions.

## Known limits

- **Unofficial API.** `api-v2` and the scraped client ID are what the web
  client uses; the ID rotates every few weeks. Inside the browser this goes
  away: a soundcloud.com page script already has the ID and the user's
  session.
- **Rate limits are unknown.** Runs of ~200 calls at 0.15s spacing were fine.
  Budget is controlled by `--max-playlists`, `--playlists-per-seed`,
  `--max-seeds`; each hydrated set costs `1 + size/50` calls.
- **Popularity bias vs. noise.** Raw co-occurrence favours anthems; the
  normalised list can favour junk. The distinct-people requirement and the
  play floor are what keep it honest; tune `--popularity-power`,
  `--artist-power`, `--min-contexts`.
- **Dates lie.** `created_at` is the upload time; reuploads of old tracks
  look brand new. The tool uses the earliest of `display_date`,
  `release_date` and `created_at`, and drops tracks with more likes than half
  their plays, a bought-engagement signature seen on one such reupload.
- **No audio or genre features.** Everything is graph structure. Genre tags
  are carried in the JSON output for a later filter.

## Path to the browser

The scoring is a few hundred lines of arithmetic over JSON the page can fetch
itself. The natural home is the existing soundcloud.com page script on iOS
(`SoundCloudScript.js`), which already reaches into the site's player; it
could seed from the playlist being viewed, run the expansion with the page's
own credentials, and offer the up-and-coming list as a "Discover from this
playlist" action. Evaluate with `--holdout` on real user playlists first.
