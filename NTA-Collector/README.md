# NTA real-time collector

Fetches the NTA's two GTFS-Realtime feeds, vehicle positions and trip updates, at the start of
every minute, and keeps every response exactly as received, along with every version of the
static timetable. It runs in Docker: you test changes on your PC, GitHub publishes an image for
each release, and the server runs that image. Turning the responses into tables is a separate
job, the builder (see section 7).

## 1. Get the API keys
1. Sign up at https://developer.nationaltransport.ie and subscribe to the **GTFS-Realtime** product.
2. Copy your **primary key** and your **secondary key** from your profile page. You need both:
   the two feeds are fetched at the same moment, and the NTA allows each key one call a minute.
3. On the API definition page, confirm the v2 URLs (`.../gtfsr/v2/TripUpdates` and
   `.../gtfsr/v2/Vehicles`) and read the fair-usage policy. If the NTA ever moves the feeds or
   the timetable, set `NTA_BASE_URL` or `NTA_STATIC_URL` in `collector.env`.

## 2. Set up the healthchecks.io check
The collector checks in with healthchecks.io after every good round, and reports a failure, with
what went wrong, once 3 rounds in a row have failed. If the check-ins stop altogether (the server
or the collector is down), healthchecks.io alerts you when the grace time runs out. In your
check's settings on healthchecks.io:
- Set **Period** to **1 minute** and **Grace Time** to **10 minutes**. New checks start at a day
  and an hour, which would let an outage go unnoticed for a day.
- Make sure a notification method (email, or an app on your phone) is switched on.
- Before you stop the collector on purpose, **pause** the check, or you'll get an alert. The
  next check-in un-pauses it.

## 3. Test it on your PC
Try a change with Docker Desktop before you release it. Put your keys in `collector.env` first.
It's git-ignored, so it stays on your PC. Then, in PowerShell:
```
cd $HOME\Documents\ProjectTransport\ireland-transit-analytics\NTA-Collector
docker compose up -d --build
docker compose logs -f
```
The log starts with `Starting NTA collector dev` (a local build has no release number), then
shows a `vehicles` line and a `trip_updates` line every minute. Ctrl+C closes the log and leaves
the collector running. The data goes into the `data` folder here, which is git-ignored too.
When you're done, run `docker compose down`.

Once the server is collecting:
- Before a test, empty the ping URL in your PC's `collector.env` (`NTA_HEALTHCHECK_URL=`).
  Otherwise a test that works would hide a server that doesn't, and a test that fails would
  send an alert.
- Your PC and the server use the same keys, and both call on the minute, so one of them is
  refused each time and the server's data has gaps while a test runs. Keep tests short, or test
  with a second pair of keys if the NTA's portal lets you add another subscription.

## 4. Publish a release
GitHub Actions builds the image and publishes it to GitHub's container registry as
`ghcr.io/riansmuts/nta-collector`, for both x86 and ARM servers, each time you create a release
tag like `nta-collector-v0.1.1`. The workflow is `.github/workflows/publish-nta-collector.yml`,
at the top of the repo. The tag starts with the part of the repo it releases, so other parts
(the builder, say) can have releases of their own later.

### Each release
1. Test the change on your PC (section 3), then commit and push it.
2. On the repo's GitHub page, click **Releases**, then **Draft a new release**.
3. Under **Choose a tag**, type the new tag, like `nta-collector-v0.1.2`, and pick **Create new
   tag on publish**. Leave the target as `main`.
4. Click **Generate release notes**, add a line on what changed, and click **Publish release**.
5. The **Actions** tab shows the build. When it's green, after a few minutes, the new version is
   published, and your friend's Renovate opens a pull request to update the server.

A tag pushed from GitHub Desktop starts the build too, but without release notes. Never change
or reuse a published version: release a new one instead.

