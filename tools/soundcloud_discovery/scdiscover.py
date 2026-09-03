#!/usr/bin/env python3
# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Local prototype of a crowd-sourced SoundCloud discovery algorithm.

Given a track or playlist URL, expands through the playlists (and optionally
the likers) that contain the seed tracks, scores every other track by how
strongly it co-occurs with the seeds, and prints three lists side by side:

  1. SoundCloud's own "related" recommendations (baseline)
  2. Crowd picks: raw co-occurrence, favours well-known tracks
  3. Up and coming: co-occurrence normalised by popularity, artist size and
     release age, so small/new tracks that the same curators keep filing
     next to your seeds rise to the top

No dependencies beyond the Python standard library (curl is used as a
transport fallback). Responses are cached under .cache/ next to this file.
"""

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api-v2.soundcloud.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
CLIENT_ID_FILE = os.path.join(CACHE_DIR, "client_id")
HYDRATE_BATCH = 50


def log(msg):
  print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Client:

  def __init__(self, use_cache=True, delay=0.15):
    self.use_cache = use_cache
    self.delay = delay
    self.calls = 0
    self.cache_hits = 0
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
          ["curl", "-sL", "--compressed", "-A", UA, "-w", "\n%{http_code}", url],
          capture_output=True, text=True, timeout=60)
      body, _, code = out.stdout.rpartition("\n")
      return int(code or 0), body

  def _cached(self, key, fetch):
    path = os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")
    if self.use_cache and os.path.exists(path):
      self.cache_hits += 1
      with open(path) as f:
        return json.load(f)
    data = fetch()
    if self.use_cache and data is not None:
      with open(path, "w") as f:
        json.dump(data, f)
    return data

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
    scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
    for src in reversed(scripts):
      _, js = self._fetch_text(src)
      m = re.search(r'client_id:"([A-Za-z0-9]{32})"', js)
      if m:
        with open(CLIENT_ID_FILE, "w") as f:
          f.write(m.group(1))
        return m.group(1)
    sys.exit("could not find a client_id in SoundCloud's web bundles")

  # -- api --------------------------------------------------------------------

  def get(self, path_or_url, **params):
    if path_or_url.startswith("http"):
      url = path_or_url
      sep = "&" if "?" in url else "?"
    else:
      url = API + path_or_url
      sep = "?"
    params = {k: v for k, v in params.items() if v is not None}
    q = urllib.parse.urlencode(params)
    key = url + (sep + q if q else "")

    def fetch():
      for attempt in range(4):
        full = key + ("&" if "?" in key else "?") + "client_id=" + self.client_id
        self.calls += 1
        code, body = self._fetch_text(full)
        if code == 200:
          time.sleep(self.delay)
          return json.loads(body)
        if code in (401, 403) and attempt == 0:
          log("client_id rejected; refreshing")
          self.client_id = self._discover_client_id()
          continue
        if code == 404:
          return None
        if code == 429 or code >= 500:
          wait = 2 ** attempt
          log(f"HTTP {code} on {path_or_url}; retrying in {wait}s")
          time.sleep(wait)
          continue
        raise RuntimeError(f"HTTP {code} for {full}: {body[:200]}")
      raise RuntimeError(f"giving up on {path_or_url}")

    return self._cached(key, fetch)

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
      data = self.get(nxt)
    return out

  def resolve(self, url):
    return self.get("/resolve", url=url)

  def hydrate_tracks(self, ids):
    """Fetch full track objects for a list of ids, 50 per call."""
    out = []
    ids = list(ids)
    for i in range(0, len(ids), HYDRATE_BATCH):
      batch = ids[i:i + HYDRATE_BATCH]
      got = self.get("/tracks", ids=",".join(str(x) for x in batch)) or []
      out.extend(got)
    return out

  def playlist_tracks(self, playlist_id):
    """Full track objects for a playlist. The API returns the first five in
    full and the rest as {id} stubs which must be hydrated separately."""
    pl = self.get(f"/playlists/{playlist_id}", representation="full")
    if not pl:
      return None, []
    tracks = pl.get("tracks", [])
    by_id = {t["id"]: t for t in tracks if "title" in t}
    stubs = [t["id"] for t in tracks if "title" not in t]
    for t in self.hydrate_tracks(stubs):
      by_id[t["id"]] = t
    # Keep the playlist's own order: SoundCloud appends new adds at the end,
    # so position is the only hint we get about when a track was added.
    ordered = [by_id[t["id"]] for t in tracks if t["id"] in by_id]
    return pl, ordered


# ---------------------------------------------------------------------------
# Recommender
# ---------------------------------------------------------------------------


def parse_date(s):
  if not s:
    return None
  try:
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
  except ValueError:
    return None


class Track:
  __slots__ = ("id", "title", "artist", "artist_followers", "plays", "likes",
               "created", "url", "genre")

  def __init__(self, t):
    self.id = t["id"]
    self.title = t.get("title") or "?"
    u = t.get("user") or {}
    self.artist = u.get("username") or "?"
    self.artist_followers = u.get("followers_count") or 0
    self.plays = t.get("playback_count") or 0
    self.likes = t.get("likes_count") or 0
    # created_at is when the file was uploaded; display_date/release_date is
    # the date the artist declares and SoundCloud shows. Reuploads of old
    # tracks have a fresh created_at, so take the earliest date we are given.
    dates = [parse_date(t.get(k)) for k in ("display_date", "release_date", "created_at")]
    dates = [d for d in dates if d]
    self.created = min(dates) if dates else None
    self.url = t.get("permalink_url") or ""
    self.genre = t.get("genre") or ""

  @property
  def age_days(self):
    if not self.created:
      return None
    return (dt.datetime.now(dt.timezone.utc) - self.created).days


class Context:
  """A list of tracks made by a human: a playlist, or a user's likes."""
  __slots__ = ("kind", "id", "title", "owner", "track_ids", "order", "seed_hits",
               "created", "modified")

  def __init__(self, kind, id_, title, owner, track_ids, created=None, modified=None):
    self.kind = kind
    self.id = id_
    self.title = title
    self.owner = owner
    self.order = list(track_ids)
    self.track_ids = set(track_ids)
    self.seed_hits = 0
    self.created = parse_date(created)
    self.modified = parse_date(modified)


