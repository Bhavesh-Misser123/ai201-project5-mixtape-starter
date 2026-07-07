"""
tests/test_notifications.py — Mixtape

Tests for notification creation when friends interact with shared songs.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_users_and_song(app):
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Test Track", artist="Test Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rate_song_notifies_sharer(app, seed_users_and_song):
    """Rating a friend's shared song should create a song_rated notification."""
    with app.app_context():
        sharer = db.session.get(User, seed_users_and_song["sharer"].id)
        rater = db.session.get(User, seed_users_and_song["rater"].id)
        song = db.session.get(Song, seed_users_and_song["song"].id)

        rate_song(rater.id, song.id, 4)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"
        assert "Test Track" in notifications[0]["body"]
        assert "4/5" in notifications[0]["body"]


def test_rate_own_song_does_not_notify_self(app, seed_users_and_song):
    """Rating your own song should not create a notification for yourself."""
    with app.app_context():
        sharer = db.session.get(User, seed_users_and_song["sharer"].id)
        song = db.session.get(Song, seed_users_and_song["song"].id)

        rate_song(sharer.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert notifications == []
