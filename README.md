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

The optional timestamp triggers processing directly whenever its state changes. A new packet carrying the same temperature is a fresh observation: a temperature state change is not required. The 15-second timer handles timeouts and also checks current source state. Each receipt timestamp counts at most once, regardless of how many automation runs it causes. **Recording the timestamp or internal helper is not required.**

If the timestamp is omitted, the most recent `last_updated` of either source provides evidence of reception: only silence in both sources times out. Unchanged states may not update this timestamp, so that fallback can discard legitimate idle intervals conservatively and cannot confirm identical packets that produce no source update. Neither route treats the absence of volume increases alone as a receiver gap. A receipt must describe a fresh meter dataset, rather than a replay of a cached dataset.

Temperature and volume should come from the same meter and be updated together. Processing waits two seconds to accommodate small write-order differences. This is not a join for independently delayed or backfilled histories; align those upstream.

## Decision flow and starting settings

1. A restart, settings change, unavailable source, or reception timeout clears the unfinished flush/window. The last accepted result survives. With the long-idle guard enabled, losing the flow baseline conservatively arms the guard.
2. The first fresh observation establishes a cumulative-volume baseline. Volume accumulated across an outage is discarded.
3. Positive volume deltas accumulate within a water-use session. Idle packets cannot start a measurement or add flushed litres.
4. In the default **batched mode**, a positive volume report that reaches the flush threshold opens a temperature confirmation window. One sufficiently large volume jump is enough during normal use; three separate volume increases are not required. An armed long-idle guard first requires the additional volume/time gates described below.
5. The flushed reading and two subsequent fresh observations must span at least 30 seconds and stay within 0.2 °C. Identical temperatures in new packets count. Confirmation observations are spaced by at least half the configured confirmation time. Once confirmed, publish the temperature and timestamp of the original flushed reading, rather than a later warmed value. No further acceptance occurs without another volume increase.
6. Any observed temperature excursion beyond the range rejects that window immediately, including observations between the spaced confirmation points. The window cannot slide into a later idle plateau: another volume increase is needed to open a new one. Reception failures, resets, and the flow-pause timeout also clear the window.
7. Optional **strict mode** retains version 1.0 behavior: three post-flush volume-increase-confirmed temperatures spanning 90 seconds, within 0.2 °C, produce their median.
8. The daily sensor keeps the minimum of accepted estimates and resets at local midnight. If Home Assistant misses midnight, a date guard clears yesterday's daily value on the next run.

| Setting | Default | Meaning |
| --- | --- | --- |
| Reporting pattern | Batched | Fresh packets confirm a flushed reading; strict mode remains selectable |
| Flush volume | 5 L | Water counted after a fresh baseline and within the current session |
| Temperature range | 0.2 °C | Maximum minus minimum throughout the confirmation window |
| Batch confirmation | 30 s | Minimum span of three fresh observations, including the flushed reading |
| Arm long-idle guard | 3 h | Time without a positive volume increase; zero disables the guard |
| Guarded flush volume | 10 L | Required in one guarded water-use session, at least the normal flush volume |
| Guarded initial delay | 120 s | Exclude early readings; a volume increase at or after this delay must arrive |
| Guarded confirmation | 30 s | Fresh-observation stability span after the volume and delay gates |
| Strict settling span | 90 s | Used only in strict mode |
| Flow pause | 150 s | No increase for longer ends the session and requires another flush |
| Reception timeout | 180 s | Missing receipt evidence invalidates the volume baseline |
| Freshness | 24 h | Age threshold for the held sensor's `fresh` attribute |

These are initial tunings. Set the reception timeout longer than your normal packet interval and the flow pause longer than the normal interval between meter increases during a draw. Keep batch confirmation shorter than the flow pause, with enough time for two fresh packets. The confirmation time is a minimum: with 20-second packets, the usual confirmation takes about 40 seconds. A short draw may still fail the flush or temperature-range requirement. A counter rollback clears the unfinished session and establishes a new baseline.

This is an estimate based on sufficient flushing plus a short stability check, not proof of ongoing flow during confirmation. A stable fresh packet after the draw is valid evidence for that check. The 5 L flush requirement does not guarantee thermal equilibration for every meter or installation; increase it if the meter still shows a cold dip or warm lag after that volume.

## Long-idle guard

The **Long-idle guard** input section makes all four thresholds configurable. By default, after three hours without a positive volume increase, the guard stays armed until a stronger measurement qualifies:

1. The first observed volume increase starts a guarded draw and its delay timer.
2. Accumulate at least 10 L in that draw and wait at least 120 seconds from its first increase. The threshold is never lower than your normal flush setting.
3. A positive volume increase at or after the delay must arrive before a confirmation window can open. Packets with unchanged volume cannot open it, regardless of elapsed time. Temperatures from before the gates are excluded completely.
4. Confirm the new reading with fresh packets spanning at least the guarded confirmation time, within the existing temperature-range setting. Identical temperatures in new packets count. Strict mode retains its three flow-confirmed samples and uses the longer of its normal span and the guarded confirmation time.
5. Only successful acceptance disarms the guard. Later draws return to normal qualification until another long idle period occurs.

