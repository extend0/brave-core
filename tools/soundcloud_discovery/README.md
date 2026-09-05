# SoundCloud discovery prototype

A local, dependency-free experiment in recommending SoundCloud tracks that are
good but not chart-topping: the mid-band artists you would otherwise only find
by browsing. It exists to answer "is this signal useful?" before any of it is
built into the browser.

```
python3 scdiscover.py https://soundcloud.com/someone/some-track
python3 scdiscover.py <playlist-url> --kind remix
python3 scdiscover.py <url> --negative <playlist-you-dislike>
python3 scdiscover.py <playlist-url> --fit taste.json     # learn your bands
python3 scdiscover.py <url> --profile taste.json          # use them
python3 scdiscover.py <playlist-url> --eval               # measure recall
python3 scdiscover.py --help
```

Python 3.9+ only. Responses are cached in `.cache/` next to the script, so
re-running with different scoring flags is free; delete the directory or pass
`--no-cache` to refetch. The API client ID is scraped from SoundCloud's web
bundles on first run and refreshed automatically when it stops working.

A typical run is **~500 requests in ~11 seconds**.

## How it works

Three independent retrieval channels, then one ranking pass. Candidates that
several channels agree on win, because channel agreement turned out to be the
single best predictor of a hand-labelled positive set.

1. **Resolve** the URL to a track (one seed) or a playlist (all its tracks are
   seeds, sampled down to `--max-seeds`).

2. **Retrieve** through the internal `api-v2` endpoints the web player uses:

   - `tracks/{id}/related` — SoundCloud's own related tracks, one call per
     seed. The highest-precision channel by a wide margin: it held **83% of
     the labelled positives while making up 5% of the candidate pool**. The
     earlier version of this tool fetched it only to print as a baseline
     column to beat, and threw the candidates away.
   - `tracks/{id}/playlists_without_albums` → `playlists/{id}` — the public
     sets a seed sits in, fetched **as track ids only**. `playlists/{id}`
     returns every member as at least an `{id}` stub, so a 500-track set costs
     one request rather than eleven. Sets are filtered by size, ordered by
     closeness to `--playlist-sweet-spot`, and near-duplicates are collapsed
     (`--dedupe-jaccard`) because SoundCloud playlists get copied wholesale and
     six copies of one set must count as one person's opinion.
   - `users/{id}/relatedartists` → `users/{id}/tracks` — SoundCloud's own
     artist-similarity graph, walked `--hops` hops with `--fan` neighbours
     each, keeping only artists inside the follower band. One call returns ten
     genuinely adjacent mid-band artists; this is the cheapest way into a
     scene.

3. **Filter** to a popularity envelope. These are hard bands, not soft
   penalties — a soft penalty lets a 3.5M-follower artist through whenever
   co-occurrence is strong enough, which is exactly how the previous version
   ended up recommending Bad Bunny. Also dropped: uploads that are not single
   tracks (edit packs, drum kits, podcasts, DJ sets), tracks outside the
   duration window, re-upload/aggregator accounts (huge catalogue, few
   followers), and any track with a seed artist's name in its title — a
   re-upload of the seed artist is not a discovery, whoever posted it.

4. **Rank** by a weighted sum of z-scored features, so a score means "unusual
   for this candidate pool, in the directions that predicted the labelled
   set". `--min-channels 2` requires agreement from two channels, which drops
   the pool ~12x at no measured recall cost. `--per-artist` caps rows per
   artist so one prolific curator cannot own the list.

