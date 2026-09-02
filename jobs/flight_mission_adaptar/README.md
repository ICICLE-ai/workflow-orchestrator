# Flight Mission Adaptar

Converts the platform-agnostic `flight_plan.json` from [`flight_plan_generator`](../flight_plan_generator/) into the mission files real ground control software actually loads — ArduPilot `.waypoints`, PX4 `.plan`, DJI WPML `.kmz`, and a plain CSV for review.

**One file per charge cycle.** Mission Planner, QGroundControl and DJI Pilot each fly one mission file at a time, and a human (or a docking/refill station) performs the battery swap and tank refill between cycles. Emitting one combined file would not be flyable.

## How It Works

1. Reads `flight_plan.json` and takes its cycles in order
2. Expands each cycle's abstract actions (`HOME`/`SPRAY_ON`/`SPRAY`/`SPRAY_OFF`/`TRANSIT`/`RTL`) into a flat list of MAVLink-style commands
3. Emits that same command list through each requested format writer
4. Writes one file per cycle per format, into a subdirectory named for the format

Every writer consumes the identical expanded command list, so the four formats cannot silently drift apart — a fix to the expansion applies to all of them.

## Files

| File | Description |
|---|---|
| `export_flight_mission.py` | Everything — CLI, command expansion, and all four format writers |
| `export_flight_mission.def` | Apptainer/Singularity definition (bare Python, no dependencies) |
| `requirements.txt` | Present but unused — the script is stdlib-only (see [Dependencies](#dependencies)) |

## Usage

```bash
cd flight_mission_adaptar

# every format at once
python export_flight_mission.py \
    --json flight_plan.json \
    --outdir ./output/missions \
    --format all \
    --sprayer-servo-channel 9 \
    --sprayer-pwm-on 1900 \
    --sprayer-pwm-off 1100

# just ArduPilot
python export_flight_mission.py \
    --json flight_plan.json --outdir ./output/missions --format ardupilot
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--json` | *required* | `flight_plan.json` to convert |
| `--outdir` | *required* | Output root; one subdirectory per format is created under it |
| `--format` | `all` | `ardupilot`, `px4`, `generic_csv`, `dji`, or `all` |
| `--sprayer-servo-channel` | `9` | Servo/relay channel wired to the sprayer valve |
| `--sprayer-pwm-on` | `1900` | PWM value that opens the sprayer |
| `--sprayer-pwm-off` | `1100` | PWM value that closes it |
| `--cruise-speed-mps` | from `meta.speed_mph` | Cruise speed override, m/s |
| `--hover-speed-mps` | `5.0` | PX4 hover speed, m/s |
| `--drone-enum-value` | `0` | **Unverified** — DJI aircraft model enum, see [DJI caveats](#dji-wpml--read-before-flying) |
| `--drone-sub-enum-value` | `0` | DJI aircraft sub-model enum |
| `--payload-enum-value` | `0` | **Unverified** — DJI sprayer payload enum |
| `--payload-position-index` | `0` | DJI payload mount position |
| `--spray-action-tag` | `sprayerEnable` | **Unverified** — WPML `actionActuatorFunc` tag for spray on/off |
| `--spray-param-tag` | `sprayerEnable` | **Unverified** — param tag name inside that action |

When `--cruise-speed-mps` is omitted, the speed is taken from the plan's `meta.speed_mph` — the value the flight planner derived from the nozzle hardware — so the exported mission applies the rate the plan was built around.

## Outputs

```
<outdir>/
  ardupilot/    flight_plan_cycle1.waypoints   flight_plan_cycle2.waypoints  ...
  px4/          flight_plan_cycle1.plan        ...
  generic_csv/  flight_plan_cycle1.csv         ...
  dji/          flight_plan_cycle1.kmz         ...
```

| Format | File | Consumer |
|---|---|---|
| `ardupilot` | QGC WPL 110 plain text | ArduPilot / Mission Planner |
| `px4` | QGroundControl `.plan` JSON | PX4 / QGroundControl |
| `dji` | WPML `.kmz` (`wpmz/template.kml` + `wpmz/waylines.wpml`) | DJI Pilot 2 |
| `generic_csv` | `seq,lat,lon,altitude_ft,action` | Spreadsheets, debugging, your own tooling — not a mission format |

Altitudes stay in feet in the JSON, ArduPilot and CSV outputs, and are converted to meters for DJI WPML.

## Sprayer Control

Spray on/off is emitted as `MAV_CMD_DO_SET_SERVO` (command 183), toggling `--sprayer-servo-channel` between the on and off PWM values. Both ArduPilot and PX4 support this, and it matches how a servo- or relay-controlled sprayer valve is normally wired.

The defaults are conventional, not universal. **Check them against your actual wiring** — a wrong channel silently sprays nothing, or actuates something else.

For the DJI path, the PWM values are interpreted through a midpoint heuristic: `≥ 1500` means on, below means off. Keep your on/off values on opposite sides of 1500.

## DJI WPML — read before flying

The KMZ packaging, mission config, waypoint/path schema and action-group mechanics follow DJI's published WPML spec ([dji-sdk/Cloud-API-Doc](https://github.com/dji-sdk/Cloud-API-Doc)), cross-checked against a real sample `waylines.wpml` from that repo. That part is solid.

**The spray control is not.** DJI's public WPML documentation covers only camera and gimbal actions for M30/M300/Mavic-class aircraft. There is no publicly documented action tag for Agras spray control — Agras missions are normally produced by DJI's own planning software, whose spray-actuator schema is not published. Rather than invent a plausible-looking tag and present it as fact, the spray tags are CLI-configurable placeholders, marked as unverified in the generated XML itself.

Likewise, the aircraft and payload enums in `wpml:missionConfig` default to `0`. DJI's enum table for Agras models is not public either (only M30 = 67 is confirmed), and Pilot 2 may reject the import outright until they are correct.

Before trusting a `.kmz` from this job on a real Agras flight:

1. Export a mission from DJI Pilot 2 / DJI Agras planning software for your aircraft
2. Unzip it and read `wpmz/waylines.wpml`
3. Find the real `actionActuatorFunc` tag used for nozzle on/off, and the real drone/payload enum values
4. Pass them via `--spray-action-tag`, `--spray-param-tag`, `--drone-enum-value` and `--payload-enum-value`

The job prints a warning whenever the DJI format is requested while those enums are still at `0`.

Two further notes on the DJI output: `template.kml` and `waylines.wpml` are written with identical content, which the spec permits since they share the mission schema; and `RTL` is expressed as `wpml:finishAction=goHome` rather than a final placemark, per DJI's documented finish-action options.

## Dependencies

The script imports only `argparse`, `csv`, `json`, `os` and `zipfile` — all standard library. The container is a slim Python interpreter with nothing installed, and its `%test` asserts exactly that.

`requirements.txt` lists the geo stack (`geopandas`, `shapely`, `fiona`, `pyogrio`) carried over from the flight planner. Nothing here uses it; installing it is harmless but unnecessary.

## Container

Build:

```bash
apptainer build export_flight_mission.sif export_flight_mission.def
```

Run, binding your data directory:

```bash
apptainer run --bind /host/path/to/data:/data \
    export_flight_mission.sif \
        --json /data/flight_plan.json \
        --outdir /data/output/missions \
        --format all
```

CPU only, no GPU, no network, and seconds to run — this stage is pure file translation.

## Pipeline Position

The last stage. It performs no geometry and makes no decisions; everything about where the drone flies and when it sprays was settled upstream:

```
output.gpkg  →  generate_flight_plan.py  →  flight_plan.json  →  export_flight_mission.py  →  .waypoints / .plan / .kmz / .csv
                                                                                              (one set per charge cycle)
```