class Recommender:

  def __init__(self, client, args):
    self.c = client
    self.a = args
    self.tracks = {}  # id -> Track
    self.contexts = {}  # (kind,id) -> Context
    self.seed_ids = []
    self.exclude_ids = set()
    self.user_seeds = {}  # user id -> set of seed/secondary track ids they liked
    self.user_info = {}

  def remember(self, raw_tracks):
    for t in raw_tracks:
      if t.get("kind", "track") == "track" and "title" in t:
        self.tracks[t["id"]] = Track(t)

  # -- seeds ------------------------------------------------------------------

  def load_seeds(self, url):
    obj = self.c.resolve(url)
    if not obj:
      sys.exit(f"could not resolve {url}")
    kind = obj.get("kind")
    if kind == "track":
      self.remember([obj])
      return "track", obj.get("title"), [obj["id"]]
    if kind == "playlist":
      _, tracks = self.c.playlist_tracks(obj["id"])
      self.remember(tracks)
      return "playlist", obj.get("title"), [t["id"] for t in tracks]
    sys.exit(f"unsupported SoundCloud object kind: {kind}")

  # -- expansion --------------------------------------------------------------

  def candidate_playlists(self, seed_id, want):
    # Roughly half of the returned sets fall outside the size window, so scan
    # enough pages of 50 to fill the per-seed quota.
    pages = self.a.playlist_pages or max(1, math.ceil(want * 2 / 50))
    pls = self.c.collection(f"/tracks/{seed_id}/playlists_without_albums",
                            pages=pages, limit=50, representation="mini")
    good = []
    for p in pls:
      n = p.get("track_count") or 0
      if self.a.min_playlist_size <= n <= self.a.max_playlist_size:
        good.append(p)
    # Prefer mid-sized, hand-curated sets: sort by distance from a sweet spot
    # so neither 15-track stubs nor 400-track dumps dominate the budget.
    sweet = math.log(80)
    good.sort(key=lambda p: abs(math.log(max(p.get("track_count"), 1)) - sweet))
    return good

  def expand(self):
    a = self.a
    # Playlists: gather candidates per seed, count how many seeds each
    # playlist was returned for, then hydrate the best ones.
    returned_for = collections.Counter()
    meta = {}
    per_seed = max(a.playlists_per_seed, a.max_playlists // len(self.seed_ids))
    for i, sid in enumerate(self.seed_ids):
      log(f"[{i + 1}/{len(self.seed_ids)}] playlists containing seed {sid}")
      for p in self.candidate_playlists(sid, per_seed)[:per_seed]:
        returned_for[p["id"]] += 1
        meta[p["id"]] = p
    # Playlists returned for several seeds first; they are the strongest
    # evidence and are shared across seeds so the budget stretches further.
    ranked = sorted(meta.values(),
                    key=lambda p: (-returned_for[p["id"]], p.get("track_count") or 0))
    budget = a.max_playlists
    for p in ranked[:budget]:
      pl, tracks = self.c.playlist_tracks(p["id"])
      if not pl:
        continue
      self.remember(tracks)
      ctx = Context("playlist", pl["id"], pl.get("title") or "?",
                    (pl.get("user") or {}).get("username") or "?",
                    [t["id"] for t in tracks],
                    created=pl.get("created_at"), modified=pl.get("last_modified"))
      self.contexts[("playlist", pl["id"])] = ctx
    log(f"hydrated {len(self.contexts)} playlists")

    # Likers: treat a liker's own likes as a pseudo-playlist. Optional because
    # it costs one call per liker and likes lists are noisier than curated
    # sets.
    if a.likers_per_seed > 0:
      for sid in self.seed_ids:
        likers = self.c.collection(f"/tracks/{sid}/likers", limit=50)
        # People who like a lot but are not celebrities behave like curators.
        likers = [u for u in likers if 100 <= (u.get("likes_count") or 0) <= 20000]
        likers.sort(key=lambda u: -(u.get("likes_count") or 0))
        for u in likers[:a.likers_per_seed]:
          key = ("likes", u["id"])
          if key in self.contexts:
            continue
          items = self.c.collection(f"/users/{u['id']}/likes", limit=50, pages=1)
          tracks = [x["track"] for x in items if x.get("track")]
          self.remember(tracks)
          self.contexts[key] = Context("likes", u["id"], "likes",
                                       u.get("username") or "?",
                                       [t["id"] for t in tracks])
      log(f"total contexts incl. likers: {len(self.contexts)}")

  # -- scoring ----------------------------------------------------------------

  def dedupe_contexts(self):
    """Collapse near-identical lists. SoundCloud playlists get copied wholesale
    (and promotion rings seed the same list under many accounts), so six
    copies of one set must count as one person's opinion, not six."""
    a = self.a
    if a.dedupe_jaccard >= 1:
      return 0
    kept = []
    merged = 0
    for ctx in sorted(self.contexts.values(), key=lambda x: -len(x.track_ids)):
      dup = None
      for k in kept:
        inter = len(ctx.track_ids & k.track_ids)
        if inter and inter / len(ctx.track_ids | k.track_ids) >= a.dedupe_jaccard:
          dup = k
          break
      if dup is None:
        kept.append(ctx)
      else:
        merged += 1
    self.contexts = {(c.kind, c.id): c for c in kept}
    return merged

  def score(self):
    a = self.a
    merged = self.dedupe_contexts()
    if merged:
      log(f"merged {merged} near-duplicate list(s) into their originals")
    seeds = set(self.seed_ids)
    raw = collections.Counter()
    in_contexts = collections.Counter()
    owners = collections.defaultdict(set)
    max_hits = collections.Counter()
    dropped = 0
    for ctx in self.contexts.values():
      ctx.seed_hits = len(ctx.track_ids & seeds)
      if ctx.seed_hits == 0:
        continue
      # A list holding most of the seeds is a copy of the input playlist (or
      # a superset of it). It would dominate every score and makes holdout
      # evaluation meaningless, so skip it.
      if len(seeds) >= 5 and ctx.seed_hits / len(seeds) > a.max_seed_fraction:
        dropped += 1
        continue
      size = len(ctx.track_ids)
      # More seeds in the same list => much stronger evidence. Very long lists
      # say less about any one pairing, hence the mild size penalty.
      w = (ctx.seed_hits ** a.seed_overlap_power) / math.log(size + 10)
      if ctx.kind == "likes":
        w *= a.likes_weight
      for tid in ctx.track_ids:
        if tid in seeds or tid in self.exclude_ids:
          continue
        raw[tid] += w
        in_contexts[tid] += 1
        owners[tid].add(ctx.owner)
        max_hits[tid] = max(max_hits[tid], ctx.seed_hits)
    if dropped:
      log(f"dropped {dropped} near-duplicate list(s) of the seed playlist")

    rows = []
    for tid, r in raw.items():
      t = self.tracks.get(tid)
      if not t:
        continue
      # Popularity normalisation: divide out how "everywhere" a track is, so
      # a 30k-play tune in three of your playlists' neighbours beats a 3M-play
      # anthem in four of them. Powers apply to linear counts (with a floor so
      # a handful of plays does not explode the score).
      pop = (t.plays + 1000) ** a.popularity_power
      artist = (t.artist_followers + 500) ** a.artist_power
      age = t.age_days
      recency = 1.0
      if age is not None:
        recency = 1.0 + a.recency_boost * math.exp(-max(age, 0) / 365.0)
      upcoming = r / pop / artist * recency
      rows.append({
          "track": t,
          "raw": r,
          "upcoming": upcoming,
          "contexts": in_contexts[tid],
          "owners": len(owners[tid]),
          "max_seed_hits": max_hits[tid],
      })
    return rows

  def seed_artists(self):
    return {self.tracks[s].artist for s in self.seed_ids if s in self.tracks}

  @staticmethod
  def cap_per_artist(rows, n):
    if n <= 0:
      return rows
    seen = collections.Counter()
    out = []
    for x in rows:
      seen[x["track"].artist] += 1
      if seen[x["track"].artist] <= n:
        out.append(x)
    return out

  @staticmethod
  def percentile(values, q):
    if not values:
      return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]

  def lists(self, rows):
    a = self.a
    if not a.include_seed_artists:
      artists = self.seed_artists()
      rows = [x for x in rows if x["track"].artist not in artists]
    crowd = self.cap_per_artist(sorted(rows, key=lambda x: -x["raw"]), a.per_artist)

    # Evidence must come from different people; one curator filing a track in
    # two of their own sets is not a trend.
    # With a real taste profile, demand that a pick sits next to several seeds
    # in at least one list; a single shared seed is how off-genre lists leak in.
    min_hits = a.min_seed_hits if a.min_seed_hits else (2 if len(self.seed_ids) >= 10 else 1)
    pool = [x for x in rows
            if x["owners"] >= a.min_contexts and x["track"].plays >= a.upcoming_min_plays
            and x["max_seed_hits"] >= min_hits
            # Organic tracks get liked by a few percent of listeners. More likes
            # than plays means bought engagement.
            and x["track"].likes <= a.max_like_ratio * x["track"].plays]
    # "Up and coming" is relative to the neighbourhood: keep the less-played
    # half of the pool and the smaller-artist half, then the absolute caps.
    max_plays = min(a.upcoming_max_plays,
                    self.percentile([x["track"].plays for x in pool], a.upcoming_percentile))
    max_followers = min(a.upcoming_max_artist_followers,
                        self.percentile([x["track"].artist_followers for x in pool],
                                        a.upcoming_percentile))
    self.upcoming_thresholds = (max_plays, max_followers)
    upcoming = [
        x for x in pool
        if x["track"].plays <= max_plays
        and x["track"].artist_followers <= max_followers
        and (x["track"].age_days is None or x["track"].age_days <= a.upcoming_max_age_days)
    ]
    upcoming.sort(key=lambda x: -x["upcoming"])
    return crowd, self.cap_per_artist(upcoming, a.per_artist)

  # -- rising: timestamped adoption by taste neighbours ------------------------

  def collect_neighbours(self, track_ids, pages, label):
    """Record who liked or reposted each of `track_ids`. Heavy likers
    (hoarders, bots) are skipped: their likes say little."""
    a = self.a
    for i, tid in enumerate(track_ids):
      for path in (f"/tracks/{tid}/likers", f"/tracks/{tid}/reposters"):
        for u in self.c.collection(path, pages=pages, limit=100):
          n = u.get("likes_count") or 0
          if not (20 <= n <= a.neighbour_max_likes):
            continue
          self.user_seeds.setdefault(u["id"], set()).add(tid)
          self.user_info[u["id"]] = u
      log(f"[{i + 1}/{len(track_ids)}] {label} of {tid}: {len(self.user_seeds)} people so far")

  def rank_neighbours(self, seed_ids, weight_scale=1.0, exclude=()):
    """Weight people by how many of `seed_ids` they touched, diluted by how
    indiscriminate they are."""
    a = self.a
    seeds = set(seed_ids)
    seed_artists = self.seed_artists()
    ranked = []
    for uid, touched in self.user_seeds.items():
      if uid in exclude:
        continue
      h = len(touched & seeds)
      if h == 0:
        continue
      u = self.user_info[uid]
      if u.get("username") in seed_artists:
        continue
      w = weight_scale * (h ** a.seed_overlap_power) / math.log((u.get("likes_count") or 0) + 10)
      ranked.append((w, h, u))
    ranked.sort(key=lambda x: -x[0])
    return ranked

  def neighbour_feed(self, uid):
    a = self.a
    feed = []
    for x in self.c.collection(f"/users/{uid}/likes", pages=a.neighbour_like_pages, limit=50):
      if x.get("track"):
        feed.append((x["track"], x.get("created_at"), 1.0))
    for x in self.c.collection(f"/stream/users/{uid}/reposts", pages=1, limit=50):
      if x.get("track") and x.get("type", "").startswith("track"):
        feed.append((x["track"], x.get("created_at"), 2.0))
    return feed

  def rising(self, rows, budget_scale=1.0):
    """Score tracks by *when* taste neighbours adopted them, not how many
    plays they have. Each like/repost event decays with a half-life, so a
    track being picked up by your scene this month outranks one they all
    liked two years ago. Reposts weigh double: a repost is public curation."""
    a = self.a
    if a.neighbours <= 0:
      return [], 0
    now = dt.datetime.now(dt.timezone.utc)
    seeds = set(self.seed_ids)
    score = collections.Counter()
    people = collections.defaultdict(set)
    latest = {}
    events = collections.Counter()
    neighbour_people = collections.defaultdict(set)

    def vote(tid, person, when_days, weight):
      decay = 0.5 ** (max(when_days, 0) / a.half_life_days)
      score[tid] += weight * decay
      people[tid].add(person)
      events[tid] += 1
      latest[tid] = min(latest.get(tid, 10 ** 6), when_days)

    # Source 1: playlists from the co-occurrence pass. We never learn when a
    # track was added, but new adds land at the end of a list, so the last
    # quarter of a recently modified playlist gets the modification date and
    # everything else the midpoint of the list's life. Weak timestamps, weak
    # weight, but many independent people.
    for ctx in self.contexts.values():
      hits = len(ctx.track_ids & seeds)
      if hits == 0 or ctx.kind != "playlist":
        continue
      if len(seeds) >= 5 and hits / len(seeds) > a.max_seed_fraction:
        continue
      w = a.playlist_vote * (hits ** a.seed_overlap_power) / math.log(len(ctx.order) + 10)
      mod_days = (now - ctx.modified).days if ctx.modified else 365
      created_days = (now - ctx.created).days if ctx.created else mod_days + 365
      mid_days = (mod_days + created_days) / 2
      tail = int(len(ctx.order) * 0.75)
      for pos, tid in enumerate(ctx.order):
        if tid in seeds or tid in self.exclude_ids:
          continue
        vote(tid, ("pl", ctx.owner), mod_days if pos >= tail else mid_days, w)

    # Source 2: taste neighbours' timestamped likes and reposts.
    def ingest(neighbours):
      for w, h, u in neighbours:
        uid = u["id"]
        for t, when, kind_w in self.neighbour_feed(uid):
          tid = t["id"]
          if tid in seeds or tid in self.exclude_ids:
            continue
          self.remember([t])
          d = parse_date(when)
          vote(tid, ("u", uid), (now - d).days if d else 365, w * kind_w)
          neighbour_people[tid].add(uid)

    if not self.user_seeds:
      self.collect_neighbours(self.seed_ids, a.neighbour_pages, "likers")
    n1 = a.neighbours if budget_scale > 0 else min(a.neighbours, a.loose_neighbours)
    round1 = self.rank_neighbours(self.seed_ids)[:n1]
    ingest(round1)
    used = {u["id"] for _, _, u in round1}
    n_neighbours = len(round1)

    # Snowball: tracks several first-round neighbours converged on are, in
    # effect, extra seeds. Their likers are people we would never have found
    # from the original seeds alone, and they were selected for taste rather
    # than for happening to like one track.
    n_sb_seeds = int(a.snowball_seeds * budget_scale)
    n_sb_neighbours = int(a.snowball_neighbours * budget_scale)
    if n_sb_seeds > 0 and n_sb_neighbours > 0:
      agreed = [(len(v), tid) for tid, v in neighbour_people.items()
                if len(v) >= a.snowball_min_people and tid in self.tracks
                and self.tracks[tid].artist_followers <= a.rising_max_artist_followers]
      agreed.sort(reverse=True)
      secondary = [tid for _, tid in agreed[:n_sb_seeds]]
      if secondary:
        log(f"snowball: {len(secondary)} tracks agreed on by {a.snowball_min_people}+ neighbours")
        self.collect_neighbours(secondary, 1, "likers (snowball)")
        round2 = self.rank_neighbours(secondary, a.snowball_weight, exclude=used)
        round2 = round2[:n_sb_neighbours]
        ingest(round2)
        n_neighbours += len(round2)
    log(f"rising: {n_neighbours} neighbours, {len(score)} tracks with votes")

    seed_artists = self.seed_artists()
    out = []
    pool = [self.tracks[t] for t in score if t in self.tracks]
    vel = [t.plays / max(t.age_days or 30, 30) for t in pool if t.plays]
    med_vel = self.percentile(vel, 0.5) or 1.0
    cooc = {x["track"].id: x for x in rows}
    for tid, sc in score.items():
      t = self.tracks.get(tid)
      if not t or len(people[tid]) < a.min_contexts:
        continue
      if not a.include_seed_artists and t.artist in seed_artists:
        continue
      if not (a.rising_min_plays <= t.plays <= a.rising_max_plays):
        continue
      if t.likes > a.max_like_ratio * max(t.plays, 1):
        continue
      if latest[tid] > a.rising_max_event_days:
        continue
      if t.age_days is not None and t.age_days > a.rising_max_age_days:
        continue
      if t.artist_followers > a.rising_max_artist_followers:
        continue
      # Velocity: plays per day of life, relative to the pool. A 60k-play track
      # from four months ago is moving faster than a 400k one from 2019.
      velocity = (t.plays / max(t.age_days or 30, 30)) / med_vel
      # Concentration: how much of the track's whole audience is *your*
      # neighbours, recently. Small denominators mean it is breaking in your
      # scene specifically rather than everywhere.
      concentration = len(people[tid]) / math.log10(t.likes + 10)
      out.append({
          "track": t,
          "rising": sc * (velocity ** a.velocity_power) * concentration,
          "raw": sc,
          "contexts": events[tid],
          "owners": len(people[tid]),
          "max_seed_hits": cooc[tid]["max_seed_hits"] if tid in cooc else 0,
          "latest_days": latest[tid],
          "velocity": velocity,
      })
    out.sort(key=lambda x: -x["rising"])
    return self.cap_per_artist(out, a.per_artist), n_neighbours

  # -- seed clustering --------------------------------------------------------

  def cluster_seeds(self):
    """Group seeds that share playlists or likers. A mixed playlist is really
    several taste profiles; scored together they cancel each other out."""
    a = self.a
    seeds = list(self.seed_ids)
    if len(seeds) < a.cluster_min_seeds:
      return [seeds], []
    idx = {s: i for i, s in enumerate(seeds)}
    sim = collections.Counter()
    for ctx in self.contexts.values():
      inside = [idx[t] for t in ctx.track_ids if t in idx]
      if len(inside) / len(seeds) > a.max_seed_fraction and len(seeds) >= 5:
        continue  # a copy of the input playlist links everything to everything
      for i in inside:
        for j in inside:
          if i < j:
            sim[(i, j)] += 1.0
    if not self.user_seeds:
      self.collect_neighbours(self.seed_ids, a.neighbour_pages, "likers")
    for touched in self.user_seeds.values():
      inside = sorted(idx[t] for t in touched if t in idx)
      for i in inside:
        for j in inside:
          if i < j:
            sim[(i, j)] += a.liker_link_weight
    # Union-find over edges above the threshold.
    parent = list(range(len(seeds)))

    def find(x):
      while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
      return x

    for (i, j), w in sim.items():
      if w >= a.cluster_link_threshold:
        parent[find(i)] = find(j)
    groups = collections.defaultdict(list)
    for i, s in enumerate(seeds):
      groups[find(i)].append(s)
    clusters = sorted(groups.values(), key=len, reverse=True)
    linked = [c for c in clusters if len(c) >= 2]
    loose = [s for c in clusters if len(c) == 1 for s in c]
    return linked, loose

  def soundcloud_related(self):
    """Baseline: SoundCloud's own related tracks, unioned across (a sample
    of) seeds and ranked by how many seeds recommended them."""
    sample = self.seed_ids[:self.a.related_seeds]
    cnt = collections.Counter()
    for sid in sample:
      rel = self.c.collection(f"/tracks/{sid}/related", limit=50) or []
      self.remember(rel)
      for t in rel:
        if t["id"] not in set(self.seed_ids) and t["id"] not in self.exclude_ids:
          cnt[t["id"]] += 1
    return [{"track": self.tracks[tid], "raw": n, "contexts": n, "owners": n,
             "max_seed_hits": 1, "upcoming": 0}
            for tid, n in cnt.most_common() if tid in self.tracks]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def fmt_num(n):
  if n >= 1_000_000:
    return f"{n / 1_000_000:.1f}M"
  if n >= 1_000:
    return f"{n / 1_000:.0f}k"
  return str(n)


