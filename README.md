# media-scope

`media-scope` is a deterministic Python media-management project with three independent
components:

- `media-scope` identifies an exact released movie, a complete ended television
  series, or the latest conservatively completed season of a returning series through
  the official TMDb v3 API.
- `media-search-tv` consumes an eligible complete-series scope and searches configured
  Jackett indexers for releases whose titles appear to cover the complete series.
- `media-probe-torrents` consumes the ranked Jackett report and asks rTorrent to
  retrieve magnet metadata, stopping after the first live candidate succeeds.

Its scope is intentionally narrow. It does not recommend media, scrape websites,
download complete torrent payloads, inspect torrent file lists, transfer files, or
monitor folders. The Jackett component
classifies only release titles and Torznab metadata; it does not approve a result or
decide whether a work is legally distributable. It may retrieve a small `.torrent`
metainfo response from Jackett solely to calculate its BitTorrent v1 infohash.

## Requirements

- Python 3.12 or newer
- A TMDb API Read Access Token for scope generation
- A running, user-configured Jackett service for complete-series searches
- A running rTorrent instance exposed through a user-secured HTTP(S) XML-RPC gateway
  for live health probes

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

Every item in the final `candidates` array is acquisition-ready in one limited sense:
it has a validated `magnet_uri` containing a valid BitTorrent v1 `xt=urn:btih:`
topic. That magnet is the handoff contract for a future rTorrent component; this
project still does not submit it or start a download. A provisional title match that
cannot produce a usable magnet is moved to `rejected_results` with
`NO_USABLE_MAGNET`.

Magnet resolution runs only after normalization, deduplication, classification, and
provisional ranking. It uses these fallbacks in order:

1. A direct Torznab/Newznab `magneturl` attribute.
2. A magnet already present in a result link, enclosure, GUID, or comments field.
3. A tracker-free magnet constructed from a valid hexadecimal or Base32 BTIH
   infohash.
4. A bounded request to the result's authenticated Jackett download reference. The
   response may be a magnet redirect, plain text, a small HTML link, or a public
   `.torrent` metainfo file.

