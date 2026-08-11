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

## Moonraker update manager

Add this to `~/printer_data/config/moonraker.conf` for update notifications and
one-click updates in Mainsail or Fluidd:

```ini
[update_manager adxl345-probe]
type: git_repo
path: ~/adxl345-probe
origin: https://github.com/yzheka/adxl345-probe.git
primary_branch: master
managed_services: klipper
info_tags:
    desc=ADXL345 Probe
```

Then restart Moonraker:

```bash
sudo systemctl restart moonraker
```

Notes:

- `path` is the git clone from [Installation](#installation), **not** the
  symlink in `klippy/extras`. Updating the clone updates the installed module,
  because `install.sh` symlinks rather than copies.
- `managed_services: klipper` restarts Klipper after an update, which is
  required for the new module to be loaded.
- No `install_script` is needed. The symlink survives a `git pull`, so running
  `install.sh` again would only re-create it and issue a redundant second
  Klipper restart. Add `install_script: scripts/install.sh` only if you have
  moved or removed the symlink.
- Moonraker will not manage a repository that isn't pristine. Commit and push
  local edits, keep the working tree clean, and stay on a branch that exists on
  `origin` — a detached HEAD or a local-only branch shows up in the UI as an
  invalid repo offering only a hard recovery.
- If you track your own fork, `origin` and `primary_branch` must match it.

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
| `tap_thresh` | `5000` | 613 – 100000 | Tap threshold in mm/s². Written to the ADXL345 `THRESH_TAP` register at 62.5 mg/LSB, i.e. **612.9 mm/s² per step** — values between steps are truncated, so 12000 and 12500 both give register value 19. Lower is more sensitive, but **only down to 9807** — see [The 1 g floor](#the-1-g-floor). Too low and the move's own acceleration trips it. |
| `tap_dur` | `0.01` | >0.000625 – 0.1 | Maximum duration in seconds for an event to count as a tap. Written to the `DUR` register at 0.625 ms/LSB, so 0.01 s → 16. Acceleration must rise above `tap_thresh` and fall back below it inside this window; a sustained acceleration is not a tap. |
| `rest_time` | `0.1` | 0 – 1 | Settle dwell in seconds, applied both before arming and after disarming tap detection. Exists so ringing from the retract move doesn't trip the tap the instant it's armed. Costs 2 × `rest_time` per sample; lower it to speed up multi-sample probing, raise it if you get spurious triggers. |

#### The 1 g floor

**`tap_thresh` below 9807 mm/s² does not work at all**, and it fails in the
direction nobody expects: the probe stops triggering entirely.

`THRESH_TAP` is compared against *total* acceleration. The ADXL345's tap
detector is not AC coupled, so the 1 g the effector is already carrying counts
towards the threshold. A tap is only latched when the acceleration rises above
the threshold **and falls back below it** inside the `tap_dur` window. Below 1 g
the threshold is permanently exceeded by the effector just sitting there, the
fall-back never happens, and no tap is ever registered:

| `tap_thresh` | Register | In g | Result |
| --- | --- | --- | --- |
| 1000 | 1 | 0.06 g | `No trigger on probe after full movement` |
| 5000 | 8 | 0.50 g | `No trigger on probe after full movement` |
| 9807 | 16 | 1.00 g | exactly 1 g — still no trigger |
| 10420 | 17 | 1.06 g | lowest setting that can work |
| 12000 | 19 | 1.19 g | fine |

1 g lands exactly on register 16, so **17 is the lowest usable register**. Note
the module's own `tap_thresh` default of `5000` is inside the dead zone — it is
inherited from upstream and has to be raised.

`ADXL_PROBE_CALIBRATE` defaults its `THRESHOLD_START` to 10420 — register 17 —
so a bare run never goes near the dead zone. If you point it lower, it starts
where you say but does not *probe* the dead rungs: whether a register can latch
is arithmetic, and a probe that cannot latch runs its move to the descent floor
with the nozzle loaded against the bed for the whole of it. Those rungs are
counted in the opening summary and stepped over.

The symptom is the giveaway: a threshold that is too *low* reads as
`triggered after only 0.000mm of travel` (a misfire) only while it is above the
floor. Below the floor it reads as no trigger at all, which looks identical to a
threshold that is far too high.

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
| `disable_fans` | empty | Comma-separated list of fans to switch off for the duration of a probe session and restore afterwards. Fan vibration is a common source of false taps. See [Disabling fans](#disabling-fans). |
| `activate_gcode` | empty | G-code template run before each probing move. |
| `deactivate_gcode` | empty | G-code template run after each probing move. |

## Commands

| Command | Description |
| ------- | ----------- |
| `SET_ACCEL_PROBE [TAP_THRESH=<mm/s²>] [TAP_DUR=<s>] [ACCEL=<mm/s²>]` | Adjust tap threshold, tap duration and probing acceleration at runtime and echo the resulting values. Not saved — put the final numbers in the config. |
| `ADXL_PROBE_CALIBRATE [X=] [Y=] [Z=] [DEVIATION=] [SPEED_START=] [SPEED_END=] [SPEED_STEP=] [THRESHOLD_START=] [THRESHOLD_END=] [THRESHOLD_STEP=] [SAMPLES=] [LIFT=] [ACCURACY_MAX=] [TRAVEL_SPEED=]` | Home if needed, move to the middle of the bed, and find the `speed` and `tap_thresh` pair that probes most accurately. Applies them and stages them for `SAVE_CONFIG`. See [Calibrating speed and tap_thresh automatically](#calibrating-speed-and-tap_thresh-automatically). |
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

Once `probe_accel` is settled, `ADXL_PROBE_CALIBRATE` finds `speed` and
`tap_thresh` for you — see
[Calibrating speed and tap_thresh automatically](#calibrating-speed-and-tap_thresh-automatically).

**4. Check repeatability.** `PROBE_ACCURACY` with `samples: 10`. A well-tuned
setup gives a range under 0.01 mm. If it drifts in one direction across
samples, the bed or gantry is deflecting and you need a lower `tap_thresh`
(lighter contact), not a higher one.

**5. Set the offset.** `PROBE_CALIBRATE`, then `Z_OFFSET_APPLY_PROBE`.

**6. Speed it up.** Reduce `rest_time` toward 0.03 and `sample_retract_dist`
toward 1.0, re-running `PROBE_ACCURACY` after each change. Back off as soon as
repeatability degrades.

## Disabling fans

Every fan is off for the whole probe session and restored when it ends:

```ini
[adxl345_probe]
disable_fans: fan, hotend_fan, fan_generic toolhead_fan
```

Both naming styles work. `fan` is the part cooling fan (`[fan]`), and for a
section with a name — `[heater_fan hotend_fan]` — you can write either the bare
name `hotend_fan` or the full object name `heater_fan hotend_fan`. Use the full
name if the bare one is ambiguous; the module says so at startup if it is.

All fan types are supported: `[fan]`, `[fan_generic]`, `[heater_fan]`,
`[controller_fan]` and `[temperature_fan]`. `heater_fan`, `controller_fan` and
`temperature_fan` drive themselves from a periodic callback, so those are
pinned off for the session rather than merely set to zero once — otherwise they
would come back on a second later, in the middle of a probe. The fan's own
`max_power` is held at zero for the same reason, which also covers a
`[fan_generic]` being driven by `SET_FAN_SPEED TEMPLATE=...`, since a template
re-evaluates every 0.5 s and no section-level setting stops it.

The speed restored afterwards is the one that was *requested*, including a
request still queued when the probe started. Klipper reports fan speed
post-scaling, so a `max_power: 0.6` fan would otherwise come back at 0.36, and
lose another 40% on every probe after that.

A name that matches nothing is a **startup** error listing the fans that do
exist, rather than an exception in the middle of a probe.

Fans are restored when the probe session ends — including when it ends badly.
Klipper's `PROBE` and `PROBE_ACCURACY` only call `end_probe_session()` on the
success path and rely on the `gcode:command_error` event otherwise, so this
module hooks that event as well; a misfired probe puts the fans back rather
than leaving them off until the next successful one. A failure *inside* session
startup, which the probe helper never sees, restores them too.

Note that a hotend fan being off means the hotend is unattended while hot.
Probe sessions are short, but do not run a `PROBE_ACCURACY` with hundreds of
samples at printing temperature with the hotend fan disabled.

## Calibrating speed and tap_thresh automatically

`speed` and `tap_thresh` are not independent. A faster probe hits the bed
harder, so it takes a higher threshold before the move's own acceleration stops
tripping the tap — and a threshold that works at 10 mm/s may miss the bed
entirely at 25. `ADXL_PROBE_CALIBRATE` walks `tap_thresh` up until the probe
taps at each speed, measures how repeatable that tap is, and keeps the most
accurate pair.

```
ADXL_PROBE_CALIBRATE
```

That is the whole thing. It homes if any axis is unhomed, travels to the middle
of the bed and probes from `Z=10` — no `G28`/`G1` preamble. Pass `X=`, `Y=` or
`Z=` to override any of that.

It refuses to start while a print is running or paused — see
[Running during a print](#running-during-a-print).

### What it does

For each speed from `SPEED_START` to `SPEED_END`:

**1. Walk `tap_thresh` up.** Starting at `THRESHOLD_START`, tap once. Rungs at
or below 1 g are logged and stepped over rather than probed — see
[The 1 g floor](#the-1-g-floor). Each attempt that does run ends one of three
ways:

| Result | Meaning | What the nozzle does |
| ------ | ------- | -------------------- |
| `sensitive` | Misfired: the tap latched while arming, or fired on the start-of-move acceleration. Threshold too low. | Stops in the first fraction of a millimetre — **never reaches the bed** |
| `pass` | Descended past `min_probe_travel` and triggered. | A normal tap |
| `deaf` | Ran the whole move without triggering. Threshold too high. | **Drives into the bed** and keeps pushing to the descent floor |

A misfire means `tap_thresh` goes up by `THRESHOLD_STEP` and it taps again.
This is the direction that spares the bed: a threshold that is too low fails
before the effector has descended, so the climb costs one contactless probe per
step. A `deaf` result ends the speed — nothing higher can be more sensitive.

**2. Measure the accuracy.** The tap that found the threshold is tap #1 of the
measurement: it already landed on a spot, so the remaining `SAMPLES` - 1 taps go
to the same place, lifting `LIFT` mm between them rather than returning to `Z`.
Nothing moves in XY from there on — the point is to measure the probe, and moving
between taps would fold the shape of the bed into the result. `SAMPLES` taps in
total, all of them counted.

The **average trigger height** of those taps is the accuracy for that pair —
strictly, its distance from nominal Z=0, since the trigger position itself is
usually negative. Contact below zero is the normal case for a nozzle probe: the
tap latches only after the effector has pressed in far enough for the chip to
see it, so the reported position sits below the nominal plane. A smaller
distance is a probe that felt the bed sooner, with less deflection.

An accuracy worse than `ACCURACY_MAX` (0.1 mm) is not a measurement of the bed
at all — the tap is latching on something other than the contact, or the
effector is deflecting that far first. That threshold is treated as unusable and
the walk carries on upward, exactly as it does for a misfire.

The spread and sigma are reported alongside the average.

If the accuracy run breaks down part way, that threshold only works
intermittently: the walk carries on upward instead of giving up on the speed.

**3. Start over at the next speed.** Every speed walks again from
`THRESHOLD_START`, so a band that moves — in either direction — is always
found.

The pairs are then ranked by accuracy — distance from zero, closest first —
ties broken by the smaller spread. The winner is applied immediately and staged for
`SAVE_CONFIG`, which the command does **not** run for you:

```
ADXL_PROBE_CALIBRATE: best accuracy was 0.0062 mm at speed 14 with tap_thresh 34000.
Both are applied now and staged for [adxl345_probe]:
speed: 14
tap_thresh: 34000
Run SAVE_CONFIG to keep them - it restarts Klipper.
```

Only `pass`, `sensitive` and `deaf` are verdicts. Anything else — a tap latch
that would not clear, an SPI readback mismatch, an MCU homing timeout, a move
out of range — is a **fault**. It says nothing about `tap_thresh`, so it fails
its own step, is reported as itself rather than as "this threshold misfired",
and the walk carries on to the next rung:

```
  speed  24.0  tap_thresh  14711  probe fault, step failed: ADXL345 probe pin reads TRIGGERED while the tap register is clear. ...
```

Losing a twenty-minute run to one passing glitch is worse than losing a rung.
But `CAL_MAX_FAULTS` faults **in a row** means it is not a glitch, and the run
stops and quotes the last one — a machine that faults on every tap will not
recover by being asked another hundred times. Any tap that answers, however it
answers, resets that streak. A printer shutdown still stops the run
immediately, since nothing after it can work.

An aborted run restores the previous `tap_thresh`, lifts back to the height it
started from, and stages nothing.

### Parameters

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `THRESHOLD_START` | `10420` | First `tap_thresh` tried at every speed, in mm/s². The default is register 17, the lowest that can latch a tap at all — see [The 1 g floor](#the-1-g-floor). The walk starts exactly where you put it, but rungs at or below 1 g are stepped over without being probed: nothing can latch there, and a probe would only drive the nozzle into the bed to prove it. |
| `THRESHOLD_END` | `100000` | Highest `tap_thresh` the walk will reach before giving up on a speed. |
| `THRESHOLD_STEP` | `613` | How much `tap_thresh` rises after a misfire, in mm/s². The chip stores the threshold at **612.9 mm/s² per register step**, and the default of 613 is exactly one of them: every register in the range gets tried, so a narrow band cannot be stepped over and the walk stops on the lowest threshold that works. Nothing finer is worth asking for — values landing on a register already tried are dropped, so a smaller step probes the same settings. A larger one trades resolution for time; see [Choosing the range](#choosing-the-range). |
| `SPEED_START` | `10` | First probing speed in mm/s. |
| `SPEED_END` | `30` | Last probing speed in mm/s. |
| `SPEED_STEP` | `2` | Increment. The defaults measure 10, 12, 14, … 30 mm/s — eleven speeds. Capped at 20 per run. |
| `X` | middle of the bed | Centre of the probing area. The default is the midpoint of the travel the kinematics report, less `x_offset`. |
| `Y` | middle of the bed | As `X`, less `y_offset`. |
| `DEVIATION` | `20` | Half-width in mm of the square around `X`/`Y` that the **walk's** taps are scattered over, so the climb does not dent one spot. Each tap of the walk picks a fresh random point in `X±dev`, `Y±dev`. The accuracy measurement never moves, whatever this is set to. `0` puts everything on the same spot. |
| `SAMPLES` | `10` | Taps per accuracy measurement. This is the number the ranking is built on — don't set it below about 5. |
| `ACCURACY_MAX` | `0.1` | Worst accuracy in mm that counts as a measurement. Beyond that the threshold is treated as unusable and the walk carries on up, the same as for a misfire. |
| `LIFT` | 2 × `min_probe_travel` | How far the nozzle rises between those taps, in mm, and therefore how far the next one descends. Must be above `min_probe_travel`, or every tap would trigger inside it and be read as a misfire — so the default is twice it, and the command refuses to start if you pass less. Never below 1 mm, for a `min_probe_travel` of 0. |
| `Z` | `10` | Height the first tap at each threshold descends from. Must clear anything on the bed. The move to the probing point also happens at this height — on a delta the reachable radius near the top of the travel is nil, so traversing at the height `G28` finishes at is out of range. |
| `TRAVEL_SPEED` | `50` | mm/s for the moves to the probing point. Not the probing speed — that is what the command is measuring. |
| `CHIP` | — | Accelerometer name, matching `chip:`. Optional; only useful if you have named your `[adxl345]` section. |

### Choosing the range

The defaults are chosen to be safe and exact rather than fast. Both threshold
values sit on the chip's register grid instead of round decimals, because that
grid is what the hardware actually has.

**`THRESHOLD_START=10420`** is register 17, the first that can latch a tap.
Nothing below it can work, so a lower start only adds rungs that get stepped
over. If you already know a `tap_thresh` that homes reliably, start ~2000 below
*that* instead — the band cannot be under it, and everything lower is wasted
climbing.

**`THRESHOLD_STEP=613`** is one register. That matters twice: a coarser step can
step clean over a narrow band, and because the walk stops at the *first*
threshold that works, landing two or three registers above the true bottom edge
means a harder tap than the machine needs — which shows up directly as a worse
average trigger height.

| Step | Probes to walk the full range | Registers skipped |
| --- | --- | --- |
| 100 | 147 | 0 |
| **613** | **147** | **0** |
| 1000 | 90 | 57 of 147 |
| 2000 | 45 | 89 |
| 5000 | 18 | 129 |

The first two rows are the same because rungs landing on an already-tried
register are dropped before probing: any step at or below 613 probes exactly one
setting per register. So 613 is the finest step worth asking for, and a larger
one is purely a time trade.

What full resolution costs, in extra misfires per speed — each a couple of
seconds, none of them touching the bed:

| Band starts at | step 613 | step 1000 |
| --- | --- | --- |
| reg 19 (11645 mm/s²) | 2 | 2 |
| reg 25 (15323 mm/s²) | 8 | 5 |
| reg 40 (24517 mm/s²) | 23 | 15 |
| reg 60 (36775 mm/s²) | 43 | 27 |

**Speeds.** Each one costs its climb plus `SAMPLES` measuring taps, so a minute
or two. For a first run, widen the step rather than narrowing the span — five
speeds show the trend as well as eleven:

```
ADXL_PROBE_CALIBRATE SPEED_STEP=5
```

Then re-run with `SPEED_STEP=2` over whichever part of the range looked useful.

Two things to check before sweeping to 30 mm/s. `[printer] max_z_velocity`
clamps every Z move, and a probing move is a Z move — if yours is below
`SPEED_END`, the top of the sweep is silently clamped and you will measure the
same actual velocity several times over, with a different `tap_thresh` found for
each. And impact force scales with speed: the top of the sweep is where the bed
takes the most punishment, which is the cost of finding out how fast you can
probe.

**Read the table, not just the winner.** The ranking picks the lowest average
trigger height, and two speed-dependent effects push that figure *down* as speed
rises: a faster probe needs a higher `tap_thresh`, which needs more force and so
more deflection before latching, and homing latency across a CAN toolhead puts
the reported trigger deeper in proportion to speed. If the averages in the
results table march monotonically with speed, the "winner" is just whichever end
of the range you swept to and it is not telling you much — pick on `range`
instead: the fastest speed whose range is still comfortably under 0.01 mm. If the
averages turn over somewhere in the middle, that is a real optimum and the pick
is worth taking.

### Read this before running it

- **This takes a while.** The defaults are eleven speeds, each walking up from
  the 1 g floor one register at a time, so roughly 540 probes for a machine
  whose threshold lands near 34000 — about 25 minutes. Only 110 of them touch
  the bed. See [Choosing the range](#choosing-the-range) for the two
  settings that cut it down.
- **Stand over the machine for the first run.** A probe at a threshold that is
  too high does not stop at the bed; the nozzle drives down to `position_min` /
  `minimum_z_position`. Walking up from the sensitive end means the run should
  never produce one, but keep a hand on the emergency stop until you have seen
  it work.
- Tune `probe_accel` and `rest_time` **before** running it. The result is only
  valid for the settings in force during the run.
- **Homing uses the current `tap_thresh`.** If the probe is so badly tuned that
  `G28` itself fails, the command cannot get started. Raise the threshold by
  hand first with `SET_ACCEL_PROBE TAP_THRESH=...`, or home once with a Z
  endstop, then run this.
- `DEVIATION` spreads the climb's taps over an area so they do not all land in
  one place. It does not affect the measurement, which always happens on a
  single spot.
- If `[adxl345_probe]` lives in an included file, `SAVE_CONFIG` will refuse to
  write it (Klipper cannot edit includes). Copy the two printed lines in by hand
  in that case.

### Sparing the bed

Two things protect the bed, and they are independent.

**The search direction.** Walking up from the sensitive end means a wrong
threshold misfires in the first fraction of a millimetre instead of pressing the
nozzle into the bed, and stopping at the first success means never probing past
the working threshold to see where it ends. Measured on a simulated
eleven-speed run whose band drifts with speed:

| | Total probes | Misfires (no contact) | Taps | Drove into the bed |
| --- | --- | --- | --- | --- |
| Bisecting down from `THRESHOLD_END` | 385 | 26 | 341 | 18 |
| `ADXL_PROBE_CALIBRATE` | 913 | 803 | **110** | **0** |

Ten bed contacts per speed, however far the walk had to climb: the tap that
finds the threshold is the first of the ten that measure it.

**The spot.** The walk's contacts are spread out by `DEVIATION`, which defaults
to 20 mm, so the climb does not land in one place. The ten measuring taps do all
land together, because that is the only way the average means anything: the
accuracy measurement never moves, whatever `DEVIATION` is set to.

The area is clipped to the travel the kinematics report, so a point near an edge
still works — the square is just cut short, and the run says so. On a delta,
where the reported range is the square bounding a round bed, the taps are also
kept inside the circle.

### Reading the output

The climb is quiet — a misfire is the expected outcome of a threshold that is
still too low, and one line per register step buries the results in hundreds.
What gets logged is the measurement: every tap of it, with the average
recomputed as it settles.

```
ADXL_PROBE_CALIBRATE: probing at X0.000 Y0.000 from Z10.000, climbing over X-20.000-20.000 Y-20.000-20.000
ADXL_PROBE_CALIBRATE: speeds 10, 12 mm/s, tap_thresh 10000 - 100000 mm/s^2 in 91 step(s) (the first 1 are at or below 1g and are skipped), 10 taps per measurement. ...
  tap #1:  speed  10.0  tap_thresh  25000  tapped at z -0.0187
  tap #2:  speed  10.0  tap_thresh  25000  accuracy 0.0193
  tap #3:  speed  10.0  tap_thresh  25000  accuracy 0.0182
  tap #4:  speed  10.0  tap_thresh  25000  accuracy 0.0180
  ...
  tap #10: speed  10.0  tap_thresh  25000  accuracy 0.0183
  tap #1:  speed  12.0  tap_thresh  27000  tapped at z -0.0211
  ...
ADXL_PROBE_CALIBRATE: results, best first
  1. speed  10.0  tap_thresh  25000  accuracy 0.0183  (average z -0.0183)  range 0.0040  sigma 0.0015  (10 taps)
  2. speed  12.0  tap_thresh  27000  accuracy 0.0218  (average z -0.0218)  range 0.0040  sigma 0.0014  (10 taps)
```

`tap #1` is the tap that found the threshold. It reports the height it triggered
at, because one tap is not an accuracy yet — and it is signed, because it is a toolhead position, and contact below
nominal zero is normal. From `tap #2` on, each line is the average distance from
zero over every tap so far at that threshold and speed, so you can watch it
converge — and see immediately if it does not. The last one is the figure the
speeds are ranked by. The results table shows the signed average next to it, so
the sign is not lost.

The results table adds `range` and `sigma` per pair. They are worth a look
before trusting the ranking: a range much larger than the differences between
speeds means the averages are inside the noise, and `probe_accel` is the first
thing to look at.

An average that grows steadily with speed is the normal picture — a faster probe
hits harder and needs a higher threshold, and a higher threshold lets the nozzle
travel further before latching. A speed reported as `unusable` is not a fault:
no threshold in the range worked there, the run says so and carries on.

### Failure messages

| Message | Meaning |
| ------- | ------- |
| `no speed produced a usable tap_thresh` | No speed worked at all. `probe_accel` is too high, or something is vibrating — a fan (see `disable_fans`), or a stepper. Check the pin with `QUERY_PROBE` first. |
| `speed N: unusable - every tap_thresh from A to B misfired` | Even the least sensitive setting in the range misfires at this speed. Raise `THRESHOLD_END`, or lower `probe_accel`. |
| `speed N: unusable - the most sensitive usable tap_thresh (M) did not feel the bed at all` | Not a threshold problem: nothing above M can be more sensitive. Check the pin with `QUERY_PROBE`, check `tap_dur` is long enough for the contact, and confirm a hand tap on the nozzle stops a `PROBE`. |
| `speed N: unusable - tap_thresh M already misses the bed` | The band ended below where the walk reached. Usually means the previous speed's band was much lower. |
| `speed N: unusable - tap_thresh M misses the bed part way through the accuracy run` | It tapped once, then stopped feeling the bed. Usually a bed that is deflecting under repeated contact. |
| `only N of M taps worked - raising tap_thresh` | Not a failure: that threshold is marginal, and the walk is carrying on upward. |
| `probe fault, step failed: ...` | Not fatal: that rung is abandoned and the walk carries on. The quoted message is the real cause. |
| `N probe faults in a row, so this is not a passing glitch - stopping` | The same fault on `N` consecutive taps. The quoted message is what to fix — a `probe_pin` polarity or pullup problem shows up like this. |
| `accuracy X is worse than Y - raising tap_thresh` | The taps triggered further than `ACCURACY_MAX` from zero, so they measured deflection rather than the bed. Also not fatal — the walk carries on up. |
| `LIFT=x is not above min_probe_travel=y` | The accuracy taps would trigger inside `min_probe_travel` and be read as misfires. Leave `LIFT` out and it defaults to twice `min_probe_travel`. |

### Running during a print

`ADXL_PROBE_CALIBRATE` homes the toolhead, drives to the middle of the bed and
taps it a few hundred times. Doing that partway through a print would destroy
it, so the command checks first and refuses:

```
!! ADXL_PROBE_CALIBRATE: not starting - a print job is printing. This command
homes the toolhead, drives to the middle of the bed and taps it a few hundred
times, which would wreck the print. Run it when the printer is idle. Nothing
has been changed.
```

It refuses by **returning**, not by raising an error. An error raised inside an
SD print makes Klipper break out of the print loop and stop the job — which is
the outcome the check exists to prevent. So a stray `ADXL_PROBE_CALIBRATE` in a
macro or a queued console command warns and does nothing; the print carries
on.

A job is considered active when `[print_stats]` reports `printing` or `paused`,
or when `[virtual_sdcard]` is streaming a file. `paused` counts: the part is
still on the bed. `complete`, `cancelled` and `standby` do not.

One limitation worth knowing: a job streamed line by line over the serial port
by a host that drives neither of those objects cannot be detected from inside
Klipper, and the check will not catch it.

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
| `ADXL345 probe pin reads TRIGGERED while the tap register is clear` | `probe_pin` polarity. Remove a `^` pullup, or add `!` if the interrupt idles high. If it happens only occasionally mid-run, the latch simply did not clear in time — raise `rest_time`. `ADXL_PROBE_CALIBRATE` treats it as a failed step and carries on. |
| `ADXL345 tap triggered before move, it may be set too sensitive.` | The tap latched while arming. Raise `tap_thresh`, raise `rest_time`, or check for fan/motor vibration. |
| `No trigger on probe after full movement`, at every `tap_thresh` you try | If the values you tried were below 9807, that is the [1 g floor](#the-1-g-floor) — the chip cannot latch a tap there. Otherwise check the pin, `tap_dur`, and that a hand tap stops a `PROBE`. |
| `ADXL345 probe triggered after only N mm of travel` | False trigger on the start-of-move acceleration. Lower `probe_accel`, then raise `tap_thresh` — or let `ADXL_PROBE_CALIBRATE` find it. |
| `ADXL_PROBE_CALIBRATE: G28 did not home X, Y and Z` | The command homed for you, but an axis is still unhomed afterwards. Usually a failed Z home — fix that first. |
| `the kinematics do not report a travel range` | The middle of the bed cannot be worked out for this kinematics. Pass `X=` and `Y=` explicitly. |
| `AttributeError: 'PrinterFan' object has no attribute 'fan_speed'` | Old version of this module: it assumed every fan section had a `fan_speed` attribute, which `[fan]` and `[fan_generic]` do not. Update — see [Disabling fans](#disabling-fans). |
| `disable_fans: no fan named 'x'` | Typo, or the fan is a named section. Both `hotend_fan` and `heater_fan hotend_fan` are accepted; the message lists the fans that exist. |
| `Move out of range` right after a probe | The trigger happened with no headroom for the retract. Set `min_probe_travel` above 0 to get the real error, and probe from a lower Z. |
| `Failed to set ADXL345 register [0x..] to 0x..: got 0x..` | SPI communication problem — wiring, bus contention, or too high an SPI speed. Klipper's `adxl345.set_reg()` verifies every write, so this is a genuine bus fault, not a tuning issue. |

## License

This work is licensed under a
[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License][cc-by-nc-sa].

[![CC BY-NC-SA 4.0][cc-by-nc-sa-image]][cc-by-nc-sa]

[cc-by-nc-sa]: http://creativecommons.org/licenses/by-nc-sa/4.0/
[cc-by-nc-sa-image]: https://licensebuttons.net/l/by-nc-sa/4.0/88x31.png
[cc-by-nc-sa-shield]: https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg?style=for-the-badge