def fmt_age(days):
  if days is None:
    return "  ?"
  if days < 60:
    return f"{days:>2}d"
  if days < 730:
    return f"{days // 30:>2}mo"
  return f"{days // 365:>2}y"


def print_table(title, rows, limit, score_key, show_url):
  print()
  print(f"== {title} ({min(limit, len(rows))} of {len(rows)})")
  if not rows:
    print("   (nothing)")
    return
  top = rows[0][score_key] or 1.0
  rows = [dict(x, **{score_key: x[score_key] / top}) for x in rows[:limit]]
  hdr = (f"{'#':>3} {'score':>6} {'lists':>5} {'ppl':>4} {'seeds':>5} {'plays':>6} "
         f"{'artist':>6} {'age':>4}  ")
  if "latest_days" in rows[0]:
    hdr += f"{'last':>4} {'speed':>6}  "
  print(hdr + "title")
  for i, x in enumerate(rows[:limit], 1):
    t = x["track"]
    line = (f"{i:>3} {x[score_key]:>6.2f} {x['contexts']:>5} {x['owners']:>4} {x['max_seed_hits']:>5} "
            f"{fmt_num(t.plays):>6} {fmt_num(t.artist_followers):>6} {fmt_age(t.age_days):>4}  ")
    if "latest_days" in x:
      line += f"{fmt_age(x['latest_days']):>4} {x['velocity']:>5.1f}x  "
    line += f"{t.title[:60]} — {t.artist[:24]}"
    print(line)
    if show_url:
      print(f"{'':>45}{t.url}")