### The first time
Release `nta-collector-v0.1.1` as above. Once the build is green:
1. Make the package public, so the server and Renovate can pull it without a password. On
   GitHub, open your profile, then **Packages** → **nta-collector** → **Package settings**.
   Under **Danger Zone**, click **Change visibility**, pick **Public** and confirm. It can't be
   made private again, but the image only holds `collector.py` and its Python packages, never
   your keys.
2. Give your friend the image name, `ghcr.io/riansmuts/nta-collector:0.1.1`, and section 5.

### Version numbers
- **Patch** (0.1.1 → 0.1.2): fixes. Nothing changes for the server or the builder.
- **Minor** (0.1.2 → 0.2.0): new features. Still nothing to change on the server, and the files
  the builder reads keep their layout.
- **Major** (0.2.0 → 1.0.0): something has to change elsewhere too, like a new required setting
  in `collector.env` or a new layout for the files the builder reads. Say what in the release
  notes. Renovate raises major versions as separate pull requests, so your friend can make the
  change before updating. Renovate only counts a change in the first number as major, even
  while it's 0, so the first change like this takes you to 1.0.0, not 0.3.0.

## 5. Run it on the server
The server runs the published image, so it needs no copy of the code: just a compose file,
`collector.env` and a folder for the data. Your friend's setup will have its own way of laying
these out. What matters is in this compose file:
```yaml
services:
  nta-collector:
    image: ghcr.io/riansmuts/nta-collector:0.1.1  # a full version, so Renovate can update it
    container_name: nta-collector  # one collector per pair of keys; a second copy won't start
    restart: unless-stopped        # back up after a crash or a reboot
    init: true                     # passes `docker stop` on to the collector
    stop_grace_period: 60s         # time to finish the round in progress (up to about 40s)
    user: "1000:1000"              # must own the data folder
    env_file: collector.env        # your keys and the healthchecks.io ping URL
    volumes:
      - ./data:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"            # keep the log from growing forever
        max-file: "3"
```

Before the first start:
- **Stop the collector on your PC** (`docker compose down`). Only one collector can use your
  keys at a time.
- **`collector.env`** holds your keys and ping URL. It's a secret, so keep it out of git on the
  server's side too.
- **The data folder** must exist before the first start, owned by the `user:` in the compose
  file. If Docker creates it, it belongs to root and the collector can't write to it. To carry
  on with the data you've collected so far, copy it in. Earlier versions also wrote
  `data/static/current/` and `data/static/state.json`, which this one doesn't use, so you can
  leave those out.
- **The clock**: file names are fetch times from the server's clock, so it must be
  synchronised. `timedatectl` should say `System clock synchronized: yes`.
- **Disk space**: compare a day of `data/raw` on your PC with the space the server can spare.

Once it's running, `docker logs -f nta-collector` shows a `vehicles` line and a `trip_updates`
line every minute, and the check turns green on healthchecks.io. If it stops straight away, the
log says why (a key missing from `collector.env`, for example).

**Updates:** merging a Renovate pull request updates the server. The old container finishes the
round in progress and stops, and the new one starts at the next minute, so an update costs at
most one round. To go back, put the previous version back in the compose file.

**Getting the data to your PC:** your PC needs a copy of `data/` for the builder, DuckDB and
Power BI. WinSCP's *Synchronize*, set to update your local folder, copies only new and changed
files; with WSL, `rsync -av you@server:<data folder>/ data/` does the same. That copy is also
your backup if the server ever fails.

## 6. How it behaves

### Schedule
Both feeds are requested at the same moment at the start of every minute (UTC), so the snapshots
line up with the clock, across restarts too. The schedule never changes: if a round fails, that
minute is a gap in the data rather than a reason to fetch differently. If the API answers "429
Too Many Requests" with a Retry-After, the rounds inside that wait are skipped, and the next
round is back on the minute.

