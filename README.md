# ADXL345 Probe
[![](https://dcbadge.vercel.app/api/server/APw7rgPGPf)](https://delta2.eu/discord)
[![CC BY-NC-SA 4.0][cc-by-nc-sa-shield]][cc-by-nc-sa]

**!!! This project is in a BETA state, use at your own risk !!!**

The ADXL345 has an interesting feature called tap detection. With the appropriate tuning, this can be used to implement a nozzle probe on 3D printers.
This project aims to support nozzle probing through tap detection for printers using Klipper.

You can watch this thing in action here:
[https://www.youtube.com/shorts/_qd0kMkrVZw](https://www.youtube.com/shorts/_qd0kMkrVZw)

Results you can expect for a properly tuned system (This was on a Voron Trident):

```
probe accuracy results: maximum 0.007500, minimum 0.000937, range 0.006563, average 0.004031, median 0.004219, standard deviation 0.001841
```

I also measured the force on the bed using a standard kitchen scale, this was approximately 200g. A CAN bus board was used, so a direct connection might result in a quicker stop (See Multi MCU homing in the Klipper docs for more information on this).

This fork targets Klipper **after** the `probe.py` refactor that removed
`ProbeSessionHelper` — see [Klipper compatibility](#klipper-compatibility).

## Installation

```bash
cd $HOME
git clone https://github.com/yzheka/adxl345-probe.git
cd adxl345-probe
./scripts/install.sh
```

`install.sh` symlinks `adxl345_probe.py` into `~/klipper/klippy/extras/` and
restarts Klipper, so a `git pull` here updates the installed module in place.

## Klipper compatibility

Klipper's `probe.py` has been through two API changes. This fork targets the
newest one:

| Klipper | Helper classes in `probe.py` | This fork |
| ------- | ---------------------------- | :-------: |
| ≤ v0.12.0 | `PrinterProbe`, `ProbeEndstopWrapper`, `ProbePointsHelper` | ✗ |
| v0.13.0 | adds `ProbeCommandHelper`, `HomingViaProbeHelper`, `ProbeSessionHelper`, `ProbeOffsetsHelper` | ✗ (use upstream) |
| after v0.13.0 | `ProbeSessionHelper` split into `ProbeParameterHelper`, `SampleAveragingHelper`, `DescendToEndstopHelper`, `LookupZSteppers` | ✓ |

Check which you have:

```bash
grep "^class " ~/klipper/klippy/extras/probe.py
```

If you see `ProbeParameterHelper`, use this fork. If you see
`ProbeSessionHelper` instead, use [upstream](https://github.com/jniebuhr/adxl345-probe).
Loading the wrong one fails at startup with
`AttributeError: module 'extras.probe' has no attribute '...'`.

## Physical setup

This code requires the ADXL int1 or int2 pins to be wired to one of your boards (preferrably the one that controls Z motion).
For a ADXL345 breakout board, simply run a wire. If you're using a CAN toolboard, the following boards are supported as they have wired the pins:

## Supported Boards

| Board  | Supported | int_pin | probe_pin | Link |
| ------ | :-------: | ------- | --------- | ---- |
| Mellow Fly SB2040 (v1/v2) | ✓ | int1 | gpio21 | https://aliexpress.com/item/1005004675264551.html |
| Mellow Fly SHT36 v2 | ✓ | int1 | PA10 | https://aliexpress.com/item/1005004675264551.html |
| Huvud | ✓ | ? | ? | |
| NiteHawk | ✓ | int1 | gpio21 |
| EBB36 | with soldering | int1/int2 | choose | |

## Configuration

This configuration must be **below** your `[adxl345]` section — the module
looks the accelerometer up at load time.

```
[adxl345_probe]
probe_pin: <pin wired to int1 or int2>
int_pin: int1
z_offset: 0
tap_thresh: 12000
tap_dur: 0.01
speed: 5
probe_accel: 1000
samples: 3
sample_retract_dist: 3.0
samples_result: median
samples_tolerance: 0.01
samples_tolerance_retries: 20
```

If you want to use the probe as Z endstop as well:

```
[stepper_z]
... your remaining config ...
endstop_pin: probe:z_virtual_endstop
```

Make sure to remove `position_endstop` in this case.

## Parameter reference

### Wiring

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `probe_pin` | **required** | MCU pin wired to the accelerometer's INT1 or INT2 output. Accepts the usual Klipper modifiers: `!` inverts, `^` enables the internal pullup. **Do not use `^`** unless you know you need it — the ADXL345 interrupt output is push-pull and a pullup makes the pin read permanently triggered, which produces zero-travel probes. Must be on the MCU that drives Z, or accept [multi-MCU homing](https://www.klipper3d.org/Multi_MCU_Homing.html) overshoot. |
| `int_pin` | **required** | Which accelerometer interrupt output carries the tap: `int1` or `int2`. Prefix with `!` (e.g. `!int1`) to set the ADXL345's INT_INVERT bit, making the interrupt active-low. This is a *chip* setting and is separate from the `!` modifier on `probe_pin`, which inverts the MCU's reading. |
| `chip` | `adxl345` | Name of the `[adxl345]` section to use. For a named section such as `[adxl345 hotend]`, set `chip: adxl345 hotend`. |

### Tap detection

| Parameter | Default | Range | Description |
| --------- | ------- | ----- | ----------- |
| `tap_thresh` | `5000` | 613 – 100000 | Tap threshold in mm/s². Written to the ADXL345 `THRESH_TAP` register at 62.5 mg/LSB, i.e. **612.9 mm/s² per step** — values between steps are truncated, so 12000 and 12500 both give register value 19. Lower is more sensitive. Too low and the move's own acceleration trips it. |
| `tap_dur` | `0.01` | >0.000625 – 0.1 | Maximum duration in seconds for an event to count as a tap. Written to the `DUR` register at 0.625 ms/LSB, so 0.01 s → 16. Acceleration must rise above `tap_thresh` and fall back below it inside this window; a sustained acceleration is not a tap. |
| `rest_time` | `0.1` | 0 – 1 | Settle dwell in seconds, applied both before arming and after disarming tap detection. Exists so ringing from the retract move doesn't trip the tap the instant it's armed. Costs 2 × `rest_time` per sample; lower it to speed up multi-sample probing, raise it if you get spurious triggers. |

### Probing motion

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `speed` | `5.0` | Probing speed in mm/s. |
| `lift_speed` | value of `speed` | Speed in mm/s for retract moves between samples. |
| `probe_accel` | unset | Acceleration limit in mm/s² applied to the **probing move only**, restored afterwards. The acceleration transient at the start of the move is the usual cause of false triggers, so this is the main knob for them. Ignored if the current limit is already lower. On a delta the effective Z acceleration is `min(max_accel, max_z_accel)`, so this only bites if it is below both. |
| `min_probe_travel` | `0.5` | Minimum descent in mm before a trigger is accepted. A trigger inside this distance raises a clear error instead of returning a bogus Z. Must be **smaller than `sample_retract_dist`**, or every sample after the first is rejected. Set to `0` to disable. |

### Offsets

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `z_offset` | **required** | Trigger height in mm. For a nozzle tap probe this is normally `0` — set it with `PROBE_CALIBRATE`. |
| `x_offset` | `0.0` | X offset of the probe from the nozzle. Leave at 0 for nozzle tap probing. |
| `y_offset` | `0.0` | Y offset of the probe from the nozzle. Leave at 0 for nozzle tap probing. |

### Sampling

Standard Klipper probe parameters, handled by `probe.ProbeParameterHelper`.

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `samples` | `1` | Number of probes per point. |
| `sample_retract_dist` | `2.0` | Z lift in mm between samples. Also the distance re-descended at `speed`, so it dominates multi-sample probing time. |
| `samples_result` | `average` | `average` or `median`. |
| `samples_tolerance` | `0.100` | Maximum spread in mm across a sample set before it is discarded and retried. |
| `samples_tolerance_retries` | `0` | How many times to retry a sample set that exceeds the tolerance. |

### Extras

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `disable_fans` | empty | Comma-separated list of fan object names (e.g. `fan_generic toolhead_fan`) to switch off during a probe session and restore afterwards. Fan vibration is a common source of false taps. |
| `activate_gcode` | empty | G-code template run before each probing move. |
| `deactivate_gcode` | empty | G-code template run after each probing move. |

## Commands

| Command | Description |
| ------- | ----------- |
| `SET_ACCEL_PROBE [TAP_THRESH=<mm/s²>] [TAP_DUR=<s>] [ACCEL=<mm/s²>]` | Adjust tap threshold, tap duration and probing acceleration at runtime and echo the resulting values. Not saved — put the final numbers in the config. |
| `PROBE` | Single probe at the current XY. |
| `QUERY_PROBE` | Report the current state of the probe pin. Should read `open` with the nozzle in free air. |
| `PROBE_ACCURACY` | Repeat-probe at the current XY and report the spread. |
| `PROBE_CALIBRATE` | Probe, then start a manual probe to determine `z_offset`. |
| `Z_OFFSET_APPLY_PROBE` | Write the current Z offset back into the config. |

## Tuning guide

Work through this in order. Every step assumes the previous one passes.

**1. Check the pin.** With the nozzle in free air, `QUERY_PROBE` must report
`open`. If it reports `TRIGGERED`, fix the wiring or the `probe_pin` / `int_pin`
polarity before going any further — nothing downstream will work.

**2. Trigger by hand.** Home, move to a safe height, run `PROBE` and tap the
nozzle with a finger as it descends. It should stop immediately. This proves
the whole chain works before the bed is involved.

**3. Kill the false triggers.** Start with `probe_accel: 500` and
`tap_thresh: 12000`. If probes stop instantly you will see:

```
ADXL345 probe triggered after only 0.000mm of travel (minimum 0.500mm) ...
```

Lower `probe_accel` first — it reduces the stimulus and does not affect
sensitivity to the actual bed contact. Raise `tap_thresh` only if lowering
acceleration is not enough. Note the 612.9 mm/s² register granularity: changes
smaller than that do nothing.

**4. Check repeatability.** `PROBE_ACCURACY` with `samples: 10`. A well-tuned
setup gives a range under 0.01 mm. If it drifts in one direction across
samples, the bed or gantry is deflecting and you need a lower `tap_thresh`
(lighter contact), not a higher one.

**5. Set the offset.** `PROBE_CALIBRATE`, then `Z_OFFSET_APPLY_PROBE`.

**6. Speed it up.** Reduce `rest_time` toward 0.03 and `sample_retract_dist`
toward 1.0, re-running `PROBE_ACCURACY` after each change. Back off as soon as
repeatability degrades.

## Delta printers

Deltas work, with three things to know.

**The effector homes at `max_z`.** `G28` parks at the top of the build volume,
so `PROBE` from the home position descends the full build height at `speed` —
about 84 s for a 420 mm delta at 5 mm/s. Move down first:

```
G28
G90
G1 Z10 F1000
PROBE_ACCURACY
```

**A zero-travel trigger used to fail confusingly.** Klipper's probe commands
retract `sample_retract_dist` from wherever the trigger happened. At the home
position there is no headroom, so the retract exceeded `max_z` and you got
`Move out of range: ... 422.220` rather than anything useful. `min_probe_travel`
now catches this first and says what actually went wrong.

**There is no `[stepper_z] position_min`.** The probe's descent floor comes from
`minimum_z_position` in `[printer]`, which defaults to `0`. If the nozzle needs
to travel below nominal zero to make contact, set it there:

```
[printer]
kinematics: delta
minimum_z_position: -2
```

## Troubleshooting

| Message | Cause |
| ------- | ----- |
| `AttributeError: module 'extras.probe' has no attribute 'ProbeSessionHelper'` | Module too old for your Klipper — use this fork. See [Klipper compatibility](#klipper-compatibility). |
| `AttributeError: module 'extras.probe' has no attribute 'ProbeParameterHelper'` | Module too new for your Klipper — use upstream, or update Klipper. |
| `ADXL345 probe pin reads TRIGGERED while the tap register is clear` | `probe_pin` polarity. Remove a `^` pullup, or add `!` if the interrupt idles high. |
| `ADXL345 tap triggered before move, it may be set too sensitive.` | The tap latched while arming. Raise `tap_thresh`, raise `rest_time`, or check for fan/motor vibration. |
| `ADXL345 probe triggered after only N mm of travel` | False trigger on the start-of-move acceleration. Lower `probe_accel`, then raise `tap_thresh`. |
| `Move out of range` right after a probe | The trigger happened with no headroom for the retract. Set `min_probe_travel` above 0 to get the real error, and probe from a lower Z. |
| `Failed to set ADXL345 register [0x..] to 0x..: got 0x..` | SPI communication problem — wiring, bus contention, or too high an SPI speed. Klipper's `adxl345.set_reg()` verifies every write, so this is a genuine bus fault, not a tuning issue. |

## License

This work is licensed under a
[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License][cc-by-nc-sa].

[![CC BY-NC-SA 4.0][cc-by-nc-sa-image]][cc-by-nc-sa]

[cc-by-nc-sa]: http://creativecommons.org/licenses/by-nc-sa/4.0/
[cc-by-nc-sa-image]: https://licensebuttons.net/l/by-nc-sa/4.0/88x31.png
[cc-by-nc-sa-shield]: https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg?style=for-the-badge
