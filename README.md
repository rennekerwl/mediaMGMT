# media-scope

`media-scope` is a deterministic Python media-management project with two independent
components:

- `media-scope` identifies an exact released movie, a complete ended television
  series, or the latest conservatively completed season of a returning series through
  the official TMDb v3 API.
- `media-search-tv` consumes an eligible complete-series scope and searches configured
  Jackett indexers for releases whose titles appear to cover the complete series.

Its scope is intentionally narrow. It does not recommend media, scrape websites,
control download clients, download torrents, follow magnet links, inspect torrent file
lists, transfer files, or monitor folders. The Jackett component classifies only
release titles and Torznab metadata; it does not approve a result or decide whether a
work is legally distributable.

## Requirements

- Python 3.12 or newer
- A TMDb API Read Access Token for scope generation
- A running, user-configured Jackett service for complete-series searches

## Installation

Create and activate a virtual environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On macOS or Linux, activate the environment with:

```bash
source .venv/bin/activate
```

## TMDb authentication

Create a TMDb account, request API access from the API section of your TMDb account
settings, and copy the **API Read Access Token**. This is the long bearer token, not
the shorter v3 API key.

Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

Then replace the placeholder:

```dotenv
TMDB_BEARER_TOKEN=replace_with_your_tmdb_read_access_token
```

The `.env` file is ignored by Git. The token may instead be set directly in the
process environment. It is sent only as an `Authorization: Bearer` header and is
never logged.

## Jackett configuration

Install and configure Jackett separately, add the indexers you intend to use, and
confirm they pass Jackett's own test. The API key is shown in the Jackett dashboard;
copy it from the **API Key** field.

Configure the local base URL and key in `.env`:

```dotenv
JACKETT_URL=http://127.0.0.1:9117
JACKETT_API_KEY=replace_with_your_jackett_api_key
JACKETT_INDEXERS=
MEDIA_SEARCH_MIN_SEEDERS=0
```

`JACKETT_URL` accepts a trailing slash and can include a reverse-proxy path.
`JACKETT_INDEXERS` is an optional comma-separated list of configured indexer IDs.
When it is empty, the search tool asks Jackett for all configured indexers. The API
key is never written to result JSON or logs, and Jackett API-key parameters are
removed from serialized result URLs.

`MEDIA_SEARCH_MIN_SEEDERS` is a ranking preference, not an acceptance filter. A
correctly scoped zero-seeder release remains a candidate.

## Usage

Search by title, optionally narrowing by release or first-air year:

```powershell
python -m media_scope movie "The Thing" --year 1982 --pretty
python -m media_scope tv "The Good Place" --year 2016 --pretty
python -m media_scope tv "The Simpsons" --year 1989 --latest-complete-season --pretty
```

Bypass search when the exact TMDb record is already known:

```powershell
python -m media_scope tv --tmdb-id 66573
python -m media_scope movie --tmdb-id 1091
```

Use `--output result.json` to write exactly the same JSON that is printed to standard
output. Use `--verbose` for informational diagnostics on standard error.

A released movie result has this shape:

```json
{
  "schema_version": 1,
  "result": "resolved",
  "eligible": true,
  "media_type": "movie",
  "scope_type": "single_movie",
  "tmdb_id": 1091,
  "title": "The Thing",
  "original_title": "The Thing",
  "release_date": "1982-06-25",
  "release_year": 1982,
  "status": "Released",
  "runtime_minutes": 109,
  "original_language": "en",
  "genres": [{"id": 27, "name": "Horror"}],
  "scope": {"movie_count": 1},
  "warnings": []
}
```

Values always come from the current TMDb response; the example is illustrative.

## Complete-series Jackett search

First generate the complete ended-series scope:

```powershell
python -m media_scope tv "The Good Place" --year 2016 --pretty --output completed-show.json
```

Then search Jackett:

```powershell
python -m media_scope.search_tv `
  --scope completed-show.json `
  --output search-results.json `
  --pretty
```

The installed console script is equivalent:

```powershell
media-search-tv --scope completed-show.json --pretty
```

Useful search options:

```text
--indexer INDEXER_ID   Restrict searching; may be supplied more than once.
--fresh                Add Jackett's cache=false option.
--include-rejected     Include complete details for rejected releases.
--max-rejected N       Limit rejected details (default 25; 0 omits them).
--min-seeders N        Override the ranking-only seeder preference.
--verbose              Send search diagnostics to standard error.
```

The saved fixture can be used as an example scope:

```powershell
media-search-tv `
  --scope tests\fixtures\scope_inputs\completed_tv.json `
  --indexer your-configured-indexer-id `
  --pretty
```

Every generated query and issued indexer request is recorded in the report. Jackett
indexers are queried independently, so one failed indexer does not discard successful
results from another.

An abbreviated successful report looks like:

