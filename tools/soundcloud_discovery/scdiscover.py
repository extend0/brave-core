#!/usr/bin/env python3
# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Local prototype of a SoundCloud discovery algorithm.

Given a track or playlist URL, retrieves candidates through three independent
channels and ranks them by how many channels agree, which is the signal that
best predicted a hand-labelled positive set (AUC 0.97 over 18k candidates):

  1. tracks/{id}/related   - SoundCloud's own related tracks. Highest precision
     per call by a wide margin: it held 83% of the labelled positives while
     making up 5% of the candidate pool.
  2. playlist co-occurrence - the public sets a seed sits in, fetched as track
     ids only (one call per set, even for a 500-track set).
  3. users/{id}/relatedartists - SoundCloud's artist-similarity graph, walked
     two hops, then each in-band artist's own tracks.

Candidates are filtered to a fitted popularity envelope (follower band, play
band, like-rate floor, track-length window) and scored by a weighted sum of
z-scored features. Run --fit against a playlist of tracks you like to
re-derive that envelope and re-measure the feature weights for your own taste.

No dependencies beyond the Python standard library (curl is used as a
transport fallback). Responses are cached under .cache/ next to this file.
"""

import argparse
import collections
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api-v2.soundcloud.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
CLIENT_ID_FILE = os.path.join(CACHE_DIR, "client_id")
HYDRATE_BATCH = 50

# Uploads that are not single tracks. Edit packs and drum kits are the two that
# actually showed up in early runs; the rest are cheap insurance.
NOISE_RE = re.compile(
    r'\b(pack|kit|mixtape|podcast|episode|full album|megamix|guest ?mix'
    r'|liveset|dj ?set|voice ?notes?|snippet|preview|teaser|interview'
    r'|radio ?show|tracklist)\b', re.I)
EDIT_RE = re.compile(
    r'\b(remix|rmx|edit|bootleg|flip|vip|mashup|blend|refix|rework|version'
    r'|dub|re-?drum)\b', re.I)


def log(msg):
  print(msg, file=sys.stderr, flush=True)


def norm(s):
  return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def norm_title(s):
  """Title with bracketed suffixes dropped, for collapsing re-uploads."""
  return norm(re.sub(r'\(.*?\)|\[.*?\]', '', s or ''))[:34]


def tags_of(track):
  """tag_list is space separated with quoted multi-word tags."""
  raw = track.get("tag_list") or ""
  pairs = re.findall(r'"([^"]+)"|(\S+)', raw)
  tags = {(a or b).lower().strip() for a, b in pairs}
  genre = (track.get("genre") or "").lower().strip()
  if genre:
    tags.add(genre)
  return {t for t in tags if len(t) > 2}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Client:
  """Parallel, on-disk-cached api-v2 client.

  SoundCloud did not rate-limit any run during development: measured 2.3 req/s
  serial against 43 req/s at 32 threads, with zero 429s. The old serial client
  spent ~18x its wall clock waiting.
  """

  def __init__(self, use_cache=True, workers=24):
    self.use_cache = use_cache
    self.calls = 0
    self.cache_hits = 0
    self._lock = threading.Lock()
    self.pool = ThreadPoolExecutor(max_workers=workers)
    os.makedirs(CACHE_DIR, exist_ok=True)
    self.client_id = self._load_client_id()

  # -- raw fetch --------------------------------------------------------------

  def _fetch_text(self, url):
    """Fetch a URL. urllib first; fall back to curl (some proxies break
    chunked/gzip bodies for urllib)."""
    try:
      req = urllib.request.Request(
          url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
      with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
      return e.code, e.read().decode("utf-8", "replace")
    except Exception:  # pylint: disable=broad-except
      out = subprocess.run(
          ["curl", "-sL", "--compressed", "-A", UA, "-w",
           "\n%{http_code}", url],
          capture_output=True, text=True, timeout=60)
      body, _, code = out.stdout.rpartition("\n")
      return int(code or 0), body

  def _cache_path(self, key):
    digest = hashlib.sha1(key.encode()).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".json")

  # -- client id --------------------------------------------------------------

  def _load_client_id(self):
    if os.path.exists(CLIENT_ID_FILE):
      with open(CLIENT_ID_FILE) as f:
        cid = f.read().strip()
      if cid:
        return cid
    return self._discover_client_id()

  def _discover_client_id(self):
    """Scrape the web client's bundles for its client_id, like the browser
    itself would have it in hand."""
    log("discovering client_id from soundcloud.com ...")
    _, html = self._fetch_text("https://soundcloud.com/")
    scripts = re.findall(
        r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
    for src in reversed(scripts):
      _, js = self._fetch_text(src)
      m = re.search(r'client_id:"([A-Za-z0-9]{32})"', js)
      if m:
        with open(CLIENT_ID_FILE, "w") as f:
          f.write(m.group(1))
        return m.group(1)
    sys.exit("could not find a client_id in SoundCloud's web bundles")

  # -- api --------------------------------------------------------------------

  def fetch(self, url):
    """GET a fully-formed api-v2 URL, with caching and retries."""
    path = self._cache_path(url)
    if self.use_cache and os.path.exists(path):
      with self._lock:
        self.cache_hits += 1
      try:
        with open(path) as f:
          return json.load(f)
      except ValueError:
        pass  # truncated cache entry; refetch

    data = None
    for attempt in range(4):
      with self._lock:
        self.calls += 1
        cid = self.client_id
      full = url + ("&" if "?" in url else "?") + "client_id=" + cid
      code, body = self._fetch_text(full)
      if code == 200:
        try:
          data = json.loads(body)
        except ValueError:
          data = None
        break
      if code in (401, 403) and attempt == 0:
        with self._lock:
          if cid == self.client_id:  # first thread to notice refreshes it
            log("client_id rejected; refreshing")
            self.client_id = self._discover_client_id()
        continue
      if code == 404:
        break
      if code == 429 or code >= 500:
        time.sleep(2 ** attempt)
        continue
      log(f"HTTP {code} for {url}")
      break

    if self.use_cache and data is not None:
      with open(path, "w") as f:
        json.dump(data, f)
    return data

  def get(self, path, **params):
    params = {k: v for k, v in params.items() if v is not None}
    q = urllib.parse.urlencode(params)
    return self.fetch(API + path + ("?" + q if q else ""))

  def collection(self, path, pages=1, **params):
    """Follow linked_partitioning up to `pages` pages."""
    out = []
    data = self.get(path, linked_partitioning=1, **params)
    for _ in range(pages):
      if not data:
        break
      out.extend(data.get("collection", []))
      nxt = data.get("next_href")
      if not nxt:
        break
      data = self.fetch(nxt)
    return out

  def pmap(self, fn, items):
    return list(self.pool.map(fn, list(items)))

  def resolve(self, url):
    return self.get("/resolve", url=url)

  def hydrate_tracks(self, ids):
    """Full track objects for a list of ids, 50 per call, in parallel."""
    ids = list(ids)
    batches = [ids[i:i + HYDRATE_BATCH]
               for i in range(0, len(ids), HYDRATE_BATCH)]
    out = []
    for got in self.pmap(
        lambda b: self.get("/tracks", ids=",".join(str(x) for x in b)) or [],
        batches):
      out.extend(got)
    return out

  def playlist_ids(self, playlist_id):
    """Track ids in a playlist, in one call.

    /playlists/{id} returns every member as at least an {id} stub, so a
    500-track set costs one request. Only the ~200 candidates that survive
    ranking are ever hydrated into full track objects.
    """
    pl = self.get(f"/playlists/{playlist_id}")
    if not pl:
      return None, set()
    ids = {t["id"] for t in pl.get("tracks", [])
           if isinstance(t, dict) and t.get("id")}
    return pl, ids


# ---------------------------------------------------------------------------
# Taste profile
# ---------------------------------------------------------------------------

# Feature weights are the signed AUC separations measured over an 8-fold
# leave-one-out run against a hand-labelled playlist (18,414 viable candidates,
# 6 recoverable positives). They encode a robust *ordering* of features, not a
# fitted regression; re-derive them for your own taste with --fit.
#
#   nch    number of channels that retrieved the candidate  (AUC 0.966)
#   plw    playlist co-occurrence weight                    (AUC 0.959)
#   screl  present in SoundCloud's own related list          (AUC 0.892)
#   er     like rate                                        (AUC 0.815)
#   artw   artist-graph weight, decayed by hop distance      (AUC 0.751)
#   ntags  number of tags: a proxy for upload care          (AUC 0.733)
#   lfol   log followers, negative - smaller artists win     (AUC 0.282)
#   lpf    log plays per follower: overperforms its audience (AUC 0.708)
#
# Deliberately absent: raw play count (AUC 0.509, no signal inside the band)
# and seed/candidate tag overlap (AUC 0.433, anti-correlated - generic tags
# like "edit" and "club" match everything).
DEFAULT_PROFILE = {
    "follower_band": [300, 20000],
    "play_band": [2500, 250000],
    "min_like_rate": 0.030,
    "max_like_rate": 0.50,
    # 155s is the highest floor that costs no known positive. A graded round
    # of labels suggested 200s (all 4 of its keeps were >=205s), but that
    # contradicted the 8 earlier ones, half of which are 159-180s, and dropped
    # --eval from 6/8 to 2/8. Short does not mean bad; only very short does.
    # At 155s: 0 of 12 known positives lost, 4 of 19 known rejects cut.
    "duration_band": [155, 345],
    "weights": {
        "nch": 0.47, "plw": 0.46, "screl": 0.39, "er": 0.32,
        "artw": 0.25, "ntags": 0.23, "lfol": -0.22, "lpf": 0.21,
        "lkw": 0.25,
    },
}


def load_profile(path):
  if not path:
    return dict(DEFAULT_PROFILE)
  with open(path) as f:
    p = json.load(f)
  merged = dict(DEFAULT_PROFILE)
  merged.update(p)
  return merged


# ---------------------------------------------------------------------------
# Candidate retrieval
# ---------------------------------------------------------------------------


class Seed:
  """A track we are recommending from."""

  def __init__(self, t):
    u = t.get("user") or {}
    self.id = t["id"]
    self.uid = u.get("id")
    self.artist = u.get("username") or "?"
    self.title = t.get("title") or "?"
    self.tags = tags_of(t)


class Retriever:

  def __init__(self, client, args, profile):
    self.c = client
    self.a = args
    self.p = profile
    self.meta = {}     # track id -> full track object, when we have one
    self.n_lists = 0
    self.n_artists = 0

  def _add(self, cand, tid, channel, weight, obj=None):
    slot = cand[tid]
    if weight > slot.get(channel, 0):
      slot[channel] = weight
    if obj is not None and isinstance(obj, dict) and obj.get("title"):
      self.meta.setdefault(tid, obj)

  def retrieve(self, seeds):
    a = self.a
    cand = collections.defaultdict(dict)
    seed_ids = {s.id for s in seeds}
    seed_uids = sorted({s.uid for s in seeds if s.uid})

    # -- channel 1: SoundCloud's own related tracks (1 call per seed) ---------
    if a.related:
      for got in self.c.pmap(
          lambda t: self.c.collection(f"/tracks/{t}/related", limit=50),
          sorted(seed_ids)):
        for t in got:
          if isinstance(t, dict) and t.get("id"):
            self._add(cand, t["id"], "screl", 1.0, t)
      log(f"related: {len(cand)} candidates")

    # -- channel 2: playlist co-occurrence, ids only -------------------------
    if a.playlists:
      found = {}
      for got in self.c.pmap(
          lambda t: self.c.collection(
              f"/tracks/{t}/playlists_without_albums", pages=a.playlist_pages,
              limit=50, representation="mini"),
          sorted(seed_ids)):
        for pl in got:
          n = pl.get("track_count") or 0
          if a.min_playlist_size <= n <= a.max_playlist_size:
            found[pl["id"]] = pl
      # Prefer mid-sized, hand-curated sets over 15-track stubs and 400-track
      # dumps: sort by closeness to the sweet spot and take the budget.
      sweet = math.log(a.playlist_sweet_spot)
      chosen = sorted(
          found.values(),
          key=lambda p: abs(math.log(max(p.get("track_count"), 1)) - sweet))
      chosen = chosen[:a.max_playlists]

      sets = []
      for res in self.c.pmap(lambda p: self.c.playlist_ids(p["id"]), chosen):
        _, ids = res
        if len(ids) >= a.min_playlist_size:
          sets.append(ids)
      # SoundCloud playlists get copied wholesale, so near-identical sets must
      # count as one opinion rather than six.
      unique = []
      for s in sets:
        if any(len(s & o) / max(len(s | o), 1) > a.dedupe_jaccard
               for o in unique):
          continue
        unique.append(s)
      self.n_lists = len(unique)

      for s in unique:
        hits = len(s & seed_ids)
        if not hits:
          continue
        w = min((hits ** a.seed_hits_power) / math.log(len(s) + 10), 1.5)
        for tid in s - seed_ids:
          self._add(cand, tid, "pl", w)
      log(f"playlists: {len(chosen)} fetched, {self.n_lists} distinct; "
          f"{len(cand)} candidates")

    # -- channel 4: shared audience -------------------------------------------
    # Who likes the seed, and what else do those people like. This is the only
    # channel keyed on *people* rather than on artists or lists, which is why
    # it is the one that makes --negative work: subtracting it removes an
    # audience, where subtracting the artist graph would remove an artist.
    if a.likers_per_seed > 0:
      voters = collections.defaultdict(set)
      for tid, users in zip(sorted(seed_ids), self.c.pmap(
          lambda t: self.c.collection(f"/tracks/{t}/likers", limit=200),
          sorted(seed_ids))):
        # Skip hoarders and bots: an indiscriminate liker says little.
        picky = [u for u in users
                 if a.liker_min_likes <= (u.get("likes_count") or 0)
                 <= a.liker_max_likes]
        picky.sort(key=lambda u: -(u.get("likes_count") or 0))
        for u in picky[:a.likers_per_seed]:
          voters[u["id"]].add(tid)
      uids = sorted(voters, key=lambda u: -len(voters[u]))
      for uid, items in zip(uids, self.c.pmap(
          lambda u: self.c.collection(f"/users/{u}/likes", limit=100), uids)):
        # Someone who liked several seeds is worth more than someone who
        # liked one.
        w = a.liker_weight * (len(voters[uid]) ** a.seed_hits_power)
        for x in items:
          t = x.get("track") if isinstance(x, dict) else None
          if t and t.get("id") and t["id"] not in seed_ids:
            self._add(cand, t["id"], "lk", w, t)
      log(f"audience: {len(voters)} shared likers; {len(cand)} candidates")

    # -- channel 3: artist-similarity graph ----------------------------------
    if a.hops > 0:
      dist = {u: 0 for u in seed_uids}
      frontier = list(seed_uids)
      artists = {}
      for hop in range(1, a.hops + 1):
        for rel in self.c.pmap(
            lambda u: self.c.collection(
                f"/users/{u}/relatedartists", limit=a.fan),
            frontier):
          for u in rel:
            if u["id"] in dist:
              continue
            dist[u["id"]] = hop
            artists[u["id"]] = u
        frontier = [uid for uid, d in dist.items() if d == hop]
      self.n_artists = len(artists)

      lo, hi = self.p["follower_band"]
      in_band = [uid for uid, u in artists.items()
                 if lo <= (u.get("followers_count") or 0) <= hi]
      for uid, tracks in zip(in_band, self.c.pmap(
          lambda u: self.c.collection(
              f"/users/{u}/tracks", limit=a.artist_tracks),
          in_band)):
        w = a.artist_weight * (a.hop_decay ** (dist[uid] - 1))
        for t in tracks:
          if isinstance(t, dict) and t.get("id"):
            self._add(cand, t["id"], "art", w, t)
      log(f"artists: {self.n_artists} related, {len(in_band)} in band; "
          f"{len(cand)} candidates")

    return cand


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def features(track, user, channels):
  plays = track.get("playback_count") or 0
  likes = track.get("likes_count") or 0
  followers = max(user.get("followers_count") or 0, 1)
  return {
      "nch": float(len(channels)),
      "plw": channels.get("pl", 0.0),
      "screl": 1.0 if "screl" in channels else 0.0,
      "er": likes / max(plays, 1),
      "artw": channels.get("art", 0.0),
      "lkw": channels.get("lk", 0.0),
      "ntags": float(len(tags_of(track))),
      "lfol": math.log(followers),
      "lpf": math.log(max(plays, 1) / followers),
  }


class Ranker:

  def __init__(self, args, profile):
    self.a = args
    self.p = profile
    self.w = profile["weights"]

  def viable(self, track, user, seed_names):
    """Popularity envelope. These are hard bands, not soft penalties: a soft
    penalty lets a 3.5M-follower artist through whenever co-occurrence is
    strong enough, which is exactly the failure the labelled set exposed."""
    p = self.p
    title = track.get("title") or ""
    plays = track.get("playback_count") or 0
    likes = track.get("likes_count") or 0
    followers = user.get("followers_count") or 0
    seconds = (track.get("duration") or 0) / 1000

    if NOISE_RE.search(title):
      return False
    if not p["duration_band"][0] <= seconds <= p["duration_band"][1]:
      return False
    if not p["follower_band"][0] <= followers <= p["follower_band"][1]:
      return False
    if not p["play_band"][0] <= plays <= p["play_band"][1]:
      return False
    rate = likes / max(plays, 1)
    if not p["min_like_rate"] <= rate <= p["max_like_rate"]:
      return False
    # Re-upload/aggregator accounts: huge catalogues, few followers.
    if (user.get("track_count") or 0) > 400 and followers < 5000:
      return False
    # A re-upload of a seed artist's own track is not a discovery, whoever
    # posted it. This single rule cleared most of one early run's output.
    if any(n and n in norm(title) for n in seed_names):
      return False
    return True

  def rank(self, client, seeds, cand, meta, exclude=(), negcand=None):
    a = self.a
    seed_names = {norm(s.artist) for s in seeds}
    seed_uids = {s.uid for s in seeds}
    exclude = set(exclude) | {s.id for s in seeds}

    # Channel agreement is the strongest single feature, so apply it before
    # hydrating: it drops the pool ~12x and saves most of the /tracks calls.
    # Rocchio: how strongly the --negative seeds' own audience and curators
    # vouch for this candidate. Scaled to its own max so it combines with the
    # z-scored features rather than swamping them.
    negcand = negcand or {}
    negtotal = {tid: sum(ch.values()) for tid, ch in negcand.items()}
    negmax = max(negtotal.values()) if negtotal else 1.0
    def negweight(tid):
      return (negtotal.get(tid, 0.0) / negmax) * a.negative_weight

    keep = {tid: ch for tid, ch in cand.items()
            if len(ch) >= a.min_channels and tid not in exclude}
    missing = [tid for tid in keep if tid not in meta]
    if missing:
      for t in client.hydrate_tracks(missing):
        meta[t["id"]] = t

    rows = []
    seen = set()
    for tid, channels in keep.items():
      t = meta.get(tid)
      if not t or not t.get("title"):
        continue
      u = t.get("user") or {}
      if u.get("id") in seed_uids:
        continue
      if not self.viable(t, u, seed_names):
        continue
      is_edit = bool(EDIT_RE.search(t["title"]))
      if a.kind == "remix" and not is_edit:
        continue
      if a.kind == "original" and is_edit:
        continue
      key = (norm(u.get("username")), norm_title(t["title"]))
      if key in seen:
        continue
      seen.add(key)
      rows.append({
          "id": tid, "track": t, "user": u, "channels": channels,
          "features": features(t, u, channels),
          "negev": negweight(tid), "edit": is_edit,
      })
    if not rows:
      return []

    # Z-score each feature across the surviving pool, then weight and sum. The
    # pool is the reference point, so the score means "unusual for this pool in
    # the directions that predicted the labelled set".
    keys = list(self.w)
    mean = {k: sum(r["features"][k] for r in rows) / len(rows) for k in keys}
    sd = {}
    for k in keys:
      var = (sum((r["features"][k] - mean[k]) ** 2 for r in rows)
             / max(len(rows) - 1, 1))
      sd[k] = math.sqrt(var) or 1.0
    for r in rows:
      # Rocchio: subtract the negative evidence AFTER z-scoring. Folding it in
      # as a z-scored feature would make --negative-weight a no-op, since
      # z-scoring divides out any scale applied to it.
      r["score"] = (sum(self.w[k] * (r["features"][k] - mean[k]) / sd[k]
                        for k in keys)
                    - r["negev"])
    rows.sort(key=lambda r: -r["score"])

    # Cap per artist so one prolific curator cannot own the list.
    if a.per_artist:
      capped = []
      count = collections.Counter()
      for r in rows:
        name = r["user"].get("username")
        if count[name] >= a.per_artist:
          continue
        count[name] += 1
        capped.append(r)
      rows = capped
    return rows


# ---------------------------------------------------------------------------
# Fitting a profile from a labelled playlist
# ---------------------------------------------------------------------------


def auc(pos, pool, key):
  """Probability a positive outranks a random pool member on `key`."""
  if not pos:
    return 0.5
  random.seed(0)
  sample = random.sample(pool, min(4000, len(pool))) if pool else []
  if not sample:
    return 0.5
  wins = sum((1.0 if a > b else 0.5 if a == b else 0.0)
             for a in (p[key] for p in pos) for b in (s[key] for s in sample))
  return wins / (len(pos) * len(sample))


def fit(client, args, seeds, out_path):
  """Re-derive the popularity envelope and feature weights from a playlist of
  tracks the user likes, by leave-one-out over its members."""
  base = load_profile(None)
  # Envelope: the labelled set's own spread, widened by a margin so the bands
  # do not exclude anything just outside what happens to be in the playlist.
  raw = [client.get(f"/tracks/{s.id}") for s in seeds]
  raw = [t for t in raw if t]
  fol = [(t.get("user") or {}).get("followers_count") or 0 for t in raw]
  plays = [t.get("playback_count") or 0 for t in raw]
  rate = [(t.get("likes_count") or 0) / max(t.get("playback_count") or 1, 1)
          for t in raw]
  secs = [(t.get("duration") or 0) / 1000 for t in raw]
  m = args.fit_margin
  prof = dict(base)
  prof["follower_band"] = [max(1, int(min(fol) / m)), int(max(fol) * m)]
  prof["play_band"] = [max(1, int(min(plays) / m)), int(max(plays) * m)]
  prof["min_like_rate"] = round(min(rate) / m, 4)
  prof["duration_band"] = [int(min(secs) / 1.2), int(max(secs) * 1.2)]

  log(f"envelope from {len(raw)} labelled tracks:")
  log(f"  followers  {prof['follower_band']}  (observed {min(fol)}-{max(fol)})")
  log(f"  plays      {prof['play_band']}  (observed {min(plays)}-{max(plays)})")
  log(f"  like rate  >= {prof['min_like_rate']}  "
      f"(observed {min(rate):.3f}-{max(rate):.3f})")
  log(f"  duration   {prof['duration_band']}s")

  # Leave-one-out: pool every fold's viable candidates, labelling the held-out
  # track, then measure how well each feature separates positives from pool.
  ranker = Ranker(args, prof)
  pos_rows, pool_rows = [], []
  recovered = 0
  for i, held in enumerate(seeds):
    others = [s for s in seeds if s.id != held.id]
    retr = Retriever(client, args, prof)
    cand = retr.retrieve(others)
    meta = dict(retr.meta)
    # Measure over the whole pool: no channel-agreement gate, and no
    # per-artist cap, which would hide a held-out track whose artist already
    # has rows above it. Both are list-shaping, not retrieval or ranking.
    saved = args.min_channels, args.per_artist
    args.min_channels, args.per_artist = 1, 0
    rows = ranker.rank(client, others, cand, meta)
    args.min_channels, args.per_artist = saved
    got = False
    for r in rows:
      (pos_rows if r["id"] == held.id else pool_rows).append(r["features"])
      if r["id"] == held.id:
        got = True
    recovered += got
    log(f"  fold {i + 1}/{len(seeds)} {held.artist[:18]:18s} "
        f"{len(rows):5d} viable  held-out {'recovered' if got else 'MISSED'}")

  log(f"\n{recovered}/{len(seeds)} held-out tracks recovered into the pool "
      f"({len(pool_rows)} negatives)")
  if not pos_rows:
    log("no positives recovered; keeping default weights")
  else:
    log(f"\n  {'feature':8s} {'AUC':>6}  {'labelled':>10} {'pool':>10}")
    weights = {}
    for k in base["weights"]:
      a = auc(pos_rows, pool_rows, k)
      pm = sum(r[k] for r in pos_rows) / len(pos_rows)
      nm = sum(r[k] for r in pool_rows) / max(len(pool_rows), 1)
      log(f"  {k:8s} {a:6.3f}  {pm:10.3f} {nm:10.3f}")
      w = round(a - 0.5, 3)
      if abs(w) >= args.fit_min_auc:
        weights[k] = w
    if weights:
      prof["weights"] = weights
      log(f"\nkept {len(weights)} of {len(base['weights'])} features "
          f"(|AUC-0.5| >= {args.fit_min_auc})")

  with open(out_path, "w") as f:
    json.dump(prof, f, indent=2)
  log(f"\nwrote profile to {out_path}")
  log(f"use it with:  --profile {out_path}")


def evaluate(client, args, seeds, profile):
  """Leave-one-out: hide each labelled track, recommend from the rest, report
  the rank the hidden track comes back at."""
  ranker = Ranker(args, profile)
  # The per-artist cap shapes a listening list; it would also hide a held-out
  # track whose artist already has rows above it, so it is off while measuring.
  args.per_artist = 0
  ranks = []
  for i, held in enumerate(seeds):
    others = [s for s in seeds if s.id != held.id]
    retr = Retriever(client, args, profile)
    cand = retr.retrieve(others)
    rows = ranker.rank(client, others, cand, dict(retr.meta))
    at = next((j for j, r in enumerate(rows, 1) if r["id"] == held.id), None)
    ranks.append(at)
    log(f"  fold {i + 1}/{len(seeds)} {held.artist[:18]:18s} "
        f"{len(rows):5d} ranked   held-out "
        f"{('#' + str(at)) if at else 'MISS'}")
  hits = [r for r in ranks if r]
  print()
  print(f"recovered {len(hits)}/{len(seeds)}")
  for n in (5, 10, 20, 50, 100):
    print(f"  recall@{n:<4d} {sum(1 for r in hits if r <= n)}/{len(seeds)}")
  if hits:
    print(f"  ranks: {sorted(hits)}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def fmt_num(n):
  if n >= 1_000_000:
    return f"{n / 1_000_000:.1f}M"
  if n >= 1_000:
    return f"{n / 1_000:.0f}k"
  return str(n)


def print_table(title, rows, limit, show_url):
  print()
  print(f"== {title} ({min(limit, len(rows))} of {len(rows)})")
  if not rows:
    print("   (nothing survived the filters)")
    return
  print(f"{'#':>3} {'score':>6} {'ch':>5} {'plays':>6} {'artist':>6} "
        f"{'ER':>5}  title")
  for i, r in enumerate(rows[:limit], 1):
    t, u = r["track"], r["user"]
    ch = "".join(sorted(c[0] for c in r["channels"]))
    print(f"{i:>3} {r['score']:>6.2f} {ch:>5} "
          f"{fmt_num(t.get('playback_count') or 0):>6} "
          f"{fmt_num(u.get('followers_count') or 0):>6} "
          f"{100 * r['features']['er']:>4.1f}%  "
          f"{t['title'][:58]} — {(u.get('username') or '?')[:22]}")
    if show_url:
      print(f"{'':>32}{t.get('permalink_url')}")


def rows_to_json(rows, limit):
  out = []
  for r in rows[:limit]:
    t, u = r["track"], r["user"]
    out.append({
        "id": r["id"], "title": t.get("title"), "artist": u.get("username"),
        "url": t.get("permalink_url"), "plays": t.get("playback_count"),
        "likes": t.get("likes_count"),
        "artist_followers": u.get("followers_count"),
        "genre": t.get("genre"), "tags": sorted(tags_of(t)),
        "duration_s": (t.get("duration") or 0) // 1000,
        "is_edit": r["edit"], "channels": sorted(r["channels"]),
        "score": round(r["score"], 4),
        "features": {k: round(v, 4) for k, v in r["features"].items()},
    })
  return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser():
  p = argparse.ArgumentParser(
      description="Recommend SoundCloud tracks from a track or playlist URL.",
      formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  p.add_argument("url", help="soundcloud.com track or playlist URL")

  g = p.add_argument_group("what to return")
  g.add_argument("--kind", choices=("any", "remix", "original"), default="any",
                 help="restrict to edits/remixes or to originals")
  g.add_argument("--limit", type=int, default=25, help="rows to print")
  g.add_argument("--per-artist", type=int, default=2,
                 help="max rows per artist (0 for no cap)")
  g.add_argument("--negative", action="append", metavar="URL",
                 help="a track or playlist of tracks you do NOT want; their "
                      "evidence is subtracted from every candidate (Rocchio "
                      "relevance feedback). Repeatable. Needs "
                      "--likers-per-seed to do much, since the audience "
                      "channel is the only one worth subtracting.")
  g.add_argument("--negative-weight", type=float, default=2.0,
                 help="lambda on the subtracted evidence. Only bites on the "
                      "shared-audience channel, so it needs "
                      "--likers-per-seed; subtracting the artist graph "
                      "measurably hurts.")
  g.add_argument("--min-channels", type=int, default=2,
                 help="require agreement from this many retrieval channels; "
                      "2 drops the pool ~12x at no measured recall cost")
  g.add_argument("--json", action="store_true",
                 help="emit JSON instead of a table")
  g.add_argument("--no-url", dest="url_lines", action="store_false",
                 help="omit the URL under each row")

  g = p.add_argument_group("channels")
  g.add_argument("--no-related", dest="related", action="store_false",
                 help="skip tracks/{id}/related")
  g.add_argument("--no-playlists", dest="playlists", action="store_false",
                 help="skip playlist co-occurrence")
  g.add_argument("--likers-per-seed", type=int, default=0,
                 help="shared-audience channel: likers to sample per seed. "
                      "Off by default: it roughly doubles the request count "
                      "and triples the candidate pool for a gain that is "
                      "within noise on the labels measured so far. Turn it on "
                      "(e.g. 60) to make --negative effective; see the README.")
  g.add_argument("--liker-weight", type=float, default=0.5,
                 help="channel weight for the shared-audience channel")
  g.add_argument("--liker-min-likes", type=int, default=20,
                 help="ignore likers with fewer likes than this")
  g.add_argument("--liker-max-likes", type=int, default=5000,
                 help="ignore hoarders/bots with more likes than this")
  g.add_argument("--hops", type=int, default=2,
                 help="relatedartists hops to walk (0 disables the channel)")
  g.add_argument("--fan", type=int, default=10,
                 help="related artists per artist")
  g.add_argument("--artist-tracks", type=int, default=40,
                 help="tracks to pull per in-band artist")
  g.add_argument("--artist-weight", type=float, default=0.75,
                 help="channel weight for the artist graph")
  g.add_argument("--hop-decay", type=float, default=0.6,
                 help="artist-graph weight decay per extra hop")

  g = p.add_argument_group("playlist channel")
  g.add_argument("--max-playlists", type=int, default=90,
                 help="playlists to fetch (1 call each, ids only)")
  g.add_argument("--playlist-pages", type=int, default=2,
                 help="pages of 50 playlists to list per seed")
  g.add_argument("--min-playlist-size", type=int, default=5)
  g.add_argument("--max-playlist-size", type=int, default=400)
  g.add_argument("--playlist-sweet-spot", type=int, default=60,
                 help="preferred playlist size; hand-curated sets cluster here")
  g.add_argument("--dedupe-jaccard", type=float, default=0.65,
                 help="collapse playlists overlapping by at least this much")
  g.add_argument("--seed-hits-power", type=float, default=1.3,
                 help="exponent on seeds-per-playlist; "
                      ">1 rewards concentration")

  g = p.add_argument_group("seeds")
  g.add_argument("--max-seeds", type=int, default=40,
                 help="sample this many tracks from a large playlist")

  g = p.add_argument_group("profile and evaluation")
  g.add_argument("--profile", help="JSON profile from a previous --fit")
  g.add_argument("--fit", metavar="OUT",
                 help="treat the URL's playlist as labelled positives, "
                      "re-derive the envelope and weights, write them to OUT")
  g.add_argument("--fit-margin", type=float, default=1.6,
                 help="widen fitted bands by this factor")
  g.add_argument("--fit-min-auc", type=float, default=0.03,
                 help="drop features whose |AUC-0.5| is below this")
  g.add_argument("--eval", action="store_true",
                 help="leave-one-out over the playlist; report held-out ranks")

  g = p.add_argument_group("transport")
  g.add_argument("--workers", type=int, default=24, help="parallel requests")
  g.add_argument("--no-cache", dest="cache", action="store_false",
                 help="refetch instead of reading .cache/")
  return p


def load_seeds(client, url, max_seeds):
  obj = client.resolve(url)
  if not obj:
    sys.exit(f"could not resolve {url}")
  kind = obj.get("kind")
  if kind == "track":
    return [Seed(obj)], f"track {obj.get('title')!r}"
  if kind == "playlist":
    ids = [t["id"] for t in obj.get("tracks", [])
           if isinstance(t, dict) and t.get("id")]
    tracks = client.hydrate_tracks(ids)
    by_id = {t["id"]: t for t in tracks}
    ordered = [by_id[i] for i in ids if i in by_id]
    if len(ordered) > max_seeds:
      random.seed(0)
      ordered = random.sample(ordered, max_seeds)
    if not ordered:
      sys.exit("playlist has no readable tracks")
    return ([Seed(t) for t in ordered],
            f"playlist {obj.get('title')!r} ({len(ordered)} tracks)")
  sys.exit(f"unsupported URL kind: {kind}")


def main():
  args = build_parser().parse_args()
  started = time.time()
  client = Client(use_cache=args.cache, workers=args.workers)

  seeds, what = load_seeds(client, args.url, args.max_seeds)
  log(f"seeds: {what}")

  enabled = sum((bool(args.related), bool(args.playlists), args.hops > 0,
                 args.likers_per_seed > 0))
  if not enabled:
    sys.exit("all retrieval channels are disabled; nothing to do")
  if args.min_channels > enabled:
    log(f"--min-channels {args.min_channels} exceeds the {enabled} enabled "
        f"channel(s); using {enabled}")
    args.min_channels = enabled

  if args.fit:
    if len(seeds) < 4:
      sys.exit("--fit needs a playlist of at least 4 tracks")
    fit(client, args, seeds, args.fit)
    log(f"api calls: {client.calls} (cache hits: {client.cache_hits}) "
        f"in {time.time() - started:.1f}s")
    return

  profile = load_profile(args.profile)

  if args.eval:
    if len(seeds) < 4:
      sys.exit("--eval needs a playlist of at least 4 tracks")
    evaluate(client, args, seeds, profile)
    log(f"api calls: {client.calls} (cache hits: {client.cache_hits}) "
        f"in {time.time() - started:.1f}s")
    return

  retriever = Retriever(client, args, profile)
  cand = retriever.retrieve(seeds)

  negcand, negseeds = None, []
  if args.negative:
    for url in args.negative:
      got, label = load_seeds(client, url, args.max_seeds)
      negseeds.extend(got)
      log(f"negative: {label}")
    negcand = Retriever(client, args, profile).retrieve(negseeds)
    log(f"negative evidence over {len(negcand)} candidates "
        f"from {len(negseeds)} disliked track(s)")

  meta = dict(retriever.meta)
  # Never recommend a disliked track back, nor anything by an artist whose
  # track was explicitly rejected.
  drop = {s.id for s in negseeds}
  rows = Ranker(args, profile).rank(client, seeds, cand, meta,
                                    exclude=drop, negcand=negcand)

  if args.json:
    print(json.dumps({
        "seed": what, "profile": profile,
        "candidates": len(cand), "ranked": len(rows),
        "results": rows_to_json(rows, args.limit),
    }, indent=2))
  else:
    lo, hi = profile["follower_band"]
    print_table(
        f"{args.kind} · {lo}-{fmt_num(hi)} followers, "
        f"like rate >= {100 * profile['min_like_rate']:.1f}%, "
        f"{args.min_channels}+ channels"
        + (f", -{args.negative_weight:g}x {len(negseeds)} neg"
           if negseeds else "")
        + " (ch: " + ", ".join(
            n for n, on in (("s=sc-related", args.related),
                            ("p=playlist", args.playlists),
                            ("a=artist", args.hops > 0),
                            ("l=audience", args.likers_per_seed > 0)) if on)
        + ")",
        rows, args.limit, args.url_lines)

  log(f"\napi calls: {client.calls} (cache hits: {client.cache_hits}); "
      f"{len(cand)} candidates, {len(rows)} ranked, "
      f"in {time.time() - started:.1f}s")


if __name__ == "__main__":
  main()
