# Incoming cold water temperature

Home Assistant automation blueprint that estimates incoming water temperature after water has flushed the meter and the temperature has settled. Designed for meters that warm toward room temperature while water is idle.

Works with ordinary sensor entities from any integration. **No MQTT dependency, custom integration, Python runtime, or external service is needed in Home Assistant.**

[![Import automation blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fandrekuehne%2Fha_cold_water_temperature%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fcold_water_temperature.yaml)

## Entities

One automation, one Text helper, and two output sensors:

| Entity in the example | Purpose |
| --- | --- |
| `input_text.incoming_water_state` | All persistent state as compact JSON; maximum length 255 |
| `sensor.incoming_water_temperature` | Last accepted estimate; holds through idle periods and reception gaps |
| `sensor.incoming_water_temperature_today` | Minimum accepted estimate today; unavailable until a reading qualifies |
| `automation.incoming_water_temperature_estimator` | Processes source changes and checks continuity |

Blueprints do not create their companion helpers automatically. Install the helper and sensors first using either route below.

## Install

### Route A: package

1. If packages are not already enabled, merge this into the existing `homeassistant:` configuration:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages/
   ```

2. Copy [examples/package.yaml](examples/package.yaml) to `/config/packages/incoming_water_temperature.yaml`. This creates the helper and both sensors. Leave `initial` unset so the helper restores its state after restart.
3. Validate the configuration and restart Home Assistant to load the new YAML helper.
4. Import the automation blueprint using the button above, create an automation, and select your sources and the helper. Configure the volume conversion before enabling it.

### Route B: existing or UI Text helper

1. Create a Text helper named **Incoming water state**, set maximum length to **255**, and verify its entity ID is `input_text.incoming_water_state`.
2. Add the contents of [examples/sensors_only.yaml](examples/sensors_only.yaml) as the list under your existing `template:` configuration. If using a package, put `template:` before that list. Change `helper_entity` if your helper has a different entity ID. Do not add a second `template:` key to an existing mapping.
3. Validate the configuration and reload Template entities.
4. Import and configure the automation blueprint as above.

Choose one route; do not define the same helper in both YAML and the UI. For additional meters, use a separate helper, distinct sensor names/unique IDs, and a separate automation instance.

Two optional [template blueprints](blueprints/template/) are also supplied for manual YAML use. They require Home Assistant 2024.11 or newer; see the [official template blueprint documentation](https://www.home-assistant.io/integrations/template/#using-blueprints). The inline examples avoid needing to install them. The automation blueprint requires 2024.10 or newer. Live installation was checked on Core 2026.10.1; older versions have not been runtime-tested.

## Source requirements

- **Temperature:** a numeric water-temperature sensor in °C.
- **Volume:** a numeric cumulative meter. Use `1000` litres per unit for m³, `1` for litres, or the appropriate conversion for your meter. Do not select a flow-rate sensor or daily resetting utility meter.
- **Last received (optional):** a timestamp sensor whose **state becomes the actual receipt time on every fresh packet**, even if temperature and volume stay unchanged. For example, `2026-10-20T10:20:30+00:00`. It must cover reception of both selected measurements, rather than an unrelated device heartbeat. Do not use a template that simply returns `now()`; that would hide outages.

The optional timestamp is how the estimator can prove reception continued during zero flow. It is checked every 15 seconds. If omitted, the most recent `last_updated` of either source provides evidence of reception: only silence in both sources times out. Unchanged states may not update this timestamp, so that fallback can discard legitimate idle intervals conservatively. Neither route treats the absence of volume increases alone as a receiver gap.

Temperature and volume should come from the same meter and be updated together. Processing waits two seconds to accommodate small write-order differences. This is not a join for independently delayed or backfilled histories; align those upstream.

## Decision flow and starting settings

1. A restart, settings change, unavailable source, or reception timeout clears the unfinished flush/window. The last accepted result survives.
2. The first fresh observation establishes a cumulative-volume baseline. Volume accumulated across an outage is discarded.
3. Positive volume deltas accumulate within a water-use session. Idle packets establish continuity but never add temperature candidates.
4. Once the flush threshold is reached, volume increases can supply temperature candidates. Candidates are spaced by at least half the configured settling span.
5. Three candidates spanning the settling period and staying within the configured temperature range qualify. Their median becomes the new estimate. A sliding window can qualify again during continued use.
6. The daily sensor keeps the minimum of accepted estimates and resets at local midnight. If Home Assistant misses midnight, a date guard clears yesterday's daily value on the next run.

| Setting | Default | Meaning |
| --- | --- | --- |
| Flush volume | 5 L | Water counted after a fresh baseline and within the current session |
| Settling range | 0.2 °C | Maximum minus minimum across three candidates |
| Settling span | 90 s | Minimum time from first to third candidate |
| Flow pause | 150 s | No increase for longer ends the session and requires another flush |
| Reception timeout | 180 s | Missing receipt evidence invalidates the volume baseline |
| Freshness | 24 h | Age threshold for the held sensor's `fresh` attribute |

These are initial tunings. Set the reception timeout longer than your normal packet interval and the flow pause longer than the normal interval between meter increases during a draw. A short toilet flush may never satisfy both flush and settling requirements; it then supplies no estimate. A counter rollback clears the unfinished session and establishes a new baseline.

## Outputs and history

The held sensor exposes `measured_at`, `age_hours`, `fresh`, `reception_healthy`, `last_observed_at`, `status`, and `flushed_litres`. Its value can remain present while `fresh` is false or reception is unhealthy. It is unavailable until the first accepted measurement; Home Assistant may omit its custom attributes while unavailable.

The daily sensor exposes the local date, accepted-reading count, and last measurement time. A day without qualifying use is unavailable, rather than a warmed meter reading or yesterday's value. Acceptance counts are sliding-window estimates, not counts of independent draws.

The held sensor intentionally has no statistics `state_class`, because averaging a stale held value would be misleading. The daily sensor uses `measurement`; for an annual daily-minimum chart, use **minimum** aggregation. Intraday means average a progressively falling daily minimum, rather than the actual incoming-water samples. Ensure Recorder includes the output sensors if you use include filters.

## Storage, reloads, and updates

The automation runs queued and reads the helper inside its action sequence, preventing simultaneous sensor updates from overwriting each other's state. It writes only when the encoded state changes. Never share a state helper between automation instances.

JSON version 1 uses: `p` prior volume in litres, `r` receipt time, `f` last flow time, `l` session litres, `b` baseline flag, `z` reset time, `q` up to three `[time, temperature]` candidates, `a` accepted `[time, temperature]`, `d` local date/minimum/count, and `c` settings. Times are Unix seconds. Do not edit this internal state manually. Oversized state stops the run and preserves the previous helper value rather than truncating JSON.

Reloading automations or using **Run actions** deliberately resets the unfinished session and waits for fresh receipt evidence. It preserves the accepted result and today's minimum. The blueprint's source URL supports Home Assistant's **Re-import blueprint** workflow. Sensor YAML/template blueprint updates are separate from automation updates.

## Development

The tests execute the shipped Jinja reducer, including gap recovery, idle reception, counter resets, restart recovery, midnight rollover, stability checks, and dense updates.

```sh
uv run python -m unittest discover -s tests -v
uv run python tools/build_package.py
```

The generator creates both inline examples from the two template blueprint sources. Python is only used for development. See [LICENSE](LICENSE).
