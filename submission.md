# Mixtape Bug Hunt — Submission

## AI Usage

I used Cursor AI (Claude) throughout this project in the following ways:

- **Codebase orientation:** After reading `README.md` and `models.py` myself, I asked the AI to summarize each service module's responsibilities and trace call chains (e.g., rating a song from route → service → database). This helped me confirm my understanding of the routes-vs-services split before opening any bug files.
- **Understanding Python datetime behavior:** For Issue #1, I had already spotted the `today.weekday() != 6` guard in `update_listening_streak`. I asked the AI to explain the difference between `weekday()` and `isoweekday()` to verify why Sunday (weekday 6) was being treated differently. The AI's explanation matched the Python docs; I confirmed by running `test_streak_increments_on_sunday`.
- **Comparing notification patterns:** For Issue #4, I read `add_to_playlist()` and `rate_song()` side by side myself. The AI helped articulate that the architectural gap was a missing notification call, not a typo — but I verified this by reading both functions directly before changing anything.
- **Where I overrode AI:** The AI initially suggested using `.distinct()` for the search duplicate fix. After reading the query, I removed the unnecessary `outerjoin` entirely instead, since tags are already loaded via the Song relationship in `to_dict()` and the join served no filtering purpose.

---

## Codebase Map

### Main Files and Roles

