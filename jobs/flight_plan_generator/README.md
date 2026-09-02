# Flight Plan Generator

Turns a spray-zone GeoPackage into a flyable, platform-agnostic spray mission: coverage paths over every zone that needs treating, a cruise speed derived from the nozzle hardware, and the whole route split into battery/tank-sized charge cycles.

The output is deliberately generic. Converting it into ArduPilot, PX4 or DJI mission files is the job of [`flight_mission_adaptar`](../flight_mission_adaptar/).

## How It Works

1. Reads the `spray_zones` layer and keeps every cell whose `spray_decision` is not `"No"`
2. Dissolves those cells into contiguous zone polygons, and measures each zone's area in a local feet projection (not naive degrees²)
3. Orders the zones by greedy nearest-neighbor, starting from home
4. For each zone, generates a boustrophedon ("lawnmower") path: scan lines spaced one nozzle swath apart, aligned with the **longest edge of the zone's minimum rotated rectangle** so the passes run the long way and the drone turns as little as possible
5. Computes cruise speed from the nozzle hardware using the standard ag-spray formula
6. Walks the whole route, accumulating flight time and spray volume, and cuts a new charge cycle whenever the next leg — *plus the flight home afterward* — would blow either budget
7. Marks where the sprayer turns on and off
8. Writes the plan as JSON, and as a GeoPackage for visual QA

## Files

| File | Description |
|---|---|
| `generate_flight_plan.py` | Everything — CLI, zone dissolve, coverage path, speed model, cycle segmentation, writers |
| `generate_flight_plan.def` | Apptainer/Singularity definition (conda-forge geo stack) |
| `requirements.txt` | Dependency list for a local pip install — the container installs via conda instead |

## Usage

```bash
cd flight_plan_generator

python generate_flight_plan.py \
    --gpkg farm_output.gpkg \
    --outdir ./output \
    --nozzle-swath-ft 20 \
    --flight-height-ft 15 \
    --tank-capacity-gal 5 \
    --nozzle-flow-rate-gpm 0.5 \
    --dispersion-rate-gpa 2 \
    --battery-time-min 20 \
    --battery-reserve-pct 20 \
    --home-lat 40.014361 --home-lon -83.042294
```

