# Mixtape — Project Overview

A plain-language walkthrough of what Mixtape is, how the base app is built, and
what changed during the bug hunt. This is a companion to `submission.md`
(which has the formal root cause analyses) — think of this file as "explain it
to me like I'm new to the codebase."

---

## 1. What Mixtape Is

Mixtape is a small social music app. Users share songs, rate songs their
friends shared, build collaborative playlists together, and keep a daily
"listening streak." There's no frontend in this starter repo — it's a Flask
JSON API. Everything is exercised through HTTP requests (or directly through
the service functions, which is how the test suite uses it).

---

## 2. How the Base App Is Built

### 2.1 The three-layer structure

```
routes/  →  services/  →  models.py (database)
```

- **`routes/`** — Flask blueprints. Each function here is an HTTP endpoint.
  They do almost nothing themselves: pull data out of the request, call a
  service function, and turn the result into JSON. There's no business logic
  in this layer at all.
- **`services/`** — this is where every decision actually gets made: how a
  streak increments, which songs match a search, who gets notified about
  what. Routes never touch the database directly — they always go through a
  service function.
- **`models.py`** — the SQLAlchemy table definitions and the raw data
  relationships between them.

This split matters for bug hunting: whenever something seemed broken at the
API level, the actual problem was always inside a `services/*.py` file, never
in the routes.

### 2.2 The data model (`models.py`)

Seven things are modeled:

| Model | What it represents |
|---|---|
| `User` | An app user. Tracks `listening_streak` and `last_listened_at` directly on the row — there's no separate "streak" table. |
| `Song` | A shared song (title, artist, album, genre, who shared it). |
| `Tag` | A label like "rap" or "lo-fi". Songs can have many tags. |
| `ListeningEvent` | One row per "user listened to song at this timestamp." This is the raw log that streaks and feeds are both computed from. |
| `Rating` | A user's 1–5 score for a song. One rating per (user, song) pair — enforced by a unique constraint. |
| `Playlist` | A named collection created by a user. |
| `Notification` | A message shown to a user, e.g. "so-and-so rated your song." |

Three **association tables** connect these:

- `friendships` — many-to-many, symmetric (if A is friends with B, both
  directions get inserted).
- `song_tags` — many-to-many between songs and tags.
- `playlist_entries` — many-to-many between playlists and songs, but with
  extra columns: `position` (explicit ordering, not just insertion order),
  `added_by`, and `added_at`.

The `position` column is important — it's *why* playlists have a defined
order rather than relying on whatever order the database happens to return
rows in.

### 2.3 Example data flow: rating a friend's song

This is the flow traced in the README, and it's the cleanest example of how
every layer fits together:

1. Client sends `POST /songs/<song_id>/rate` with `{ "user_id": ..., "score": ... }`.
2. `routes/songs.py::rate()` pulls `user_id` and `score` out of the JSON body,
   validates they're present, and calls `notification_service.rate_song()`.
3. `rate_song()` (in `services/notification_service.py`):
   - Loads the `Song` and the rating `User`.
   - Checks if a `Rating` already exists for this (user, song) pair — if so,
     updates the score; if not, inserts a new one.
   - Commits the rating to the database.
   - If the rater isn't the person who originally shared the song, creates a
     `Notification` for the sharer.
4. Client can later fetch that notification via
   `GET /users/<user_id>/notifications` → `get_notifications()`.

Every feature in the app follows this same shape: route parses input →
service does the real work and touches the database → service optionally
triggers a side effect (like a notification) → route serializes the result.

---

## 3. What I Changed

I found and fixed all five of the app's known issues. Below is what was
happening before, and what the fix does, in plain terms. (Full root-cause
writeups with reproduction steps are in `submission.md` — this section is the
short version.)

### Fix 1 — Listening streaks broke on Sundays

**File:** `services/streak_service.py`

The streak logic increments your streak by 1 if you listened yesterday and
today. That check had an extra condition tacked on: it would only increment
if today *wasn't* a Sunday. Since Python's `date.weekday()` returns `6` for
Sunday, any time the "second consecutive day" happened to be a Sunday, the
code fell through to the "reset to 1" branch instead of incrementing —
even though the user hadn't actually skipped a day.