A small handwash does not disarm the guard, even though it updates the last-flow time. Ending a session resets its accumulated litres, delay timer, and unfinished temperature window; the guard stays armed for the next draw. The existing flow-pause setting determines that boundary. Rejected temperature windows also leave the guard armed, and need a new volume increase to retry.

The last observed flow time survives ordinary idle periods inside the same Text helper. Initial installation, upgrade from older state, a counter reset, or loss of the flow baseline starts guarded because continuous flow history is unknown. Restarting/reloading the automation or changing its settings also starts guarded. Set **Arm after no water use** to `0` to disable this behavior.

The displayed last valid temperature remains held while guarding. Early guarded readings never update the held value or today's minimum. Existing accepted values and today's existing minimum are preserved on upgrade; historical readings are not reclassified. No additional helper, receipt timestamp recording, or integration dependency is required.

These thresholds are initial tunings, not a demonstrated calibration of the service pipe. A short draw after a long idle period may supply no new estimate; that is intentional.

## Outputs and history

The held sensor exposes `measured_at`, `age_hours`, `fresh`, `reception_healthy`, `last_observed_at`, `status`, `flushed_litres`, `long_idle_guard`, and `last_flow_at`. `status: guarding` identifies an armed guard with healthy reception. Its value can remain present while `fresh` is false or reception is unhealthy. It is unavailable until the first accepted measurement; Home Assistant may omit its custom attributes while unavailable.

The daily sensor exposes the local date, accepted-reading count, and last measurement time. A day without qualifying use is unavailable, rather than a warmed meter reading or yesterday's value. Acceptance counts are qualified estimates, not counts of independent draws. A flushed reading taken before midnight and confirmed after midnight updates the held sensor but is not counted toward the new day's minimum.

The held sensor intentionally has no statistics `state_class`, because averaging a stale held value would be misleading. The daily sensor uses `measurement`; for an annual daily-minimum chart, use **minimum** aggregation. Intraday means average a progressively falling daily minimum, rather than the actual incoming-water samples. Ensure Recorder includes the output sensors if you use include filters.

## Storage, reloads, and updates

The automation runs queued and reads the helper inside its action sequence, preventing simultaneous sensor updates from overwriting each other's state. It writes only when the encoded state changes. Never share a state helper between automation instances.

JSON version 1 uses: `p` prior volume in litres, `r` receipt time, `f` last flow time retained during ordinary idle, `l` session litres, `b` baseline flag, `z` reset time, `h` guard state, `q` pending confirmation data, `a` accepted `[time, temperature]`, `d` local date/minimum/count, and `c` settings. Guard state is `0` for unguarded, `1` for armed with no active guarded draw, or the guarded draw's first-flow timestamp. In batched mode, `q` holds `[anchor_offset, anchor_temperature, last_spaced_observation_offset, count, minimum, maximum]`; strict mode uses up to three `[offset, temperature]` pairs. Candidate time offsets are relative to `f`, keeping the state within 255 characters. Other times are Unix seconds. Settings indices 8–11 hold idle hours, guarded flush litres, initial delay, and guarded confirmation span; the original first eight positions stay compatible with the display sensors. Do not edit this internal state manually. Oversized state stops the run and preserves the previous helper value rather than truncating JSON.

Reloading automations or using **Run actions** deliberately resets the unfinished session and waits for fresh receipt evidence. It preserves the accepted result and today's minimum. The blueprint's source URL supports Home Assistant's **Re-import blueprint** workflow. Sensor YAML/template blueprint updates are separate from automation updates.

Version 1.1 defaults to batched mode and adds direct receipt-timestamp triggering. Existing instances automatically get the new defaults after re-import. The state migration discards unfinished work and preserves accepted values; companion sensors need no changes. Select strict mode to retain the previous qualification rule.

Version 1.2 adds the configurable long-idle guard, enabled by default. It starts armed on upgrade, preserves accepted values, and compresses candidate times inside the existing helper. Update the companion sensor YAML/template blueprint for guard-status attributes; the previous companion sensors remain compatible for temperature display.

## Development

The tests execute the shipped Jinja reducer, including guard thresholds, cold-dip exclusion, short-draw bypass prevention, guard release/rearming, state migration and size, coarse volume batches, unchanged-temperature packets, duplicate-run rejection, warming-window rejection, gap recovery, idle reception, counter resets, restart recovery, midnight rollover, strict stability checks, and dense updates.

```sh
uv run python -m unittest discover -s tests -v
uv run python tools/build_package.py
```

The generator creates both inline examples from the two template blueprint sources. Python is only used for development. See [LICENSE](LICENSE).
