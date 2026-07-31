"""Typed internal models for acquisition scopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Genre:
    """A TMDb genre."""

    id: int
    name: str

    def to_dict(self) -> JsonObject:
        """Return the public JSON representation."""
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True, slots=True)
class ScopeWarning:
    """A non-fatal or scope-blocking metadata warning."""

    code: str
    message: str
    season_number: int | None = None
    episode_number: int | None = None
    tmdb_id: int | None = None

    def to_dict(self) -> JsonObject:
        """Return the public JSON representation without empty context fields."""
        value: JsonObject = {"code": self.code, "message": self.message}
        if self.season_number is not None:
            value["season_number"] = self.season_number
        if self.episode_number is not None:
            value["episode_number"] = self.episode_number
        if self.tmdb_id is not None:
            value["tmdb_id"] = self.tmdb_id
        return value


@dataclass(frozen=True, slots=True)
class EpisodeScope:
    """One episode in a complete-series acquisition scope."""

    tmdb_episode_id: int
    season_number: int
    episode_number: int
    name: str | None
    air_date: str | None
    runtime_minutes: int | None

    def to_dict(self) -> JsonObject:
        """Return the public JSON representation."""
        return {
            "tmdb_episode_id": self.tmdb_episode_id,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "name": self.name,
            "air_date": self.air_date,
            "runtime_minutes": self.runtime_minutes,
        }


@dataclass(frozen=True, slots=True)
class SeasonScope:
    """One regular season and all of its TMDb episodes."""

    tmdb_season_id: int
    season_number: int
    name: str | None
    air_date: str | None
    episodes: tuple[EpisodeScope, ...]

    def to_dict(self) -> JsonObject:
        """Return the public JSON representation."""
        return {
            "tmdb_season_id": self.tmdb_season_id,
            "season_number": self.season_number,
            "name": self.name,
            "air_date": self.air_date,
            "episode_count": len(self.episodes),
            "episodes": [episode.to_dict() for episode in self.episodes],
        }