| File | Role |
|------|------|
| `app.py` | Flask application factory. Creates the app, configures SQLite, initializes SQLAlchemy, registers blueprints under `/songs`, `/playlists`, `/users`, and `/feed`. |
| `models.py` | SQLAlchemy models: `User`, `Song`, `Tag`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`. Association tables: `friendships`, `song_tags`, `playlist_entries` (with explicit `position` column for ordering). |
| `routes/songs.py` | Song search, detail, rating (`POST /songs/<id>/rate`), and listening events (`POST /songs/<id>/listen`). |
| `routes/playlists.py` | Playlist CRUD and adding songs (`POST /playlists/<id>/songs` → `add_to_playlist`). |
| `routes/users.py` | User profiles, streak lookup, and notification retrieval. |
| `routes/feed.py` | "Friends Listening Now" and general activity feed endpoints. |
| `services/*.py` | All business logic. Routes delegate here immediately — they only parse input and format JSON responses. |
| `seed_data.py` | Populates 5 users, 13 songs (0/1/3+ tags), 3 playlists, listening events, and sample notifications. |
| `tests/` | Pytest suite covering streaks, search, playlists, and notifications. |

### Data Flow — Friend Rates a Shared Song

1. Client sends `POST /songs/<song_id>/rate` with `{user_id, score}`.
2. `routes/songs.py` → `rate()` validates input and calls `notification_service.rate_song()`.
3. `rate_song()` loads the `Song` and `User`, upserts a `Rating` record, commits to DB.
4. If the rater is not the original sharer, `create_notification()` inserts a `Notification` for `song.shared_by` with type `song_rated`.
5. Client fetches notifications via `GET /users/<user_id>/notifications` → `get_notifications()`.

### Patterns Noticed

- **Thin routes, fat services:** Every route is 5–15 lines; logic lives exclusively in `services/`.
- **Notifications follow a consistent pattern:** Side-effect functions (`add_to_playlist`, `rate_song`) call `create_notification()` after the primary DB write, but only when the actor is not the song owner.
- **Association tables carry metadata:** `playlist_entries` has `position`, `added_by`, and `added_at` — playlist order is explicit, not insertion-order dependent.

### Bug Fix Plan

I fixed all 5 issues because they were independent, clearly scoped to one line each (except the notification architectural gap), and verifiable with existing or new tests.

---

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** Ran `pytest tests/test_streaks.py::test_streak_increments_on_sunday -v`. The test simulates listening on Saturday 2024-06-15 then Sunday 2024-06-16. Expected streak of 2; got 1 (reset instead of increment).

**How I found the root cause:** Read `README.md` → traced `POST /songs/<id>/listen` → `routes/songs.py` → `streak_service.record_listening_event()` → `update_listening_streak()`. Line 73 had an extra condition: `days_since_last == 1 and today.weekday() != 6`.

**The root cause:** When a user listened on consecutive calendar days and the second day was Sunday (`weekday() == 6`), the `elif` branch failed and execution fell through to the `else` branch, resetting the streak to 1. The streak rules in the docstring say consecutive days should increment — there is no Sunday exception. The `weekday() != 6` guard incorrectly treated Sunday as a week boundary reset.

**Fix and side-effect check:** Removed `and today.weekday() != 6` so `days_since_last == 1` always increments. Ran all 5 streak tests — Monday→Tuesday increment, same-day no double-count, skipped-day reset, and Saturday→Sunday increment all pass.

---

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it:** Read `seed_data.py` — older listening events are seeded at `now - timedelta(hours=2 + i*8)`, which can fall within the previous calendar day but still inside a 24-hour rolling window. Called `get_friends_listening_now()` logic mentally: any event from the last 24 hours qualifies, including yesterday evening's activity.

**How I found the root cause:** `README.md` pointed to `feed_service.py`. Found `RECENT_THRESHOLD = timedelta(hours=24)` and `cutoff = now - RECENT_THRESHOLD`. The feature name "Listening Now" implies today's activity, but the code used a rolling 24-hour window.

**The root cause:** A rolling 24-hour cutoff includes events from yesterday (e.g., Monday 11 PM appears in Tuesday 10 AM's feed). Users expect "today's listening" — calendar-day filtering, not a sliding window.

**Fix and side-effect check:** Replaced rolling window with `cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)` (start of today UTC). Verified `get_activity_feed()` is unchanged — it intentionally returns all recent events without day filtering.

---

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it:** Searched seed data — songs like "Crown Heights Anthem" have 3 tags (`rap`, `hip-hop`, `boom bap`). The search query in `search_service.py` used `.outerjoin(song_tags)`, which produces one SQL row per tag match. A song with 3 tags can appear 3 times in results.

**How I found the root cause:** `README.md` → `search_service.py` → `search_songs()`. The `outerjoin(song_tags)` is not used in the `filter()` clause — title/artist matching doesn't need tags at all. Tags are loaded separately via the `Song.tags` relationship in `to_dict()`.

**The root cause:** The unnecessary `outerjoin` on `song_tags` multiplies result rows for multi-tag songs. Each tag association creates an additional row in the JOIN, so the same `Song` appears once per tag.

**Fix and side-effect check:** Removed the `outerjoin` and unused imports (`Tag`, `song_tags`). Search still returns tags correctly via the ORM relationship. All 5 search tests pass, including `test_search_no_duplicates_multi_tag_song`.

---

### Issue #4 — Notified for playlist add but not for rating

**How I reproduced it:** Compared seed data (has a `song_added_to_playlist` notification) with `rate_song()` behavior — rating a friend's song via `POST /songs/<id>/rate` saves the rating but creates no notification. Checked `GET /users/<sharer_id>/notifications` — no `song_rated` entry.

**How I found the root cause:** Read `notification_service.py`. `add_to_playlist()` calls `create_notification()` after adding the song (lines 65–70). `rate_song()` commits the rating and returns — no notification call. Same file, same pattern, one function complete and one missing the notification step.

**The root cause:** `rate_song()` was implemented as rating-only logic. Unlike `add_to_playlist()`, it never called `create_notification()` for the song's original sharer. This is an architectural omission — the notification side-effect was never wired up, not a typo in an existing call.

**Fix and side-effect check:** Added notification creation after commit, mirroring `add_to_playlist()`: notify `song.shared_by` when `user_id != song.shared_by`. Added `tests/test_notifications.py` with regression tests. Self-rating does not notify.

---

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** Ran `pytest tests/test_playlists.py::test_playlist_returns_all_songs -v`. Playlist seeded with 5 songs; `get_playlist_songs()` returned 4. Missing "Track 5".

**How I found the root cause:** `README.md` → `playlist_service.py` → `get_playlist_songs()`. Line 66: `return [song.to_dict() for song in songs[:-1]]` — Python slice `[:-1` excludes the last element.

**The root cause:** An off-by-one error in the return statement. The query correctly fetches all songs ordered by position, but `songs[:-1]` drops the final song before serialization.

**Fix and side-effect check:** Changed to `songs` (no slice). All 3 playlist tests pass — count, order, and empty playlist.

---

## Regression Test

Added `tests/test_notifications.py` for Issue #4. This test would have caught the missing notification before merge:

- `test_rate_song_notifies_sharer` — verifies a `song_rated` notification is created when a friend rates a shared song.
- `test_rate_own_song_does_not_notify_self` — verifies no self-notification.

---

## Git Log

```
13bed5e fix: return all playlist songs instead of excluding the last entry
21c52f7 fix: notify song sharer when a friend rates their song
dce802f fix: remove tag join that duplicated multi-tag songs in search results
fd1fa04 fix: filter listening-now feed by calendar day instead of rolling 24 hours
9b94741 fix: allow streak increment when listening on consecutive Sunday
```

*(Take a screenshot of `git log --oneline` on the `bugfix/mixtape` branch for portal submission.)*