For a `.torrent` response, the SHA-1 infohash is computed from the exact original raw
bencoded `info` dictionary bytes. No file names, episode coverage, or payload content
are validated. Tracker URLs from the metainfo are not added to the magnet. Explicitly
private torrents are rejected as `PRIVATE_TORRENT_MAGNET_UNSUPPORTED`.

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
    "provisional_candidate_count": 5,
    "magnet_resolution_attempted_count": 5,
    "magnet_resolution_succeeded_count": 3,
    "magnet_resolution_failed_count": 2,
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
      "extra_seasons": [],
      "size_bytes": 81412969851,
      "seeders": 100,
      "score": 96,
      "source_indexers": ["example-indexer"],
      "infohash": "0123456789abcdef0123456789abcdef01234567",
      "magnet_uri": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=The%20Good%20Place%20S01-S04%20Complete%201080p",
      "magnet_source": "torznab_infohash"
    }
  ],
  "warnings": []
}
```

`magnet_source` identifies which fallback produced the final URI:
`torznab_magneturl`, `result_field`, `torznab_infohash`, `jackett_redirect`,
`jackett_plain_text`, `jackett_html`, or `jackett_torrent_file`.

`content_verified: false` is always emitted. Reading the bencoded metadata required to
derive an infohash does not verify the torrent's file contents or that it actually
contains the expected seasons. Candidate status means only that the release title and
Torznab metadata passed deterministic MVP rules and that a usable BTIH magnet was
obtained.

Jackett API-key parameters are retained only in the resolver's internal request
reference. They are removed from serialized URLs, warnings, and logs. Magnets
constructed from infohashes or metainfo are tracker-free, so private tracker
credentials are not exposed.

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

## Live rTorrent metadata validation (Step 5)

`media-probe-torrents` is a live swarm-health gate after the title-based Jackett
ranking. It processes accepted candidates in ascending original rank, temporarily
starts each magnet, and selects the first one for which rTorrent retrieves BitTorrent
metadata before the deadline. A newly submitted magnet must also become active within
ten seconds; an inactive torrent fails as `TORRENT_NOT_ACTIVE` rather than consuming
the metadata timeout. That is the complete MVP health rule.

The indexer's `seeders` value remains in the report as historical source metadata. It
is not added to, equated with, or substituted for rTorrent's live connected-peer
statistics. Metadata retrieval shows that the magnet and swarm were usable enough to
deliver metainfo at that moment. It does **not** prove that every payload byte remains
available or that the full torrent will complete.

This step deliberately does not inspect torrent file names, season coverage, episode
coverage, or payload content. It trusts Step 4's release-title classification. A small
amount of payload data may arrive between metadata retrieval and the immediate stop;
zero payload bytes cannot be guaranteed.

### rTorrent RPC configuration

Expose rTorrent XML-RPC through a properly secured HTTP or HTTPS gateway and configure
the complete endpoint. The program never assumes or appends `/RPC2` and does not
connect directly to a public SCGI port.

```dotenv
RTORRENT_RPC_URL=https://rtorrent.example.internal/custom/RPC
RTORRENT_RPC_USERNAME=
RTORRENT_RPC_PASSWORD=
RTORRENT_RPC_VERIFY_TLS=true
RTORRENT_RPC_TIMEOUT_SECONDS=15
RTORRENT_PROBE_DIRECTORY=/srv/rtorrent/media-probes
RTORRENT_PROBE_MAX_CANDIDATES=10
RTORRENT_METADATA_TIMEOUT_SECONDS=300
RTORRENT_METADATA_POLL_INTERVAL_SECONDS=5
RTORRENT_PREFLIGHT_MAGNET=
RTORRENT_PREFLIGHT_TIMEOUT_SECONDS=120
```

Authentication is optional. When either username or password is set, HTTP Basic
authentication is used. Credentials must be supplied in their dedicated settings,
not embedded in the URL. The endpoint is sanitized in reports, and passwords and
authentication headers are never logged. HTTPS certificates are verified by default.
Setting `RTORRENT_RPC_VERIFY_TLS=false` is intended only for controlled local testing
and produces a warning.

At connection time the tool discovers `system.client_version`,
`system.library_version`, and `system.api_version` where available. It inspects the
connected instance's method list, prefers `load.start_verbose` or `load.start`, and
falls back to `load.normal_verbose` or `load.normal` followed by `d.start`. If none of
those compatible paths exists, it returns `MAGNET_SUBMISSION_UNSUPPORTED`; it never
automates ruTorrent in a browser. Metadata detection prefers `d.is_meta == 0`. Older
instances may use the explicitly reported `compatibility_fallback` based on regular
torrent size/file-count fields.

Step 5 runs from the desktop but manages its temporary folders on the seedbox through
the same secured XML-RPC gateway. The gateway must expose `execute.capture` and
`execute.throw`, in addition to the normal torrent methods. The program invokes fixed
`mkdir`, `realpath`, `stat`, `test`, `rm`, and `rmdir` executables with separately
validated arguments; it never sends a shell command string.

Check authentication, versions, required methods, and seedbox probe-directory access
without adding a torrent:

```powershell
python -m media_scope.probe_torrents check-connection --pretty
```

### Probe-directory safety

`RTORRENT_PROBE_DIRECTORY` must be an absolute, dedicated POSIX path on the
rTorrent/seedbox host. It must already exist, be writable by rTorrent, and must not be
`/`, a filesystem root, a home directory, or the normal completed-download directory.
It does not need to be visible to the Windows desktop. Each command creates an
owner-only seedbox directory such as:

```text
/srv/rtorrent/media-probes/probe-20260731T120000Z-a1b2c3d4/<infohash>/
```

Release titles are never used as path components. Failed, script-created torrents are
stopped and erased from rTorrent, then only their verified infohash child directory is
removed through the RPC gateway. rTorrent erase is not assumed to delete data.
Preexisting torrents and their files are never stopped, retagged, redirected, or
erased. The shared probe root is never removed. `--keep-failed-probes` intentionally
disables failed-probe cleanup for debugging and is dangerous.

### Optional infrastructure preflight

`RTORRENT_PREFLIGHT_MAGNET` may contain a user-supplied, known-good legal magnet. The
tool probes and removes that item before evaluating candidates (unless it was already
present). If preflight metadata cannot be retrieved, the command returns
`RTORRENT_NETWORK_UNHEALTHY`, exits with code 5, and does not classify any candidate as
unhealthy. No test magnet is built into this repository. Without a preflight, probing
continues with a warning that candidate timeouts cannot be cleanly distinguished from
a general DHT, tracker, firewall, or rTorrent networking problem.

Use `--skip-preflight` to bypass a configured magnet for one run.

### Probe commands and output

Validate the input and planned order without contacting rTorrent:

```powershell
python -m media_scope.probe_torrents `
  --search-results search-results.json `
  --max-candidates 10 `
  --dry-run `
  --pretty