**Fix:** Removed the Sunday-specific condition. Consecutive-day logic now
applies uniformly regardless of which day of the week it is.

```python
# before
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1

# after
elif days_since_last == 1:
    user.listening_streak += 1
```

### Fix 2 — "Friends Listening Now" showed people from yesterday

**File:** `services/feed_service.py`

The "listening now" feed is supposed to show friends who are *currently*
active. It was filtering listening events using a **rolling 24-hour window**
(`now - timedelta(hours=24)`). That means at 10 AM on Tuesday, anything from
11 AM Monday onward still counted — so a friend who listened last night would
still show up under "Listening Now" the next morning.

**Fix:** Changed the cutoff to the start of the current calendar day (UTC
midnight) instead of a sliding 24-hour window, so the feed only reflects
today's activity.

```python
# before
cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD  # rolling 24h

# after
now = datetime.now(timezone.utc)
cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)  # start of today
```

### Fix 3 — Songs with multiple tags showed up multiple times in search

**File:** `services/search_service.py`

The search query joined the `Song` table to the `song_tags` association
table, even though tags were never used to filter or sort the results — they
were only needed afterward, for display. In SQL, joining to a table where a
song can have several matching rows (several tags) produces one result row
*per tag*. A song with 3 tags came back 3 times.

**Fix:** Removed the unnecessary join. Tags are already available through the
`Song.tags` relationship, which `Song.to_dict()` uses directly — the join
was serving no purpose other than duplicating rows.

```python
# before
db.session.query(Song).outerjoin(song_tags, Song.id == song_tags.c.song_id).filter(...)

# after
db.session.query(Song).filter(...)
```

### Fix 4 — No notification when a friend rates your song

**File:** `services/notification_service.py`

`add_to_playlist()` and `rate_song()` are structurally parallel functions —
both should notify the original sharer when someone else interacts with
their song. `add_to_playlist()` did this correctly. `rate_song()` saved the
rating and returned, but never called `create_notification()` at all. This
wasn't a typo in an existing line — the notification step was simply never
written for the rating flow.

**Fix:** Added the same notification pattern used in `add_to_playlist()`:
after saving the rating, if the rater isn't the song's original sharer,
create a `song_rated` notification for them.

```python
# added after the rating is committed
if song.shared_by != user_id:
    create_notification(
        user_id=song.shared_by,
        notification_type="song_rated",
        body=f"{rater.username} rated your song '{song.title}' {score}/5.",
    )
```

### Fix 5 — The last song in a playlist never showed up

**File:** `services/playlist_service.py`

`get_playlist_songs()` correctly queries every song in the playlist, ordered
by position — but the return statement sliced the list with `[:-1]`, which
drops the last item in Python. A 5-song playlist always came back with only
4 songs, no matter what was actually stored.

**Fix:** Return the full list instead of slicing off the last element.

```python
# before
return [song.to_dict() for song in songs[:-1]]

# after
return [song.to_dict() for song in songs]
```

---

## 4. Testing

Every fix has test coverage:

- `tests/test_streaks.py::test_streak_increments_on_sunday` — Fix 1
- `tests/test_search.py::test_search_no_duplicates_multi_tag_song` (and the
  single-tag / no-tag variants) — Fix 3
- `tests/test_playlists.py::test_playlist_returns_all_songs` and
  `test_playlist_returns_songs_in_order` — Fix 5
- `tests/test_notifications.py` (new file I added) —
  `test_rate_song_notifies_sharer` and `test_rate_own_song_does_not_notify_self`
  — Fix 4, written as a regression test so this exact bug can't silently come
  back

Fix 2 (feed calendar-day filtering) doesn't have a dedicated automated test
in this repo; I verified it by reasoning through the seed data's event
timestamps and confirming the cutoff logic against both sides of the midnight
boundary.

Running `pytest tests/` after all fixes: **15 passed, 0 failed.**