### Health checks
Each response is decoded just far enough to check it, and the log shows a line like:
```
vehicles     1,180 entities, feed 25s old, 212 KB
```
The log warns when a response isn't a valid GTFS-RT feed (it's saved anyway, so you can look at
it), when a feed has no entities, and when a feed is more than 10 minutes old, which means the
NTA has stopped updating it. An empty or old feed is logged when it starts and when it clears,
not every minute.

A round in which both feeds were saved and decoded checks in with healthchecks.io, with that
round's log lines attached, so you can see the latest counts there. From the third failed round
in a row, each failed round is reported to healthchecks.io as a failure, with what went wrong,
so you're alerted within minutes. An empty or old feed still counts as a good round: the
collector is working, and the problem is on the NTA's side.

### Static timetable
The collector also checks for a new static timetable (routes, stops, trips, stop times, shapes)
at start-up and every hour, in the background. When the NTA has published a new version, it
keeps it as a dated zip, exactly as published, and adds a row to `data/static/versions.csv`:
when it was downloaded and published, its ETag, a SHA-256 fingerprint and its size. When
nothing has changed, the server just answers "not modified" and nothing is downloaded. Every
version is kept because the NTA regenerates `trip_id`s whenever it uploads new schedules, so
live data only matches the version that was in use when it was collected.

## 7. Notes for the builder
The builder turns `data/raw` into tables. What it can rely on:
- Every response is in `data/raw/<endpoint>/<YYYY-MM-DD>/<HHMMSS>.pb.gz`: gzipped GTFS-RT
  protobuf, exactly as received. The folder date and file name are the fetch time in UTC.
  A `vehicles` file and a `trip_updates` file with the same date and name were fetched
  together, so they're one snapshot of the network.
- Rounds start on the minute, so file names normally end in `00`. Files from early development
  versions of the collector can have any seconds.
- Responses that failed the health check are saved too, so skip any file that doesn't decode.
- Decode with `gtfs_realtime_pb2.FeedMessage` from the same `gtfs-realtime-bindings` package.

The plan for it:
- Take the columns from the GTFS-RT schema (the message descriptors), not from the data, so
  every field the spec defines becomes a column and the set is the same every day.
- Give each repeated field (like `stop_time_update`) its own table, linked back by fetch time
  and entity id.
- Rebuild a whole day at a time and write it as Parquet, so a rerun replaces the day instead of
  adding to it.
- Write a coverage table too: minutes with no snapshot, minutes where only one feed was saved,
  and entity counts through the day.
- Match each snapshot to the timetable version in use when it was fetched, since `trip_id`s
  change between versions. In `data/static/versions.csv`, a version counts from its
  `published_utc` (or `downloaded_utc` when that's blank) until the next one: the collector
  checks hourly, so a version can be live before it's downloaded. Then confirm the match from
  the data: the share of a snapshot's `trip_id`s found in that version's `trips.txt` should be
  close to 100%. Put that share in the coverage table too.
- The timetables stay zipped; read the files you need straight out of the zip (Python's
  `zipfile` does this) rather than unpacking every version.

`data/parsed/` holds the CSVs that early development versions of the collector wrote; later
versions don't add to it. The
builder can replace them by rebuilding those days from `data/raw`. Queries written for those
CSVs (like `explore.sql`) need pointing at the builder's tables once they exist.

## Files
- `collector.py`: the collector.
- `Dockerfile`, `.dockerignore`: the image. Only `collector.py` and `requirements.txt` go into it.
- `docker-compose.yml`: builds and runs the collector on your PC, for testing.
- `requirements.txt`: the Python packages.
- `collector.env`: your keys and healthchecks.io ping URL. Private: the repo's `.gitignore`
  keeps it out of git.
- `data/raw/`, `data/static/`: what the collector writes (see section 7 and the top of
  `collector.py`). Also kept out of git.
- `.github/workflows/publish-nta-collector.yml`, at the top of the repo: publishes the image for
  each release (section 4).

Data: National Transport Authority, GTFS-R, CC BY 4.0. Credit the NTA wherever you publish results.
