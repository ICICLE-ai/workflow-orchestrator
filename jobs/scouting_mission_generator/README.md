# Parrot ANAFI Scouting Mission Generator + Flight Executor

Generates a lawnmower survey mission clipped to a farm boundary polygon,
for image or video capture. Split across two run paths:

- **On the platform**, the `scouting_mission` step
  (`backend/steps/scouting_mission/step.json`) submits `run_job.py` as a
  Tapis job — **mission generation only**. It writes
  `mission.waypoints` / `.plan` / `.kmz` / `.json` and a
  `next_steps.txt` into the run's output, pre-filled with the exact
  local command below.
- **Locally**, on a machine connected to the drone's own Wi-Fi network,
  `fly_mission.py` (or the standalone executable built from it) flies
  the generated mission, triggers captures, downloads media, and
  uploads it to Tapis.

These are split because an HPC batch node has no route to a drone's
local Wi-Fi AP (typically `192.168.42.1`) — flying can only ever happen
on a machine that's actually near the aircraft.

## Files

| File | Purpose |
|---|---|
| `mission_generator.py` | Core library: polygon-clipped lawnmower pattern, exports `.waypoints` / `.plan` / `.kmz` / `.json` |
| `fly_mission.py` | Generation → flight → Tapis upload, end to end. This is what gets built into the standalone executable for local use. |
| `tapis_upload.py` | Tapis Files API upload helper, also runnable standalone for testing uploads in isolation |
| `run_job.py` | **The platform's Tapis app entrypoint** — calls `fly_mission.py`'s generation path only (equivalent to `fly_mission.py --dry-run`) and writes `next_steps.txt` |
| `scouting_mission.def` | Apptainer/Singularity definition for the Tapis app `run_job.py` runs in — lightweight (shapely + requests only, no Olympe) |
| `build_executable.sh` | Bundles `fly_mission.py` into one double-clickable Linux binary via PyInstaller, for the local flight step |
| `requirements.txt` | Python dependencies (Olympe is *not* on this list — see below) |

## On the platform: the `scouting_mission` step

Add a **🔎 Scouting Mission Generator** node, trace the farm boundary and
starting point on the map, set line spacing / capture spacing / mode,
and (optionally) where captured media should eventually be uploaded.
Running it submits `run_job.py` via the `generate-scouting-mission`
Tapis app and produces:

- `mission_files/` — `mission.waypoints`, `mission.plan`, `mission.kmz`,
  `mission.json`, `next_steps.txt`
- `mission.json` — the same generic JSON, exposed as its own output port