def rows_to_json(rows, score_key, limit):
  out = []
  for x in rows[:limit]:
    t = x["track"]
    out.append({
        "id": t.id, "title": t.title, "artist": t.artist, "url": t.url,
        "plays": t.plays, "likes": t.likes, "artist_followers": t.artist_followers,
        "age_days": t.age_days, "genre": t.genre, "score": round(x[score_key], 4),
        "lists": x["contexts"], "people": x["owners"], "max_seed_hits": x["max_seed_hits"],
        **({"latest_event_days": x["latest_days"], "velocity": round(x["velocity"], 2)}
           if "latest_days" in x else {}),
    })
  return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser():
  p = argparse.ArgumentParser(description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument("url", help="SoundCloud track or playlist URL")
  p.add_argument("--limit", type=int, default=25, help="rows per list")
  p.add_argument("--json", action="store_true", help="emit JSON instead of tables")
  p.add_argument("--urls", action="store_true", help="print track URLs under each row")
  p.add_argument("--no-cache", action="store_true")
  p.add_argument("--delay", type=float, default=0.15, help="seconds between uncached calls")

  g = p.add_argument_group("expansion budget")
  g.add_argument("--max-seeds", type=int, default=40,
                 help="sample at most this many seed tracks from a playlist")
  g.add_argument("--playlists-per-seed", type=int, default=8)
  g.add_argument("--playlist-pages", type=int, default=0,
                 help="pages of 50 'in playlists' results to scan per seed "
                      "(0 = enough to fill the per-seed quota)")
  g.add_argument("--max-playlists", type=int, default=40,
                 help="total playlists to hydrate (each costs 1 + size/50 calls)")
  g.add_argument("--min-playlist-size", type=int, default=12)
  g.add_argument("--max-playlist-size", type=int, default=400)
  g.add_argument("--likers-per-seed", type=int, default=0,
                 help="also read this many likers' own likes per seed (0 = off)")
  g.add_argument("--related-seeds", type=int, default=10,
                 help="seeds to query SoundCloud's own related list for (baseline)")

  g = p.add_argument_group("rising (taste-neighbour adoption)")
  g.add_argument("--neighbours", type=int, default=100,
                 help="taste neighbours to read likes/reposts from (0 = skip rising)")
  g.add_argument("--neighbour-like-pages", type=int, default=2,
                 help="pages of 50 likes to read per neighbour")
  g.add_argument("--playlist-vote", type=float, default=0.5,
                 help="weight of a playlist placement relative to a neighbour like")
  g.add_argument("--snowball-seeds", type=int, default=15,
                 help="tracks agreed on by first-round neighbours to use as extra seeds "
                      "(0 = off)")
  g.add_argument("--snowball-min-people", type=int, default=3,
                 help="neighbours that must agree before a track becomes an extra seed")
  g.add_argument("--snowball-neighbours", type=int, default=100,
                 help="extra neighbours to read from the snowball round")
  g.add_argument("--snowball-weight", type=float, default=0.6,
                 help="weight of a snowball neighbour relative to a first-round one")
  g.add_argument("--rising-max-artist-followers", type=int, default=500_000,
                 help="major-label scale artists are not discovery; drop above this")
  g.add_argument("--rising-max-age-days", type=int, default=1095,
                 help="only tracks released within this many days can be rising; "
                      "older revivals still show in crowd picks")
  g.add_argument("--rising-max-event-days", type=int, default=120,
                 help="drop tracks nobody touched within this many days")
  g.add_argument("--neighbour-pages", type=int, default=1,
                 help="pages of 100 likers and 100 reposters to scan per seed")
  g.add_argument("--neighbour-max-likes", type=int, default=15000,
                 help="ignore people with more likes than this (hoarders, bots)")
  g.add_argument("--half-life-days", type=float, default=45,
                 help="a like/repost this old counts half as much as one today")
  g.add_argument("--velocity-power", type=float, default=0.5,
                 help="weight of plays-per-day relative to the pool")
  g.add_argument("--rising-min-plays", type=int, default=5000)
  g.add_argument("--rising-max-plays", type=int, default=1_000_000)

  g = p.add_argument_group("scoring")
  g.add_argument("--seed-overlap-power", type=float, default=1.5,
                 help="exponent on #seeds found in a list")
  g.add_argument("--likes-weight", type=float, default=0.5,
                 help="weight of a liker's likes list relative to a playlist")
  g.add_argument("--popularity-power", type=float, default=0.5,
                 help="exponent on play count to divide out (0 = ignore)")
  g.add_argument("--artist-power", type=float, default=0.25,
                 help="exponent on artist follower count to divide out (0 = ignore)")
  g.add_argument("--recency-boost", type=float, default=0.5,
                 help="max multiplier bonus for brand-new tracks, decays over ~1y")
  g.add_argument("--min-contexts", type=int, default=2,
                 help="up-and-coming picks must appear in lists from at least this "
                      "many different people")
  g.add_argument("--dedupe-jaccard", type=float, default=0.4,
                 help="lists whose track sets overlap at least this much (Jaccard) "
                      "count as one list (1 = off)")
  g.add_argument("--max-seed-fraction", type=float, default=0.6,
                 help="ignore lists containing more than this fraction of the seeds "
                      "(copies of the input playlist)")
  g.add_argument("--min-seed-hits", type=int, default=0,
                 help="up-and-coming picks must share one list with this many seeds "
                      "(0 = auto: 2 for playlists of 10+ tracks, else 1)")
  g.add_argument("--max-like-ratio", type=float, default=0.5,
                 help="up-and-coming: drop tracks whose likes exceed this fraction of "
                      "plays (bought engagement)")
  g.add_argument("--upcoming-min-plays", type=int, default=1000,
                 help="ignore tracks below this many plays (unreleased/private-ish noise)")
  g.add_argument("--upcoming-percentile", type=float, default=0.5,
                 help="up-and-coming picks must be below this percentile of the "
                      "candidate pool in plays and artist followers")
  g.add_argument("--upcoming-max-plays", type=int, default=250_000)
  g.add_argument("--upcoming-max-artist-followers", type=int, default=50_000)
  g.add_argument("--upcoming-max-age-days", type=int, default=730)
  g.add_argument("--per-artist", type=int, default=2,
                 help="max rows per artist in the crowd/up-and-coming lists (0 = off)")
  g.add_argument("--include-seed-artists", action="store_true",
                 help="allow tracks by the seed tracks' own artists")

  g = p.add_argument_group("clustering")
  g.add_argument("--clusters", choices=["auto", "off"], default="off",
                 help="also split seeds into taste clusters and score each separately. "
                      "Off by default: every extra group costs a further neighbour pass, "
                      "and the blended list plus snowball carries most of the value")
  g.add_argument("--cluster-min-seeds", type=int, default=6)
  g.add_argument("--cluster-link-threshold", type=float, default=1.0,
                 help="two seeds join a cluster when shared playlists + weighted "
                      "shared likers reach this")
  g.add_argument("--liker-link-weight", type=float, default=0.34,
                 help="a shared liker counts this much of a shared playlist")
  g.add_argument("--cluster-limit", type=int, default=10, help="rows per list per cluster")
  g.add_argument("--max-loose-seeds", type=int, default=6,
                 help="unlinked seeds to report individually")
  g.add_argument("--loose-neighbours", type=int, default=30,
                 help="neighbours to read for each unlinked seed (about 3 requests each)")
  g.add_argument("--cluster-snowball-scale", type=float, default=0.3,
                 help="snowball budget for each cluster pass, as a fraction of the main one")

  g = p.add_argument_group("evaluation")
  g.add_argument("--holdout", type=float, default=0.0,
                 help="playlist input only: hide this fraction of tracks and report "
                      "how many each list recovers (recall@limit)")
  g.add_argument("--seed", type=int, default=7, help="random seed for sampling")
  return p


def main():
  sys.stdout.reconfigure(line_buffering=True)
  args = build_parser().parse_args()
  rng = random.Random(args.seed)
  client = Client(use_cache=not args.no_cache, delay=args.delay)
  rec = Recommender(client, args)

  kind, title, seed_ids = rec.load_seeds(args.url)
  log(f"resolved {kind}: {title!r} with {len(seed_ids)} track(s)")

  held_out = []
  if args.holdout > 0 and kind == "playlist" and len(seed_ids) >= 5:
    ids = list(seed_ids)
    rng.shuffle(ids)
    k = max(1, int(len(ids) * args.holdout))
    held_out, seed_ids = ids[:k], ids[k:]
    log(f"holding out {len(held_out)} tracks for evaluation")

  if len(seed_ids) > args.max_seeds:
    seed_ids = rng.sample(seed_ids, args.max_seeds)
    log(f"sampled {len(seed_ids)} seeds")
  rec.seed_ids = seed_ids

  rec.expand()
  rows = rec.score()
  crowd, upcoming = rec.lists(rows)
  related = rec.soundcloud_related()
  rising, n_neighbours = rec.rising(rows)
  log(f"api calls: {client.calls} (cache hits: {client.cache_hits}); "
      f"candidates scored: {len(rows)}")

  if args.json:
    print(json.dumps({
        "seed": {"kind": kind, "title": title, "tracks": len(seed_ids)},
        "soundcloud_related": rows_to_json(related, "raw", args.limit),
        "crowd_picks": rows_to_json(crowd, "raw", args.limit),
        "up_and_coming": rows_to_json(upcoming, "upcoming", args.limit),
        "rising": rows_to_json(rising, "rising", args.limit),
        "held_out": held_out,
    }, indent=1))
  else:
    print(f"Seed {kind}: {title} ({len(seed_ids)} track(s))")
    print(f"Lists scanned: {len(rec.contexts)}   candidates: {len(rows)}")
    print("columns: score is relative to the top row; lists = scanned lists containing "
          "the track; ppl = distinct people behind those lists; seeds = most seed tracks "
          "sharing one list with it")
    print_table("SoundCloud recommended (baseline)", related, args.limit, "raw", args.urls)
    print_table("Crowd picks (raw co-occurrence)", crowd, args.limit, "raw", args.urls)
    mp, mf = rec.upcoming_thresholds
    print_table(f"Up and coming (<= {fmt_num(mp)} plays, artist <= {fmt_num(mf)} followers)",
                upcoming, args.limit, "upcoming", args.urls)
    if args.neighbours > 0:
      print_table(f"Rising (recently adopted by {n_neighbours} taste neighbours; "
                  f"last = days since newest like/repost, speed = plays/day vs pool)",
                  rising, args.limit, "rising", args.urls)

  if args.clusters == "auto" and not args.json:
    linked, loose = rec.cluster_seeds()
    if loose or len(linked) > 1:
      all_seeds = list(rec.seed_ids)

      def label(t):
        return f"{rec.tracks[t].title[:28]} ({rec.tracks[t].artist[:14]})" if t in rec.tracks else str(t)

      print()
      print(f"== Taste clusters: {len(linked)} linked group(s) and {len(loose)} unlinked "
            f"seed(s) among {len(all_seeds)} seeds")
      for ci, group in enumerate(linked, 1):
        rec.seed_ids = group
        names = ", ".join(label(t) for t in group[:3])
        more = f" +{len(group) - 3} more" if len(group) > 3 else ""
        print()
        print(f"--- Cluster {ci}: {len(group)} seeds: {names}{more}")
        c_rows = rec.score()
        _, c_up = rec.lists(c_rows)
        # Clusters reuse the neighbours already collected; a small snowball
        # keeps the per-cluster cost to a fraction of the main pass.
        c_rising, _ = rec.rising(c_rows, budget_scale=args.cluster_snowball_scale)
        print_table("Rising", c_rising, args.cluster_limit, "rising", args.urls)
        print_table("Up and coming", c_up, args.cluster_limit, "upcoming", args.urls)
      # Seeds that share nothing with the rest get a cheap pass of their own:
      # a few first-round neighbours, no snowball.
      for t in loose[:args.max_loose_seeds]:
        rec.seed_ids = [t]
        print()
        print(f"--- Unlinked seed: {label(t)}")
        c_rows = rec.score()
        c_rising, _ = rec.rising(c_rows, budget_scale=0.0)
        print_table("Rising", c_rising, args.cluster_limit, "rising", args.urls)
      if len(loose) > args.max_loose_seeds:
        print(f"\n({len(loose) - args.max_loose_seeds} more unlinked seeds not shown; "
              f"raise --max-loose-seeds)")
      rec.seed_ids = all_seeds

  if held_out:
    ho = set(held_out)
    print()
    print(f"== Holdout evaluation: {len(ho)} hidden tracks, recall@{args.limit}")
    for name, lst in (("soundcloud related", related), ("crowd picks", crowd),
                      ("up and coming", upcoming), ("rising", rising)):
      top = [x["track"].id for x in lst[:args.limit]]
      hit = len(ho & set(top))
      anywhere = len(ho & {x["track"].id for x in lst})
      print(f"   {name:<20} {hit:>3}/{len(ho)} in top {args.limit}   "
            f"{anywhere:>3} anywhere in list ({len(lst)})")


if __name__ == "__main__":
  main()
