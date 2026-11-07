#!/usr/bin/env python3
"""
NTA GTFS-Realtime collector
===========================

Fetches the National Transport Authority's two GTFS-Realtime feeds, vehicle positions
and trip updates, at the start of every minute (UTC), and keeps every response exactly
as received (gzipped protobuf), so nothing is ever lost. Both feeds are requested at the
same moment, each with its own API key, and saved under the same fetch time, so each
pair is one snapshot of the network. Turning the responses into tables is a separate
job, the builder: this script only collects, so it can run untouched for months.

It also keeps every version of the static timetable (routes, stops, trips, stop times,
shapes): it checks in the background at start-up and every hour, and keeps each new
version as the zip the NTA published, recorded in versions.csv. The NTA regenerates
trip_ids whenever it uploads new schedules, so live data only matches the version that was
in use when it was collected. Unpacking the right version is the builder's job too.

It runs in Docker, with no arguments. GitHub Actions publishes an image for each release
(ghcr.io/<owner>/nta-collector), which the server runs; docker-compose.yml builds and runs
it locally for testing (see README.md). The log's first line gives the release the image
was built from, or "dev" for a local build.

Configuration (environment variables, set in collector.env)
-----------------------------------------------------------
  NTA_KEY_TRIPS        required. Your primary key from developer.nationaltransport.ie,
                       used for trip updates.
  NTA_KEY_VEHICLES     required. Your secondary key, used for vehicle positions. The NTA
                       allows each key one call a minute, so each feed needs its own.
  NTA_HEALTHCHECK_URL  optional. Your healthchecks.io ping URL (see "Health checks").
  NTA_BASE_URL         optional. Default https://api.nationaltransport.ie/gtfsr/v2
  NTA_STATIC_URL       optional. Default https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip

Schedule
--------
Rounds start on the minute, so snapshots line up with the clock, across restarts too.
The schedule never changes: a failed round is a gap in the data, never a reason to fetch
differently. If the API answers 429 with a Retry-After, the rounds inside that wait are
skipped, and the next round is back on the minute.

Health checks
-------------
Each response is decoded just far enough to check it, and the log shows how many
entities it holds, how old the feed is and its size:

  vehicles     1,180 entities, feed 25s old, 212 KB

The log warns when a response isn't a valid GTFS-RT feed (it's still saved, so you can
look at it), when a feed has no entities, and when a feed is more than 10 minutes old,
which means the NTA has stopped updating it. An empty or old feed is logged once when it
starts and once when it clears, not every minute.

With NTA_HEALTHCHECK_URL set, each round in which both feeds were saved and decoded
checks in with healthchecks.io, with that round's log lines attached. From the third
failed round in a row, each failed round is reported to healthchecks.io as a failure,
with what went wrong, so you're alerted within minutes. If the reports stop altogether
(the server or the collector is down), healthchecks.io alerts you once the check's grace
time runs out. An empty or old feed still counts as a good round: the collector is
working, and the problem is on the NTA's side.

Stopping
--------
`docker stop` (SIGTERM) stops the collector straight away if it's waiting for the next
round, otherwise once the round in progress has been saved and reported, so no file is
left half-written.

Output layout
-------------
  /data/raw/<endpoint>/<YYYY-MM-DD>/<HHMMSS>.pb.gz     every response, as received. The folder date and
                                                       file name are the fetch time (UTC), and both feeds
                                                       of a round share the same name
  /data/static/GTFS_Realtime_<YYYYMMDD_HHMMSS>.zip     every static timetable version, as published (named by
                                                       UTC download time)
  /data/static/versions.csv                            one row per version: when it was downloaded and
                                                       published, its ETag, SHA-256 and size. The builder uses
                                                       it to pick the version in use for each snapshot

/data is the container's data folder; docker-compose.yml mounts the server's data/ folder there.
"""

import csv
import datetime as dt
import email.utils
import gzip
import hashlib
import logging
import math
import os
import signal
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import requests
from google.transit import gtfs_realtime_pb2 as rt

# ---------------------------------------------------------------- configuration

