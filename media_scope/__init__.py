"""Resolve deterministic acquisition scopes using TMDb metadata."""

from media_scope.client import TmdbClient
from media_scope.resolver import resolve_media
from media_scope.scope_builder import build_movie_scope, build_tv_scope

__all__ = ["TmdbClient", "build_movie_scope", "build_tv_scope", "resolve_media"]
__version__ = "0.1.0"