Open the `.kmz` in Google Earth (or drag it into
[plot.ardupilot.org](https://plot.ardupilot.org/) / QGroundControl) to
visually confirm the pattern before ever going near the drone.

## Locally: generate + fly + upload directly

**1. Generate mission files only (no drone needed), to sanity-check first:**

```bash
python3 fly_mission.py \
  --boundary "40.0130,-83.0460;40.0130,-83.0448;40.0140,-83.0448;40.0140,-83.0460" \
  --home "40.0130,-83.0460" \
  --altitude 10 \
  --line-spacing 10 \
  --mode image \
  --capture-spacing 5 \
  --hold-time 2.0 \
  --out-dir ./mission_output \
  --mission-name run_1 \
  --dry-run
```

Boundary is any number of `lat,lon` points separated by `;` — 3 minimum,
traced in the order given, and does not need to be a rectangle.

**2. Build the standalone executable:**

```bash
chmod +x build_executable.sh
./build_executable.sh
```

This produces `dist/fly_mission` — a single file with no Python
dependency for the end user. Give them that file plus instructions to
`chmod +x fly_mission` once, then double-click it going forward (or run
it from a terminal with the same flags shown above, minus `--dry-run`).

**3. Full run (generate + fly + upload):**

```bash
./fly_mission \
  --boundary "40.0130,-83.0460;40.0130,-83.0448;40.0140,-83.0448;40.0140,-83.0460" \
  --home "40.0130,-83.0460" \
  --altitude 10 \
  --line-spacing 10 \
  --mode image \
  --capture-spacing 5 \
  --out-dir ./mission_output \
  --mission-name run_1 \
  --drone-ip 192.168.42.1 \
  --tapis-base-url https://tacc.tapis.io \
  --tapis-token "$TAPIS_TOKEN" \
  --tapis-system-id farm-drone-storage \
  --tapis-remote-path missions/2026-09-14_run1/media
```

The `next_steps.txt` the platform writes has this command pre-filled
with the mission parameters and upload destination you entered in the
step's UI — copy it out rather than retyping everything by hand.

`--tapis-token` can also come from the `TAPIS_TOKEN` environment
variable instead of the command line, which is the better option if
job submission is passing secrets through env rather than argv (argv is
visible to anyone on the box via `ps`).

## What "image" vs "video" mode actually do

- **`image`**: the drone flies each scan line but stops every
  `--capture-spacing` meters, holds for `--hold-time` seconds, and takes
  a still photo before continuing.
- **`video`**: the drone starts recording right after takeoff and stops
  right before returning home — no stops, continuous flight.

## Important limitation: camera triggering on ANAFI

ANAFI's onboard flight-plan player does not reliably act on standard
MAVLink camera-trigger commands (`DO_SET_CAM_TRIGG_DIST`, etc.) the way
ArduPilot-based autopilots do. Because of that, **the authoritative
camera trigger in this workflow is `fly_mission.py` itself**, driving
the drone waypoint-by-waypoint via Olympe's `moveTo()` and calling
`take_photo()` / `start_recording()` / `stop_recording()` directly at
the right moments — not the flight-plan file's embedded camera
commands. Those commands are still written into the exported
`.waypoints`/`.plan` files for compatibility with other ground-control
software and as a fallback, but don't rely on them alone for ANAFI.

If you have since confirmed ANAFI *does* honor onboard camera triggers
via some other test, this is the place in the code to relax and let the
flight-plan drive capture directly instead of the waypoint-by-waypoint
Olympe loop — flag it and it's a small change.

## Olympe dependency (important)

Parrot's **Olympe SDK is not distributed on PyPI** and cannot be
`pip install`-ed. It must be installed separately, following Parrot's
own instructions:
<https://developer.parrot.com/docs/olympe/installation.html>

- Mission generation (`--dry-run`, and the platform's own
  `scouting_mission` Tapis job/container) has no Olympe dependency and
  works on any machine with the packages in `requirements.txt`.
- Actually flying requires Olympe to be installed **on the machine that
  runs the flight** — if you build the executable on a machine that
  doesn't have Olympe installed, PyInstaller can't bundle it, and the
  resulting binary will only support `--dry-run`. Build on a machine
  that already has Olympe set up if you want a flight-capable
  executable.

## Tapis authentication

Tapis expects a bearer JWT access token, not a traditional static API
key. `fly_mission.py` verifies the token against the Tapis `/v3/systems`
endpoint *before* taking off, so a bad or expired token fails fast
instead of only being discovered after the flight completes.

## Known simplifications / things to verify before relying on this

1. **Concave polygon coverage** is handled by connecting disjoint scan-line
   segments with a direct straight connector, not a globally optimized
   coverage path. For unusually shaped fields, check the `.kmz` visually.
2. **Media download** uses the common Olympe `drone.media.list()` /
   `drone.media.download()` pattern — the exact media API has changed
   across Olympe SDK versions, so verify this section against whatever
   Olympe version ends up installed on your flight machine.
3. **`.plan` camera semantics**: QGroundControl's own `.plan` schema for
   camera items varies by version; the `.plan` export here is nav-item-only
   and correct for that, but isn't meant to carry authoritative camera
   timing the way the internal `.json` does.
4. This has been tested for mission generation and file export (polygon
   clipping, all four export formats, the standalone executable build).
   It has **not** been tested against a physical ANAFI or a live Tapis
   endpoint in this environment — sections in `fly_mission.py` and
   `tapis_upload.py` should be validated on real hardware/credentials
   before a production flight.
5. `run_job.py` and `scouting_mission.def` (the platform-side generation
   job) are new integration code, not part of what was validated above —
   only `mission_generator.py`'s generation path and the four export
   formats were exercised end to end.