```

Run the live gate:

```powershell
python -m media_scope.probe_torrents `
  --search-results search-results.json `
  --output health-results.json `
  --timeout-seconds 300 `
  --poll-interval-seconds 5 `
  --pretty
```

The installed `media-probe-torrents` console script is equivalent. JSON is always
printed to standard output, and `--output` receives the same bytes. Logs go only to
standard error. An abbreviated successful report is:

```json
{
  "schema_version": 1,
  "result": "candidate_health_validated",
  "job_id": "probe-20260731T120000Z-a1b2c3d4",
  "preflight": {"status": "NOT_CONFIGURED", "elapsed_seconds": 0},
  "policy": {
    "metadata_timeout_seconds": 300,
    "poll_interval_seconds": 5,
    "maximum_candidates": 10,
    "content_validation_performed": false,
    "stop_after_first_healthy": true
  },
  "attempts": [
    {
      "original_rank": 1,
      "validated_rank": null,
      "infohash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "status": "METADATA_TIMEOUT",
      "metadata_retrieved": false,
      "cleanup_performed": true
    },
    {
      "original_rank": 2,
      "validated_rank": 1,
      "infohash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "status": "METADATA_RETRIEVED",
      "metadata_retrieved": true,
      "metadata_detection_method": "d.is_meta",
      "cleanup_performed": false
    }
  ],
  "selected_candidate": {
    "original_rank": 2,
    "validated_rank": 1,
    "infohash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "rtorrent_state": "stopped",
    "status": "READY_FOR_DOWNLOAD"
  }
}
```

The selected script-created torrent is stopped, retained in rTorrent, and tagged
`media_probe_state=validated_waiting_for_download` so a future download controller can
resume the same item. Preexisting healthy torrents are selected without being changed
and are reported as `preexisting_unchanged`. Lower-ranked candidates are not claimed
to be less healthy; they are listed as unattempted. If every attempted candidate
fails, the result is `NO_HEALTHY_TORRENT_FOUND` and exit code 6.

### rTorrent probe troubleshooting

- **Authentication failure:** confirm the gateway accepts Basic authentication at the
  exact configured URL. Use `check-connection`; do not put credentials in the URL.
- **Metadata timeout:** peer counts are diagnostic only. Confirm rTorrent has working
  DNS, outbound tracker access, DHT/UDP connectivity where applicable, and firewall
  rules. A configured known-good preflight separates infrastructure failure from a
  candidate-specific timeout.
- **Torrent not active:** the seedbox could not start the submitted magnet. Run
  `check-connection`, then confirm the configured remote probe directory exists and is
  writable by rTorrent.