```json
{
  "schema_version": 1,
  "result": "search_completed",
  "media_type": "tv",
  "scope": {
    "tmdb_id": 66573,
    "title": "The Good Place",
    "expected_seasons": [1, 2, 3, 4],
    "expected_season_count": 4
  },
  "search": {
    "raw_result_count": 50,
    "deduplicated_result_count": 35,
    "accepted_candidate_count": 3
  },
  "candidates": [
    {
      "rank": 1,
      "classification": "COMPLETE_SERIES",
      "accepted_as_candidate": true,
      "content_verified": false,
      "original_title": "The.Good.Place.S01-S04.Complete.1080p.WEB-DL.x265",
      "detected_seasons": [1, 2, 3, 4],
      "missing_seasons": [],
      "extra_seasons": []
    }
  ],
  "warnings": []
}
```

`content_verified: false` is always emitted because this component does not download
or inspect `.torrent` metadata or its file list. Candidate status means only that the
release title and Torznab metadata passed deterministic MVP rules.

### Release classifications

| Classification | Meaning |
| --- | --- |
| `COMPLETE_SERIES` | Strong complete-series phrase or exact expected-season coverage |
| `COMPLETE_SERIES_CANDIDATE` | Plausible but conflicting, extra, or less precise coverage |
| `MULTI_SEASON_PACK` | Multiple seasons that do not cover the expected series |
| `SINGLE_SEASON_PACK` | One season; accepted only for a matching one-season series |
| `SINGLE_EPISODE` | One explicit episode token |
| `MULTI_EPISODE_PARTIAL` | Several explicit episode tokens |
| `MOVIE_OR_UNRELATED` | Title or category contradicts the intended series |
| `ANIME` | Explicit anime category or conservative anime release signal |
| `UNKNOWN` | Title-only evidence is insufficient to classify safely |

Individual episodes, partial multi-episode releases, anime, sports, inappropriate
non-TV categories, and releases with known missing seasons are rejected. Seeder count
cannot make an incomplete result acceptable.

## Eligibility and scope rules

- A movie is eligible only when TMDb reports its status as `Released`.
- A TV series is eligible only when TMDb reports its status as `Ended`.
- Returning, in-production, planned, pilot, canceled, and all other TV statuses are
  ineligible in the default mode.
- `--latest-complete-season` is an explicit TV-only mode that accepts only
  `Returning Series` and returns exactly one season.
- A returning season is considered complete only when every listed episode has a
  valid air date on or before today and TMDb provides evidence of a later regular
  season. That evidence can be a higher-numbered season summary or
  `next_episode_to_air` belonging to a higher season.
- When the newest season lacks later-season evidence, the mode conservatively falls
  back to an older provably completed season.
- Season 0 and specials are excluded.
- Every episode from each included regular-season detail response is included in
  standard TMDb season and episode order.
- Counts are calculated from those included records rather than copied from top-level
  totals.

Incomplete dates and runtimes produce warnings rather than invented values. Metadata
that makes episode identity or ordering unreliable produces `SCOPE_INCOMPLETE`.

Title searches are normalized for common case, whitespace, and punctuation
differences. The program selects a result only when one normalized title/original
title and requested year match is unique. Otherwise it prints candidate records,
exits with code 2, and asks the caller to rerun with `--tmdb-id`.

## Exit codes

For `media-scope`:

| Code | Meaning |
| ---: | --- |
| 0 | Eligible media resolved |
| 2 | Invalid command line or ambiguous title |
| 3 | Resolved but ineligible under the MVP rules |
| 4 | TMDb authentication, record, network, response, or API failure |
| 5 | Unexpected internal or output-file failure |

For `media-search-tv`:

| Code | Meaning |
| ---: | --- |
| 0 | Search completed, including zero-result or zero-candidate searches |
| 2 | Invalid command line, file, JSON, or scope structure |
| 3 | Valid but unsupported or ineligible scope |
| 4 | Jackett configuration, authentication, discovery, or total indexer failure |
| 5 | Unexpected internal or output-file failure |

Every result or error is JSON on standard output. Logging is restricted to standard
error.

## Tests and quality checks

The automated suite uses fixtures and mocked HTTP transports; it makes no live TMDb
or Jackett requests.

```powershell
python -m ruff format --check .
python -m ruff check .
python -m pytest
```

To apply formatting:

```powershell
python -m ruff format .
```

## Current limitations

- Only the first TMDb search-results page is considered.
- TMDb's default language and region behavior is used.
- Only standard TMDb season ordering is supported.
- TMDb has no authoritative season-completion status. The returning-series mode is
  intentionally conservative and may select an older season until later-season
  metadata appears.
- Alternate, DVD, absolute, production, and anime-specific ordering are not
  supported.
- Specials and Season 0 cannot be included.
- Jackett matching is deliberately title-only and does not prove the torrent's file
  contents.
- Daily-TV date numbering, sports, anime numbering, movies, partial-series
  acquisition, and alternate episode ordering are outside the search MVP.
- Tracker-specific release naming cannot be parsed exhaustively; uncertain results
  are rejected or classified `UNKNOWN`.
- Search does not download anything, submit to rTorrent, follow magnets, scrape
  trackers directly, make copyright or licensing determinations, or automatically
  approve a torrent.

## TMDb attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.

See TMDb's [official attribution requirements](https://developer.themoviedb.org/docs/faq).