5. **Subtract what you rejected** (`--negative URL`, opt-in). The same
   retrieval runs over tracks you did not want and their evidence is
   subtracted from every candidate (Rocchio relevance feedback,
   `--negative-weight`). This only bites on the **shared-audience** channel
   (`--likers-per-seed`, off by default), which is keyed on people rather
   than on artists or lists. Subtracting the artist graph measurably *hurts*,
   because it removes an artist rather than an audience — a rejected track
   and a kept track by the same editor are tightly linked in that graph. Note
   what survives: a rejected track's author can still be recommended for a
   different track, which is usually what you want. See
   [Negative feedback](#negative-feedback) for the measurements and why it is
   not on by default.

6. **Fit** (`--fit OUT`) treats a playlist of tracks you like as labelled
   positives: it derives the envelope from their own spread (widened by
   `--fit-margin`), then runs leave-one-out to measure how well each feature
   separates them from the pool, and writes both to a JSON profile.

7. **Evaluate** (`--eval`) hides each playlist member in turn, recommends from
   the rest, and reports the rank the hidden track comes back at.

## What the measurements showed

Fitted against an 8-track hand-labelled playlist (18,414 viable candidates
across 8 leave-one-out folds). `AUC` is the probability a labelled track
outranks a random pool member on that feature alone:

| feature | AUC | labelled | pool | |
|---|---|---|---|---|
| `nch` channels agreeing | 0.966 | 2.50 | 1.11 | strongest signal |
| `plw` playlist co-occurrence weight | 0.959 | 0.88 | 0.28 | |
| `screl` in SoundCloud's related | 0.892 | 0.83 | 0.05 | |
| `er` like rate | 0.815 | 7.1% | 5.2% | |
| `artw` artist-graph weight | 0.751 | 0.45 | 0.13 | |
| `ntags` tag count | 0.733 | 9.2 | 5.2 | proxy for upload care |
| `lpf` plays per follower | 0.708 | 2.59 | 1.80 | overperforms its audience |
| `lfol` log followers | 0.282 | ~1.5k | ~3.2k | inverted: smaller wins |
| `lplays` raw play count | 0.509 | — | — | **no signal** |
| `tov` seed/candidate tag overlap | 0.433 | 0.05 | 0.11 | **anti-correlated** |

A second, graded round of labels (4 keeps / 19 rejects, drawn from this
tool's own output) added two findings.

The **duration floor** rose from 135s to 155s. All four keeps in that round
were 205s or longer, which argued for a 200s floor, and taken alone it looked
clean: 4/4 keeps retained, 7/19 rejects cut. It was wrong. Half the earlier
8-track labelled set is 159–180s, so a 200s floor discards four known
positives and dropped `--eval` from 6/8 to **2/8**. Short does not mean bad;
only very short does. 155s is the highest floor that costs no known positive
(0/12 lost, 4/19 rejects cut) and it improved every recovered rank
(`recall@50` 5/8 → 6/8). Fitting a threshold on the newest labels alone is
how you lose the older ones.

`waveform_url`, which exposes 1,800 amplitude samples per track, turned out
to be **useless**: loudness, dynamic range, crest factor, quiet fraction,
intro length and percussive flux all put the keepers' range entirely inside
the rejects' range. It is a smoothed visual envelope, not audio, and there is
no BPM field. That idea is not in the tool.

Two of the table's rows overturned earlier design choices. Raw play count carries no
signal at all once inside the band, so the old `plays^0.5` normalisation was
doing nothing. Tag *overlap* is actively harmful — generic tags like "edit"
and "club" match everything — while the *number* of tags is a decent quality
proxy. Both are reflected in the shipped weights; neither is a filter.

The labelled set was also far narrower than the defaults it replaced: 674–5,316
followers, where the previous version allowed up to 200,000.

Recall on that set, ranking ~180 candidates per fold: **6/8 recovered, all
6 inside the top 50**, versus a 2% rate for chance (ranks 3, 17, 24, 27, 32,
43).

## Negative feedback

Rejections are cheap to produce, so `--negative` looked like the best
available lever. Measured on the graded round — can the ranker put the 4
keeps above the 19 rejects, leave-one-out, 76 pairwise comparisons? — it
mostly is not:

| configuration | AUC |
|---|---|
| default channels, no negatives | **0.721** |
| default channels, subtracting playlist or sc-related evidence | 0.721 (flat) |
| default channels, subtracting the **artist graph** | 0.691 → 0.676 (worse) |
| **+ shared-audience channel**, no negatives | 0.691 |
| **+ shared-audience channel**, subtracting it (λ=2–3) | **0.750** |

So subtracting an audience does move the ranking the right way,
monotonically in λ. But the net gain over simply not doing any of it is
0.721 → 0.750, bought with roughly double the requests and triple the
candidate pool. With 4 positives one swapped pair is worth 0.013 AUC, so
+0.029 is about two pairs — inside the noise. Both the channel and the
subtraction are therefore **off by default** and kept as an opt-in
(`--likers-per-seed 60 --negative <url>`) pending a larger labelled set.

Two process notes, since both cost real time:

- An earlier prototype of this measured 0.697 → 0.855 and that number did
  **not** replicate in the tool. The prototype had no popularity envelope, no
  per-artist cap and no z-scoring, and it scored every candidate. Its channel
  mix was also different (likers, not the artist graph). A number measured
  outside the pipeline it is meant to justify should not be trusted.
- The first implementation folded the subtraction in as a z-scored feature,
  which silently made `--negative-weight` a **no-op**: z-scoring divides out
  any scale applied to its input, so every λ above 0 gave identical results.
  It is now subtracted after z-scoring. The tell was a sweep whose columns
  were all identical.

## Known limits

- **Two of eight labelled tracks are unreachable.** They are recovered by no
  channel — not related-artists, not playlists, not likers, not tag search.
  One sits at 4k plays with no graph edges to the rest of the set. A
  co-occurrence system structurally cannot find those, and no amount of
  tuning changes it.
- **The weights are coarse.** They are signed AUC separations from six
  recoverable positives, not a fitted regression: the feature *ordering* is
  robust, the exact values are not. A labelled set of 30–50 tracks would
  support fitting them properly.
- **The graded round is 4 positives.** The duration floor and the direction
  of the audience effect are the only things it can really support; it cannot
  fit weights. More *keeps* are the binding constraint — 19 rejects is
  already plenty.
- **Every feature is social; taste is musical.** The features describe who
  liked a track, who filed it, and how big its artist is. None describe how
  it sounds. The clearest evidence is a pair from the graded round: one
  Kendrick Lamar club edit was a keeper and another was a reject — both
  ~250s, both by mid-band editors — and the top keeper's own editor also made
  a rejected track. No reachable metadata separates those, so the residual
  error is largely within-artist song selection. Closing it would need real
  audio decoding, which costs the dependency-free property and does not
  transfer to the browser.
- **Unofficial API.** `api-v2` and the scraped client ID are what the web
  client uses; the ID rotates every few weeks. Inside the browser this goes
  away: a soundcloud.com page script already has the ID and the user's
  session. `relatedartists` is as unofficial as the rest, which is why the
  playlist channel is worth keeping as a fallback.
- **Rate limits.** None observed: 2.3 req/s serial against 43 req/s at 32
  threads with zero 429s. `--workers` defaults to a conservative 24.
- **Reupload-heavy genres stay hard.** Where a scene is dominated by
  aggregator accounts, the filters help but do not win outright.
- **Dates and audio.** Nothing here uses release dates (`created_at` is upload
  time, so reuploads look new) or audio features. It is all graph structure
  plus the popularity envelope.

## Path to the browser

The ranking is a few hundred lines of arithmetic over JSON the page can fetch
itself. The natural home is the existing soundcloud.com page script on iOS
(`SoundCloudScript.js`), which already reaches into the site's player; it could
seed from the playlist being viewed, run the retrieval with the page's own
credentials, and offer the result as a "Discover from this playlist" action.
The user's own likes are the obvious labelled set to fit against.
