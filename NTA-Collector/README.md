# NTA real-time collector

Fetches the NTA's two GTFS-Realtime feeds, vehicle positions and trip updates, at the start of
every minute, and keeps every response exactly as received, along with every version of the
static timetable. It runs in Docker on a Linux server. Turning the responses into tables is a
separate job, the builder (see section 5).

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

## 3. Run it on the server

### Before you start
- The server needs Docker with the Compose plugin (`docker compose version` should work), and
  your account needs permission to use it (membership of the `docker` group).
- **Stop the collector on your PC first.** Only one collector should use your keys at a time.
- Make sure there's room: compare how much a day of `data/raw` takes on your PC with the free
  space `df -h ~` shows on the server.
- The file names are the fetch times, taken from the server's clock, so the clock must be right:
  `timedatectl` should say `System clock synchronized: yes`.

### Step 1: fill in your keys
On your PC, open `collector.env` in Notepad and paste in your two keys. Your healthchecks.io
ping URL is already in it. The file holds secrets, so keep it out of anything you publish.

### Step 2: copy the files over
In PowerShell (replace `you@server` with your login and the server's address):
```
cd $HOME\Documents\ProjectTransport\NTA-Collector
ssh you@server "mkdir -p nta-collector/data"
scp collector.py requirements.txt Dockerfile docker-compose.yml .dockerignore collector.env you@server:nta-collector/
```
Creating `data` yourself matters: if Docker creates it, it belongs to root and the collector
can't write to it. To carry on with the data you've already collected, copy that too:
`scp -r data you@server:nta-collector/`. Earlier versions also wrote `data/static/current/` and
`data/static/state.json`, which this one doesn't use, so you can delete those on the server:
`rm -r ~/nta-collector/data/static/current ~/nta-collector/data/static/state.json`

### Step 3: check your user ID
Everything from here on runs on the server, so `ssh you@server` first:
```
cd ~/nta-collector
chmod 600 collector.env
id -u
id -g
```
If either number isn't `1000`, put yours in the `user:` line of `docker-compose.yml` (for
example `"1001:1001"`), so the files the collector writes belong to you.

### Step 4: start it
```
docker compose up -d --build
docker compose logs -f
```
The collector waits for the start of the next minute. From then on, every minute you should
see a `vehicles` line and a `trip_updates` line with entity counts, and the check turns green on
healthchecks.io. Ctrl+C only closes the log; the collector keeps going. It starts again by itself
after a crash or a reboot. If it stops straight away, the log says why (a key missing from
`collector.env`, for example).

### Day to day
Run these from `~/nta-collector`:

| To... | Run |
|---|---|
| Follow the log | `docker compose logs -f` |
| Check it's running | `docker compose ps` |
| Update it after changing `collector.py` | `docker compose up -d --build` |
| Apply a change to `collector.env` | `docker compose up -d` |
| Stop it (pause the check first) | `docker compose stop` |
| Start it again | `docker compose start` |

A stop or an update lets the round in progress finish saving first, so no file is left
half-written. Once you've stopped it, it stays stopped, reboots included, until you start it.

### Getting the data to your PC
Your PC needs a copy of `data/` for the builder, DuckDB and Power BI. WinSCP's *Synchronize*,
set to update your local folder, copies only new and changed files; with WSL, run
`rsync -av you@server:nta-collector/data/ data/` from the collector folder. That copy is also
your backup if the server ever fails.

## 4. How it behaves

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

## 5. Notes for the builder
The builder turns `data/raw` into tables. What it can rely on:
- Every response is in `data/raw/<endpoint>/<YYYY-MM-DD>/<HHMMSS>.pb.gz`: gzipped GTFS-RT
  protobuf, exactly as received. The folder date and file name are the fetch time in UTC.
  A `vehicles` file and a `trip_updates` file with the same date and name were fetched
  together, so they're one snapshot of the network.
- From collector 3.0, rounds start on the minute, so file names normally end in `00`. Files from
  earlier versions can have any seconds.
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

`data/parsed/` holds the CSVs that collector 1.x wrote; later versions don't add to it. The
builder can replace them by rebuilding those days from `data/raw`. `explore.sql` queries those
CSVs, so it needs pointing at the builder's tables once they exist.

## Files
- `collector.py`: the collector.
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`: the container. Only `collector.py` and
  `requirements.txt` go into the image.
- `collector.env`: your keys and healthchecks.io ping URL. Private.
- `requirements.txt`: the Python packages.
- `.gitignore`: keeps `collector.env` and `data/` out of git.
- `explore.sql`: DuckDB queries for collector 1.x's CSVs.
- `data/raw/`, `data/static/`: what the collector writes (see section 5 and the top of `collector.py`).

Data: National Transport Authority, GTFS-R, CC BY 4.0. Credit the NTA wherever you publish results.