- **Preflight failure:** fix the rTorrent host's network, tracker, DHT, proxy, or
  gateway configuration before retrying candidates. Candidates are not blamed.
- **Unsupported magnet submission:** inspect the `check-connection` method report and
  expose a supported `load.start*` or `load.normal*` RPC method. Browser automation and
  ruTorrent UI fallbacks are intentionally unsupported.
- **Cleanup failure:** exit code 7 requires operator attention. Use the job ID and
  shortened hashes in stderr logs, then inspect only that job's dedicated directory
  and named `media_probe_*` custom fields.

## Full rTorrent payload download (Step 6)

`media-download-torrent` consumes Step 5's successful JSON result and resumes the
exact validated rTorrent item through full payload completion. It never submits the
magnet again, never selects a fallback candidate, never erases the torrent, and never
transfers payload files to the local PC. Step 7 will perform the SFTP transfer using
the verified remote paths returned here.

The handoff must have `result=candidate_health_validated` and a
`selected_candidate` whose `status` is `READY_FOR_DOWNLOAD`. The candidate's
`infohash`, magnet BTIH, and `rtorrent_hash` must all identify the same torrent. Step
6 then queries `d.hash` (or the compatible `d.get_hash`) for the existing item. A
missing item returns `SELECTED_TORRENT_NOT_FOUND` and requires Step 5 to be rerun;
Step 6 does not silently re-add it.

### Download configuration

Step 6 reuses Step 5's `RTORRENT_RPC_*` settings and adds:

```dotenv
RTORRENT_DOWNLOAD_DIRECTORY=/srv/rtorrent/completed-media-downloads
RTORRENT_MIN_FREE_SPACE_BYTES=10737418240
RTORRENT_DOWNLOAD_POLL_INTERVAL_SECONDS=30
RTORRENT_STALL_TIMEOUT_SECONDS=1800
RTORRENT_DOWNLOAD_TIMEOUT_SECONDS=0
RTORRENT_POST_COMPLETION_POLICY=stop
RTORRENT_POST_PROCESS_GRACE_SECONDS=30
RTORRENT_ALLOWED_FINAL_ROOTS=/srv/media/movies,/srv/media/tv
```

- `RTORRENT_DOWNLOAD_DIRECTORY` is a dedicated absolute server-side root. It cannot
  be a filesystem root, the user's home, or overlap `RTORRENT_PROBE_DIRECTORY`.
- `RTORRENT_MIN_FREE_SPACE_BYTES` is the reserve that must remain in addition to the
  torrent's remaining bytes. The default is `0`; configure a production reserve.
- `RTORRENT_DOWNLOAD_POLL_INTERVAL_SECONDS` defaults to 30 seconds.
- `RTORRENT_STALL_TIMEOUT_SECONDS` defaults to 1800 seconds without useful progress.
- `RTORRENT_DOWNLOAD_TIMEOUT_SECONDS=0` disables the overall timeout.
- `RTORRENT_POST_COMPLETION_POLICY` accepts `stop` or `leave_running`. The CLI spelling
  for the latter is `leave-running`.
- `RTORRENT_POST_PROCESS_GRACE_SECONDS` defaults to 30 seconds, during which Step 6
  observes external completion-hook effects.
- `RTORRENT_ALLOWED_FINAL_ROOTS` is a comma-separated allowlist for final paths moved
  outside the download root by trusted post-processing.

The rTorrent process and the Step 6 process must see the configured paths as the same
filesystem paths. Step 6 can check writability for its own process, but deployment
permissions must also allow the rTorrent service account to write there.

### Permanent directory and disk-space safety

The destination is deterministic and sanitized:

```text
<download-root>/<tmdb-id>-<sanitized-title>-<first-8-infohash>/
```

Release text cannot inject separators or path traversal, and the resolved result must
remain beneath the configured root. A preexisting directory is accepted only when
the same rTorrent item or the deterministic `media_download_job_id` proves ownership;
otherwise Step 6 returns `DOWNLOAD_PATH_COLLISION` rather than overwriting data.