Input is the GeoPackage written by [`generate_geopackage`](../generate_geopackage/), which names its output `output.gpkg` — pass whatever path it actually wrote.

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--gpkg` | *required* | GeoPackage holding the `spray_zones` layer |
| `--outdir` | *required* | Directory for `flight_plan.json` and `flight_plan.gpkg` |
| `--nozzle-swath-ft` | *required* | Ground width covered per pass — also the spacing between lawnmower lines |
| `--flight-height-ft` | *required* | Altitude flown during spraying |
| `--tank-capacity-gal` | *required* | Spray tank capacity — the per-cycle volume budget |
| `--nozzle-flow-rate-gpm` | *required* | Total nozzle flow rate, gallons/minute |
| `--dispersion-rate-gpa` | *required* | Target application rate, gallons/acre |
| `--battery-time-min` | *required* | Flight endurance per charge, minutes |
| `--battery-reserve-pct` | `20.0` | Fraction of endurance held back as safety reserve |
| `--transit-speed-mph` | spray speed | Speed on non-spraying legs |
| `--home-lat` | *required* | Launch/return point latitude |
| `--home-lon` | *required* | Launch/return point longitude |

The job exits with an error if no zone has `spray_decision != "No"` — there is nothing to fly.

## The Speed Model

Cruise speed is not a parameter. It falls out of the hardware and the agronomy:

```
speed_mph = (flow_rate_gpm × 5940) / (target_gpa × swath_ft)
```

This is the standard ag-spray rate identity rearranged for speed, so the drone applies exactly `--dispersion-rate-gpa` while the nozzles run flat out at `--nozzle-flow-rate-gpm`. Fly faster and you under-apply; slower and you over-apply. The computed speed is printed at startup and recorded in the plan's `meta`.

If it comes out implausible for your aircraft, the fix is a different nozzle or a different target rate — not overriding the speed.

## Charge-Cycle Segmentation

A mission rarely fits in one battery or one tank, so the route is cut into cycles against two budgets held simultaneously:

- **Time** — `battery_time_min × (1 − reserve_pct/100)`
- **Volume** — `tank_capacity_gal`

Before committing to a leg, the planner adds the time it would then take to get home from the far end. A cycle is closed only if that projection breaks a budget, so the drone is never planned into a position it lacks the endurance to return from.

When a cycle closes it appends an `RTL` leg and starts the next cycle at `HOME` — **resuming at the exact waypoint it stopped at**, not the next one. No coverage is skipped across a battery swap.

If one single leg exceeds a whole cycle's budget even starting fresh from home, it cannot be split at this granularity: it is included anyway and the run warns, naming the waypoint index. That means the zone geometry or the budgets need revisiting.

## Outputs

### `flight_plan.json`

```json
{
  "meta": {
    "total_spray_zones": 3,
    "total_area_acres": 1.482,
    "speed_mph": 7.42,
    "num_charge_cycles": 2,
    "home": {"lat": 40.014361, "lon": -83.042294}
  },
  "cycles": [
    {
      "cycle_id": 1,
      "cum_time_s": 902.4,
      "cum_volume_gal": 4.87,
      "waypoints": [
        {"seq": 0, "lat": 40.014361, "lon": -83.042294, "alt": 0,  "action": "HOME"},
        {"seq": 1, "lat": 40.014502, "lon": -83.042110, "alt": 15, "action": "SPRAY_ON"},
        {"seq": 2, "lat": 40.014610, "lon": -83.041980, "alt": 15, "action": "SPRAY"}
      ]
    }
  ]
}
```

| Action | Meaning |
|---|---|
| `HOME` | Launch point, first waypoint of every cycle |
| `SPRAY_ON` | First point of a spraying run — open the nozzles here |
| `SPRAY` | Interior point of a spraying run |
| `SPRAY_OFF` | Last point of a spraying run — close the nozzles here |
| `SPRAY_ON_OFF` | Isolated single spray point — on and off at the same waypoint |
| `TRANSIT` | Non-spraying travel leg |
| `RTL` | Return to launch, last waypoint of every cycle |

`meta` carries the full parameter set alongside the derived speed and cycle count, so a plan is reproducible from the file alone.

### `flight_plan.gpkg`

The same mission in EPSG:4326, for dropping into QGIS next to the spray zones before anyone flies it:

| Layer | Geometry | Columns |
|---|---|---|
| `planned_path` | LineString | `cycle_id` — one line per charge cycle |
| `planned_waypoints` | Point | `cycle_id`, `seq`, `action`, `altitude` |

## Behavior Worth Knowing

- **The sprayer stays on between zones.** Every coverage waypoint is generated as a spraying point, so travel from one zone to the next is timed at spray speed and charged against the tank. This is the conservative direction — budgets are never underestimated — but on fields with widely separated zones it will plan more cycles than strictly needed, and `--transit-speed-mph` only affects the return-to-home legs.
- **A zone narrower than one swath** gets a single pass through its middle rather than being dropped.
- **Zone ordering is greedy nearest-neighbor**, not an optimal tour. Good enough at field scale; it does not claim minimal transit.
- **Zones are dissolved before planning**, so adjacent cells with different spray bands become one polygon flown at one rate. Banded `--spray-mode custom` output is planned at a single application rate, not per-band.
- **Distances use a local flat-earth approximation** (364,000 ft per degree latitude, longitude scaled by `cos(lat)`) — consistent with the rest of this pipeline and accurate at single-field scale.

## Container

Build:

```bash
apptainer build generate_flight_plan.sif generate_flight_plan.def
```

Run, binding your data directory:

```bash
apptainer run --bind /host/path/to/data:/data \
    generate_flight_plan.sif \
        --gpkg /data/farm_output.gpkg \
        --outdir /data/output \
        --nozzle-swath-ft 20 --flight-height-ft 15 \
        --tank-capacity-gal 5 --nozzle-flow-rate-gpm 0.5 \
        --dispersion-rate-gpa 2 \
        --battery-time-min 20 --battery-reserve-pct 20 \
        --home-lat 40.014361 --home-lon -83.042294
```

CPU only — do not pass `--nv`.

The geo stack comes from **conda-forge**, not pip: `geopandas`, `shapely`, `fiona`, `pyogrio` and `gdal` are C-library-backed, and conda-forge resolves them against one consistent GDAL/PROJ/GEOS build. The `%test` section imports the stack and prints the geopandas version, so a broken environment fails at build time.

## Pipeline Position

The middle stage — it consumes the geospatial product and emits a mission, which the exporter then adapts per platform:

```
generate_geopackage.py  →  output.gpkg  →  generate_flight_plan.py  →  flight_plan.json  →  export_flight_mission.py
                                                                    →  flight_plan.gpkg (QA)
```