BASE_URL = (os.environ.get("NTA_BASE_URL") or "https://api.nationaltransport.ie/gtfsr/v2").rstrip("/")
ENDPOINTS = {  # the two feeds, fetched together every round
    "vehicles": f"{BASE_URL}/Vehicles",
    "trip_updates": f"{BASE_URL}/TripUpdates",
}
STATIC_URL = os.environ.get("NTA_STATIC_URL") or "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
HEALTHCHECK_URL = os.environ.get("NTA_HEALTHCHECK_URL", "").strip()
KEY_HEADER = "x-api-key"         # the header the NTA's API reads the key from
VERSION = os.environ.get("NTA_COLLECTOR_VERSION") or "dev"   # the release, set by the image build
USER_AGENT = f"nta-gtfsr-collector/{VERSION} (personal research project)"
DATA_DIR = Path("/data")         # the container's data folder, mounted from the server

ROUND_SECONDS = 60               # a round every minute, on the minute: each key's fair-use limit
FETCH_TIMEOUT = 30               # seconds to wait for each feed
STALE_AFTER = 600                # warn when a feed's own timestamp is more than this many seconds old
FAILED_ROUNDS_BEFORE_ALERT = 3   # failed rounds in a row before healthchecks.io hears about it
HEALTHCHECK_TIMEOUT = 10         # seconds to wait for healthchecks.io

STATIC_CHECK_HOURS = 1           # how often to look for a new static timetable
NO_VALIDATOR_HOURS = 6           # how often instead, if the server can't say "unchanged"
STATIC_RETRY_MINUTES = 15        # wait after a failed timetable check
# A downloaded timetable is only kept if it has the files live data joins to.
REQUIRED_GTFS_FILES = {"routes.txt", "stops.txt", "trips.txt", "stop_times.txt"}
# The layout of static/versions.csv, which the builder reads.
VERSION_COLUMNS = ["downloaded_utc", "file", "published_utc", "etag", "sha256", "size_bytes"]

log = logging.getLogger("nta")


# ---------------------------------------------------------------- helpers

def mask(key: str) -> str:
    """Show only the end of a key in logs."""
    return f"key ...{key[-4:]}"


def fmt_size(n: int) -> str:
    if n < 1000:
        return f"{n} bytes"
    if n < 1_000_000:
        return f"{n / 1000:.0f} KB"
    return f"{n / 1e6:.1f} MB"


def fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 120:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def hhmm(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%H:%M")


def describe_error(exc: Exception) -> str:
    """A short reason for a failed request, leaving out the URL (a ping URL is a secret)."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None):
        return f"HTTP {response.status_code}"
    return str(exc) if isinstance(exc, ValueError) else type(exc).__name__


# ---------------------------------------------------------------- keys and fetching

def load_keys() -> dict[str, str]:
    """Each feed's key. Both feeds are fetched at the same moment, and the NTA allows each
    key one call a minute, so each feed needs its own."""
    names = {"vehicles": "NTA_KEY_VEHICLES", "trip_updates": "NTA_KEY_TRIPS"}
    keys = {ep: os.environ.get(name, "").strip() for ep, name in names.items()}
    unset = [names[ep] for ep, key in keys.items() if not key or key.startswith("paste-your")]
    if unset:
        sys.exit(f"Put your keys in collector.env first: {' and '.join(unset)} "
                 f"{'is' if len(unset) == 1 else 'are'} not set.")
    if keys["vehicles"] == keys["trip_updates"]:
        sys.exit("NTA_KEY_TRIPS and NTA_KEY_VEHICLES hold the same key. Each feed needs its own "
                 "(your primary and secondary keys), because both are fetched at the same moment.")
    return keys


class Fetcher:
    def __init__(self, keys: dict[str, str], session_factory=requests.Session):
        self.keys = keys
        # A connection pool per feed, so the two requests can run side by side.
        self.sessions = {ep: session_factory() for ep in ENDPOINTS}

    def get(self, endpoint: str) -> bytes:
        resp = self.sessions[endpoint].get(ENDPOINTS[endpoint], timeout=FETCH_TIMEOUT,
                                           headers={KEY_HEADER: self.keys[endpoint], "User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.content


def fetch_round(fetcher: Fetcher) -> dict[str, bytes | Exception]:
    """Request both feeds at the same moment, one thread each. Returns each feed's
    response body, or the exception its request raised."""
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        futures = {ep: pool.submit(fetcher.get, ep) for ep in ENDPOINTS}
    results: dict[str, bytes | Exception] = {}
    for ep, future in futures.items():
        try:
            results[ep] = future.result()
        except Exception as exc:  # 429, network error... check_result reports it
            results[ep] = exc
    return results


def retry_after(exc: requests.HTTPError) -> float:
    """Seconds the API asked us to wait, if it said."""
    try:
        return max(0.0, float(exc.response.headers.get("Retry-After", 0)))
    except (AttributeError, TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- saving and checking

class Outcome(NamedTuple):
    ok: bool           # saved and decoded
    line: str          # the summary line, or what went wrong
    wait: float = 0.0  # seconds the API asked us to wait before calling again


def save_raw(endpoint: str, payload: bytes, fetch_ts: int) -> Path:
    t = dt.datetime.fromtimestamp(fetch_ts, tz=dt.timezone.utc)
    path = DATA_DIR / "raw" / endpoint / t.strftime("%Y-%m-%d") / f"{t.strftime('%H%M%S')}.pb.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(payload)
    return path


def inspect(payload: bytes) -> tuple[int, int | None]:
    """Decode a response just far enough to check it: how many entities it holds, and the
    feed's own timestamp (None if it has none). Raises if it isn't a GTFS-RT feed."""
    feed = rt.FeedMessage()
    feed.ParseFromString(payload)
    if not feed.header.gtfs_realtime_version:
        raise ValueError("no GTFS-RT header")
    return len(feed.entity), (feed.header.timestamp or None)


class FeedHealth:
    """Whether a feed is empty or old, so each is logged when it starts and when it clears,
    rather than every minute."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.empty = False
        self.stale = False

    def update(self, entities: int, age: float | None) -> None:
        self.empty = self._flag(self.empty, entities == 0, "has no entities", "has entities again")
        self.stale = self._flag(self.stale, age is not None and age > STALE_AFTER,
                                f"is {fmt_age(age or 0)} old: the NTA seems to have stopped updating it",
                                "is updating again")

    def _flag(self, was: bool, now: bool, started: str, cleared: str) -> bool:
        if now and not was:
            log.warning("%-12s feed %s", self.endpoint, started)
        elif was and not now:
            log.info("%-12s feed %s", self.endpoint, cleared)
        return now


def save_and_check(endpoint: str, payload: bytes, fetch_ts: int, health: FeedHealth) -> Outcome:
    """Save one response, then check it."""
    try:
        save_raw(endpoint, payload, fetch_ts)
    except Exception as exc:  # disk full, no permission...
        line = f"{endpoint:<12} could not be saved: {exc}"
        log.error("%s", line)
        return Outcome(False, line)
    try:
        entities, feed_ts = inspect(payload)
    except Exception as exc:  # an error page, a cut-off response...
        line = f"{endpoint:<12} saved, but it isn't a valid GTFS-RT feed ({fmt_size(len(payload))}, {exc})"
        log.error("%s", line)
        return Outcome(False, line)
    age = fetch_ts - feed_ts if feed_ts else None
    when = f"feed {fmt_age(age)} old" if age is not None else "no feed timestamp"
    line = f"{endpoint:<12} {entities:,} entities, {when}, {fmt_size(len(payload))}"
    log.info("%s", line)
    health.update(entities, age)
    return Outcome(True, line)


def check_result(endpoint: str, result: bytes | Exception, fetch_ts: int, health: FeedHealth) -> Outcome:
    """What became of one feed this round: saved and checked, or why not."""
    if isinstance(result, bytes):
        return save_and_check(endpoint, result, fetch_ts, health)
    status = getattr(getattr(result, "response", None), "status_code", None)
    wait = retry_after(result) if status == 429 else 0.0
    if status:
        line = f"{endpoint:<12} HTTP {status}" + (f", the API asked to wait {wait:.0f}s" if wait else "")
    else:  # network blip, timeout...
        line = f"{endpoint:<12} failed: {result}"
    log.error("%s", line)
    return Outcome(False, line, wait)


class HealthCheck:
    """Reports to healthchecks.io: a check-in after each good round, and a failure once
    rounds keep failing. Trouble reaching healthchecks.io is logged once, and never gets in
    the way of collecting."""

    def __init__(self, url: str, session_factory=requests.Session):
        self.url = url.rstrip("/")
        self.session = session_factory() if url else None
        self.unreachable = False

    def ok(self, report: str) -> None:
        self._send(self.url, report)

    def failed(self, report: str) -> None:
        self._send(f"{self.url}/fail", report)

    def _send(self, url: str, report: str) -> None:
        if not self.url:
            return
        try:
            resp = self.session.post(url, data=report.encode("utf-8"), timeout=HEALTHCHECK_TIMEOUT,
                                     headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            if "not found" in resp.text.lower():  # healthchecks.io answers 200 even for an unknown URL
                raise ValueError("healthchecks.io doesn't recognise the ping URL")
        except Exception as exc:  # network blip, healthchecks.io down...
            if not self.unreachable:
                log.warning("Couldn't reach healthchecks.io (%s). Collecting carries on, and every "
                            "round tries again.", describe_error(exc))
            self.unreachable = True
            return
        if self.unreachable:
            log.info("Reaching healthchecks.io again.")
        self.unreachable = False


# ---------------------------------------------------------------- stopping cleanly
#
# `docker stop` sends SIGTERM, which on its own ends Python on the spot, possibly halfway
# through writing a file. Instead, the collector stops at once if it's waiting for the next
# round, or as soon as the round in progress has been saved and reported.

class StopNow(BaseException):
    """Leaves the run loop while it's waiting between rounds. A BaseException, so no
    `except Exception` handler mistakes it for an error."""


class _Stop:
    requested = False  # SIGTERM has arrived
    idle = False       # waiting for the next round, so stopping now is safe


def _on_sigterm(signum, frame) -> None:
    # Plain flags only: this runs between two steps of whatever the main thread is doing,
    # so it mustn't take locks (no logging, no threading.Event).
    if _Stop.idle:
        raise StopNow
    _Stop.requested = True


def wait_until(when: float) -> None:
    """Sleep until `when` (epoch seconds). SIGTERM during the wait stops the collector at once."""
    _Stop.idle = True
    try:
        if _Stop.requested:
            raise StopNow
        while (delay := when - time.time()) > 0:  # loops if the clock was adjusted meanwhile
            time.sleep(delay)
    finally:
        _Stop.idle = False


# ---------------------------------------------------------------- the run loop

def next_round_at(after: float) -> int:
    """The first round time at or after `after`. Rounds start on the minute (UTC), so the
    snapshots line up with the clock, across restarts too."""
    return math.ceil(after / ROUND_SECONDS) * ROUND_SECONDS


def run(fetcher: Fetcher) -> None:
    log.info("Fetching %s together at the start of every minute.",
             " and ".join(f"{ep} ({mask(fetcher.keys[ep])})" for ep in ENDPOINTS))
    healthcheck = HealthCheck(HEALTHCHECK_URL)
    if HEALTHCHECK_URL:
        log.info("Reporting to healthchecks.io (ping URL ...%s).", HEALTHCHECK_URL[-4:])
    else:
        log.warning("NTA_HEALTHCHECK_URL isn't set, so nothing will alert you if collection stops.")

    # The static timetable is checked in the background, so a slow download never delays a round.
    stop = threading.Event()
    threading.Thread(target=static_watcher, args=(stop,), daemon=True, name="static").start()

    health = {ep: FeedHealth(ep) for ep in ENDPOINTS}
    failed_rounds = 0
    next_round = next_round_at(time.time())
    log.info("First round at %s UTC.", hhmm(next_round))
    try:
        while not _Stop.requested:
            wait_until(next_round)
            round_ts = int(time.time())  # one fetch time for both feeds
            results = fetch_round(fetcher)
            outcomes = [check_result(ep, results[ep], round_ts, health[ep]) for ep in ENDPOINTS]
            report = "\n".join(o.line for o in outcomes)

            if all(o.ok for o in outcomes):
                if failed_rounds >= FAILED_ROUNDS_BEFORE_ALERT:
                    log.info("Rounds are succeeding again, after %d failed in a row.", failed_rounds)
                failed_rounds = 0
                healthcheck.ok(report)
            else:
                failed_rounds += 1
                if failed_rounds >= FAILED_ROUNDS_BEFORE_ALERT:
                    if failed_rounds == FAILED_ROUNDS_BEFORE_ALERT and HEALTHCHECK_URL:
                        log.warning("%d rounds in a row have failed; reporting it to healthchecks.io.",
                                    failed_rounds)
                    healthcheck.failed(report)
            if _Stop.requested:  # SIGTERM arrived during the round, which is now saved and reported
                break

            # The next round is on the next minute, unless the API asked for a longer wait or this
            # round overran. Then the rounds in between are skipped, and the schedule stays on the minute.
            hold_off = max(o.wait for o in outcomes)
            following = next_round_at(max(time.time(), round_ts + hold_off))
            skipped = (following - next_round) // ROUND_SECONDS - 1
            if skipped > 0:
                reason = f"the API asked to wait {hold_off:.0f}s" if hold_off else "the last round overran"
                log.warning("Skipping %d round%s because %s; the next one is at %s UTC.",
                            skipped, "" if skipped == 1 else "s", reason, hhmm(following))
            next_round = following
    finally:
        stop.set()  # stop the timetable checks too


# ---------------------------------------------------------------- static timetable
#
# The NTA's trip_ids and route_ids are regenerated whenever it uploads new schedules, so
# every timetable version is kept, as the zip the NTA published: live data only matches
# the version that was in use when it was collected. static/versions.csv is the record of
# the versions kept, and the only one. The server's change markers (ETag, Last-Modified),
# which let a check skip downloading an unchanged file, are kept in memory, starting from
# the ones recorded with the newest version.

def static_dir() -> Path:
    return DATA_DIR / "static"


def http_date_to_utc(value: str | None) -> str:
    """'Thu, 05 Nov 2026 10:00:00 GMT' -> '2026-11-05 10:00:00' (blank if missing)."""
    if not value:
        return ""
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value


def utc_to_http_date(value: str | None) -> str | None:
    """'2026-11-05 10:00:00' -> 'Thu, 05 Nov 2026 10:00:00 GMT', the form If-Modified-Since takes."""
    try:
        when = dt.datetime.strptime(value or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return email.utils.format_datetime(when, usegmt=True)


def read_versions(sdir: Path) -> list[dict]:
    """The rows of static/versions.csv, oldest first (none before the first download)."""
    path = sdir / "versions.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh) if row.get("file")]


def write_versions(sdir: Path, rows: list[dict]) -> None:
    """Rewrite static/versions.csv in one go, a temporary copy then a swap, so a stop at the
    wrong moment can't leave half a row."""
    tmp = sdir / "versions.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=VERSION_COLUMNS, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(sdir / "versions.csv")


def newest_version(sdir: Path) -> dict:
    """The newest version kept, from static/versions.csv: its file, its fingerprint and the
    server's change markers recorded with it. Empty if there's none, or its zip is missing."""
    rows = read_versions(sdir)
    if not rows or not (sdir / rows[-1]["file"]).exists():
        return {}
    last = rows[-1]
    return {"file": last["file"], "sha256": last.get("sha256"), "etag": last.get("etag") or None,
            "last_modified": utc_to_http_date(last.get("published_utc"))}


def gtfs_problem(path: Path) -> str | None:
    """Why a downloaded file can't be used as a timetable, or None if it looks fine."""
    if not zipfile.is_zipfile(path):
        return "not a complete zip file"
    with zipfile.ZipFile(path) as zf:
        missing = REQUIRED_GTFS_FILES - {Path(name).name for name in zf.namelist()}
        if missing:
            return "missing " + ", ".join(sorted(missing))
        broken = zf.testzip()
        if broken:
            return f"{broken} is corrupt"
    return None


def unique_path(path: Path) -> Path:
    candidate, n = path, 2
    while candidate.exists():
        candidate, n = path.with_name(f"{path.stem}_{n}{path.suffix}"), n + 1
    return candidate


def check_static(session, known: dict) -> str:
    """Download the static timetable if the NTA has published a new version, keep it as a dated
    zip and add it to static/versions.csv. `known` (see newest_version) describes the newest
    version kept and the server's change markers, and is kept up to date.
    Returns "new" or "unchanged"."""
    sdir = static_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    if known.get("file"):  # ask "has it changed?", so an unchanged file isn't sent again
        if known.get("etag"):
            headers["If-None-Match"] = known["etag"]
        if known.get("last_modified"):
            headers["If-Modified-Since"] = known["last_modified"]

    with session.get(STATIC_URL, headers=headers, timeout=(30, 300), stream=True) as resp:
        if resp.status_code == 304:
            log.info("Static timetable unchanged (latest: %s)", known.get("file"))
            return "unchanged"
        resp.raise_for_status()
        tmp = sdir / "download.tmp"
        digest, size = hashlib.sha256(), 0
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        etag, modified = resp.headers.get("ETag"), resp.headers.get("Last-Modified")

    sha = digest.hexdigest()
    known.update(etag=etag, last_modified=modified, validators=bool(etag or modified))
    if known.get("file") and sha == known.get("sha256"):
        tmp.unlink()  # the server sent the same file again
        log.info("Static timetable unchanged (latest: %s)", known["file"])
        return "unchanged"
    problem = gtfs_problem(tmp)
    if problem:
        tmp.unlink()
        raise ValueError(f"downloaded timetable rejected: {problem}")
    now = dt.datetime.now(dt.timezone.utc)
    path = unique_path(sdir / f"GTFS_Realtime_{now:%Y%m%d_%H%M%S}.zip")
    tmp.replace(path)
    write_versions(sdir, read_versions(sdir) + [{
        "downloaded_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "file": path.name,
        "published_utc": http_date_to_utc(modified), "etag": etag or "", "sha256": sha, "size_bytes": size}])
    known.update(file=path.name, sha256=sha)
    log.info("New static timetable saved as %s (%.1f MB, published %s)", path.name, size / 1e6,
             http_date_to_utc(modified) or "at an unknown time")
    return "new"


def static_watcher(stop: threading.Event, session_factory=requests.Session) -> None:
    """Keep the static timetable up to date in the background: check at start-up, then every hour."""
    session = session_factory()
    known = None
    interval = STATIC_CHECK_HOURS * 3600
    while not stop.is_set():
        wait = interval
        try:
            if known is None:
                known = newest_version(static_dir())
            check_static(session, known)
            if interval < NO_VALIDATOR_HOURS * 3600 and known.get("validators") is False:
                interval = wait = NO_VALIDATOR_HOURS * 3600
                log.warning("The timetable server doesn't say when the file last changed, so every check "
                            "downloads it in full. Checking every %d hours instead, to save bandwidth.",
                            NO_VALIDATOR_HOURS)
        except Exception as exc:  # network blip, bad download... try again sooner
            wait = STATIC_RETRY_MINUTES * 60
            log.warning("Static timetable check failed (%s); trying again in %d minutes.", exc,
                        STATIC_RETRY_MINUTES)
        stop.wait(wait)


def main() -> None:
    logging.Formatter.converter = time.gmtime  # log times in UTC, like the data
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S UTC")
    log.info("Starting NTA collector %s.", VERSION)
    fetcher = Fetcher(load_keys())
    signal.signal(signal.SIGTERM, _on_sigterm)  # `docker stop` (see "stopping cleanly")
    try:
        run(fetcher)
    except StopNow:
        pass
    log.info("Stopped.")


if __name__ == "__main__":
    main()