Before redirecting a stopped Step 5 torrent, Step 6 checks both rTorrent's completed
byte count and any accessible on-disk bytes at the old base path. The current MVP
does not guess how to relocate partial multi-file payload safely. If meaningful probe
data exists outside the permanent directory, it is preserved and the command returns
`PROBE_DATA_RELOCATION_REQUIRED`. An empty probe location is redirected with
`d.directory.set`, read back for confirmation, and only then started.

Before start, free space must satisfy:

```text
filesystem free bytes >= remaining torrent bytes + configured reserve
```

Already completed bytes reduce the remaining-byte requirement. The same check runs
on every status poll. If space becomes insufficient, the torrent is stopped, its data
is retained, its named state becomes `PAUSED_LOW_DISK_SPACE`, and the command returns
`LOW_DISK_SPACE_DURING_DOWNLOAD`.

### Progress, stalls, timeouts, and restart behavior

Step 6 records explicit state transitions from `PENDING` and input validation through
directory preparation, downloading, hash checking, completion grace, and
`READY_FOR_TRANSFER`. It samples byte counts, remaining bytes, rates, peer counts,
rTorrent messages, hash state, directory, and base path where the connected rTorrent
version supports them.

Only completed-byte growth, remaining-byte reduction, percentage growth, entry into
hash checking, or completion resets the stall timer. Peer-count changes alone are not
progress. At the stall deadline the torrent is stopped without deleting data and the
report distinguishes `NO_CONNECTED_PEERS`, `CONNECTED_BUT_NO_PROGRESS`, or a
`TRACKER_OR_NETWORK_ERROR`. A terminal local rTorrent error is reported separately.
Step 6 does not abandon partial data or fall back to another Jackett candidate.

Rerunning the same Step 5 result derives the same job ID from TMDb ID and infohash:

- an active incomplete item continues to be monitored without a duplicate start;
- a stopped incomplete item is revalidated and resumed;
- an already complete item goes directly through completion verification;
- a prior `STALLED` item requires the explicit `--resume-stalled` option; and
- a missing item requires a fresh Step 5 run.

The optional overall timeout stops and retains an incomplete torrent. A value of zero
means there is no overall deadline.

### Completion hooks, FileBot, and final paths

Some existing rTorrent deployments have an `event.download.finished` hook such as a
FileBot AMC script. Step 6 preserves all existing event hooks and never invokes,
replaces, or removes them. It also never writes `d.custom1`. Ownership and progress
use only these named fields through `d.custom.set`:

```text
media_download_job_id
media_download_state
media_download_source
media_download_tmdb_id
```

After rTorrent reports all bytes complete, zero bytes remaining, no active download
rate, and no hash check or terminal error, Step 6 enters `POST_PROCESSING_GRACE`. It
records the base path before completion, polls during the grace period, and validates
the final base path after any FileBot rename or move. A final path must exist beneath
the download root or one of `RTORRENT_ALLOWED_FINAL_ROOTS`. Unexpected paths return
`FINAL_PATH_OUTSIDE_ALLOWED_ROOT`; missing paths return `FINAL_PATH_NOT_FOUND`.

With policy `stop`, the completed torrent is stopped but retained. With
`leave_running`, an inactive completed torrent is started for seeding. Neither policy
removes the torrent or its files.

### Step 6 commands and JSON

Validate the Step 5 JSON, live torrent identity and metadata, prospective paths, and
disk space without starting, stopping, tagging, or redirecting the torrent:

```powershell
python -m media_scope.download_torrent `
  --health-result health-result.json `
  --dry-run `
  --pretty
```

Resume and monitor the full download:

```powershell
python -m media_scope.download_torrent `
  --health-result health-result.json `
  --output download-result.json `
  --poll-interval-seconds 30 `
  --stall-timeout-seconds 1800 `
  --download-timeout-seconds 0 `
  --post-completion-policy stop `
  --pretty
```

