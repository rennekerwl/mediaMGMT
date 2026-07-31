# media-scope

`media-scope` is a small, deterministic Python program that answers one question:
which exact released movie or complete ended television series did the user request?
It resolves metadata through the official TMDb v3 API and emits JSON suitable for a
later program.

Its scope is intentionally narrow. It does not recommend media, scrape websites,
search Jackett or torrent indexes, control download clients, download files, transfer
files, or monitor folders.

## Requirements

- Python 3.12 or newer
- A TMDb API Read Access Token

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

## Usage

Search by title, optionally narrowing by release or first-air year:

```powershell
python -m media_scope movie "The Thing" --year 1982 --pretty
python -m media_scope tv "The Good Place" --year 2016 --pretty
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

## Eligibility and scope rules

- A movie is eligible only when TMDb reports its status as `Released`.
- A TV series is eligible only when TMDb reports its status as `Ended`.
- Returning, in-production, planned, pilot, canceled, and all other TV statuses are
  ineligible.
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

| Code | Meaning |
| ---: | --- |
| 0 | Eligible media resolved |
| 2 | Invalid command line or ambiguous title |
| 3 | Resolved but ineligible under the MVP rules |
| 4 | TMDb authentication, record, network, response, or API failure |
| 5 | Unexpected internal or output-file failure |

Every result or error is JSON on standard output. Logging is restricted to standard
error.

## Tests and quality checks

The automated suite uses fixtures and mocked HTTP transports; it makes no live TMDb
requests.

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
- Alternate, DVD, absolute, production, and anime-specific ordering are not
  supported.
- Specials and Season 0 cannot be included.
- No recommendation, torrent-search, downloading, transfer, or folder-monitoring
  component is implemented.

## TMDb attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.

See TMDb's [official attribution requirements](https://developer.themoviedb.org/docs/faq).