Resume a job that Step 6 previously marked stalled:

```powershell
python -m media_scope.download_torrent `
  --health-result health-result.json `
  --resume-stalled `
  --pretty
```

The installed `media-download-torrent` command is equivalent. JSON is always written
to standard output, `--output` receives the same bytes, and logs go only to standard
error. A shortened, sanitized success result is:

```json
{
  "schema_version": 1,
  "result": "download_completed",
  "job_id": "download-4608-abcdef123456",
  "scope": {"tmdb_id": 4608, "title": "30 Rock"},
  "candidate": {
    "infohash": "abcdef1234567890abcdef1234567890abcdef12",
    "release_title": "30 Rock S01-S07 Complete"
  },
  "storage": {
    "download_directory": "/srv/rtorrent/completed-media-downloads/4608-30-rock-abcdef12",
    "torrent_size_bytes": 81412969851,
    "remaining_bytes": 81412969851,
    "filesystem_free_bytes_at_start": 200000000000,
    "required_reserve_bytes": 10737418240
  },
  "download": {
    "status": "DOWNLOAD_COMPLETED",
    "completed_bytes": 81412969851,
    "remaining_bytes": 0
  },
  "paths": {
    "download_root": "/srv/rtorrent/completed-media-downloads",
    "rtorrent_base_path": "/srv/rtorrent/completed-media-downloads/4608-30-rock-abcdef12/30 Rock",
    "final_base_path": "/srv/media/tv/30 Rock",
    "top_level_paths": ["/srv/media/tv/30 Rock"],
    "path_changed_after_completion": true
  },
  "rtorrent": {"final_state": "stopped", "torrent_retained": true},
  "status": "READY_FOR_TRANSFER",
  "ready_for_transfer": true,
  "warnings": []
}
```

RPC credentials, authentication headers, URL user information, queries, and fragments
are never serialized. The report contains only the sanitized RPC endpoint.

### Step 6 troubleshooting

- **Selected torrent missing:** do not reload the magnet manually through Step 6.
  Rerun Step 5 so torrent identity and metadata health are validated again.
- **Probe relocation required:** inspect the retained Step 5 directory and arrange a
  deliberate same-torrent relocation. No verified bytes were discarded.
- **Insufficient or low disk space:** free space or increase storage, preserving the
  configured reserve. A low-space torrent remains stopped with all payload retained.
- **Stall:** inspect the report's peer and tracker diagnostics. After correcting the
  swarm/network issue, use `--resume-stalled`; otherwise Step 6 will not restart it.
- **Hash check:** 100% is not final while rTorrent reports hashing. A hash-check
  failure is terminal and leaves the data for operator inspection.
- **Final path missing or outside allowed roots:** inspect existing rTorrent/FileBot
  completion logs. Add only trusted, dedicated library roots to the allowlist; do not
  broadly allow a home directory or filesystem root.
- **FileBot move:** ensure the hook updates rTorrent's base path within the configured
  grace period. Increase `RTORRENT_POST_PROCESS_GRACE_SECONDS` if post-processing is
  predictably slower.

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

For `media-probe-torrents`:

| Code | Meaning |
| ---: | --- |
| 0 | Healthy candidate selected, or diagnostic/dry-run completed |
| 2 | Invalid command line or malformed search JSON |
| 3 | Valid search input contains no probeable candidates |
| 4 | rTorrent configuration, authentication, RPC, or method failure |
| 5 | Known-good infrastructure preflight failed |
| 6 | Probe completed but no healthy torrent was found |
| 7 | Cleanup failure requires operator attention |
| 8 | Unexpected internal or output-file failure |

For `media-download-torrent`:

| Code | Meaning |
| ---: | --- |
| 0 | Download completed and verified for transfer, or dry-run passed |
| 2 | Invalid command line or malformed Step 5 JSON |
| 3 | Selected torrent missing, replaced, metadata-only, or invalid |
| 4 | rTorrent configuration, authentication, RPC, or method failure |
| 5 | Unsafe path, collision, relocation requirement, or insufficient disk space |
| 6 | Download stalled |
| 7 | Overall download timeout |
| 8 | Post-processing or final-path validation failure |
| 9 | Unexpected internal or output-file failure |

Every result or error is JSON on standard output. Logging is restricted to standard
error.

## Tests and quality checks

The normal automated suite uses fixtures and mocked HTTP/XML-RPC transports; it makes
no live TMDb, Jackett, or rTorrent requests.

```powershell
python -m ruff format --check .
python -m ruff check .
python -m pytest
```

To apply formatting:

```powershell
python -m ruff format .
```

The live rTorrent integration test is opt-in only. Before enabling it, note that it
temporarily adds, starts, stops, and erases the user-supplied legal magnet. It skips
without modifying anything if that hash already exists:

```powershell
$env:RUN_RTORRENT_INTEGRATION_TESTS = "true"
$env:RTORRENT_INTEGRATION_TEST_MAGNET = "magnet:?xt=urn:btih:<legal-test-hash>"
python -m pytest tests\test_rtorrent_integration.py -v
```

It uses the configured dedicated probe directory and never contains a hard-coded test
magnet.

The full-download integration test is independently opt-in. It requires a successful
Step 5 JSON file for a user-supplied legal, small torrent that already exists in
rTorrent, plus an explicit dedicated destination. It prints its side effects, never
erases a preexisting torrent, and performs no automatic file cleanup:

```powershell
$env:RUN_RTORRENT_DOWNLOAD_INTEGRATION_TESTS = "true"
$env:RTORRENT_DOWNLOAD_INTEGRATION_HEALTH_RESULT = "C:\safe\legal-health-result.json"
$env:RTORRENT_DOWNLOAD_INTEGRATION_DIRECTORY = "C:\safe\rtorrent-download-test"
python -m pytest tests\test_rtorrent_download_integration.py -v -s
```

## Current limitations

- Only the first TMDb search-results page is considered.
- TMDb's default language and region behavior is used.
- Only standard TMDb season ordering is supported.
- Step 6 deliberately returns `PROBE_DATA_RELOCATION_REQUIRED` instead of attempting
  automatic relocation when Step 5 has already written meaningful payload data.
- On-disk checks require the Step 6 process to see rTorrent's server-side paths. A
  remote-only RPC deployment needs Step 6 to run on that server or share its mounts.
- Existing completion hooks must update rTorrent's visible base path within the grace
  period for moved files to be discovered automatically.
- TMDb has no authoritative season-completion status. The returning-series mode is
  intentionally conservative and may select an older season until later-season
  metadata appears.
- Alternate, DVD, absolute, production, and anime-specific ordering are not
  supported.
- Specials and Season 0 cannot be included.
- Jackett matching is deliberately title-only and does not prove the torrent's file
  contents. Fetching `.torrent` metainfo to derive an infohash does not change this.
- Explicitly private `.torrent` files cannot be converted to public magnets in this
  MVP and are rejected.
- Daily-TV date numbering, sports, anime numbering, movies, partial-series
  acquisition, and alternate episode ordering are outside the search MVP.
- Tracker-specific release naming cannot be parsed exhaustively; uncertain results
  are rejected or classified `UNKNOWN`.
- The live gate proves only timely metadata retrieval. It does not verify files,
  seasons, episodes, payload availability, legal status, or eventual completion.
- The rTorrent XML-RPC gateway must permit the fixed remote directory-management
  commands required by Step 5.
- The probe does not scrape trackers directly, make copyright or licensing
  determinations, or intentionally start a full download.

## TMDb attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.

See TMDb's [official attribution requirements](https://developer.themoviedb.org/docs/faq).
