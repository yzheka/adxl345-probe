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
| `disable_fans` | empty | Comma-separated list of fans to switch off for the duration of a probe session and restore afterwards. Fan vibration is a common source of false taps. See [Disabling fans](#disabling-fans). |
| `activate_gcode` | empty | G-code template run before each probing move. |
| `deactivate_gcode` | empty | G-code template run after each probing move. |

## Commands

| Command | Description |
| ------- | ----------- |
| `SET_ACCEL_PROBE [TAP_THRESH=<mm/s²>] [TAP_DUR=<s>] [ACCEL=<mm/s²>]` | Adjust tap threshold, tap duration and probing acceleration at runtime and echo the resulting values. Not saved — put the final numbers in the config. |
| `TEST_TAP_TUNE [X=] [Y=] [Z=] [TEST_TAP_DEVIATION=] [SPEED_START=] [SPEED_END=] [SPEED_STEP=] [THRESHOLD_START=] [THRESHOLD_END=] [THRESHOLD_STEP=] [TRIALS=] [SAMPLES=] [MARGIN=] [WINDOW=] [SAVE=<0\|1>]` | Home if needed, move to the middle of the bed, and find the best `speed` and `tap_thresh` pair. See [Tuning speed and tap_thresh automatically](#tuning-speed-and-tap_thresh-automatically). |
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

Once `probe_accel` is settled, `TEST_TAP_TUNE` finds `speed` and `tap_thresh`
for you — see
[Tuning speed and tap_thresh automatically](#tuning-speed-and-tap_thresh-automatically).

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

## Tuning speed and tap_thresh automatically

`speed` and `tap_thresh` are not independent. A faster probe hits the bed
harder, so it takes a higher threshold before the move's own acceleration stops
tripping the tap — and a threshold that works at 10 mm/s may miss the bed
entirely at 25. `TEST_TAP_TUNE` sweeps the speeds you give it, finds the working
`tap_thresh` band at each one, and scores the pairs by how repeatable the
resulting probe actually is.

```
TEST_TAP_TUNE
```

That is the whole thing. It homes if any axis is unhomed, lifts clear, travels
to the middle of the bed and probes from `Z=10` — no `G28`/`G1` preamble. Pass
`X=`, `Y=` or `Z=` to override any of that.

It refuses to start while a print is running or paused — see
[Running during a print](#running-during-a-print).

Read this before running it:

- **This takes a while.** The defaults are eleven speeds, roughly 120–450
  probes, 20–60 minutes. Most of them are misfires that never touch the bed —
  see [Sparing the bed](#sparing-the-bed). The command prints its own estimate
  before starting.
  Narrow `SPEED_START`/`SPEED_END` or raise `SPEED_STEP` to cut it down — a
  first pass with `SPEED_STEP=4` halves it and still shows you which end of the
  range is worth looking at.
- **Stand over the machine anyway.** A probe at a threshold that is too high
  does not stop at the bed; the nozzle drives down to `position_min` /
  `minimum_z_position`. The search is built to avoid that — it starts at the
  sensitive end, where a wrong threshold misfires without the nozzle ever
  reaching the bed, and it never hunts for the top of the band — but a machine
  whose band is narrower than `MARGIN` will still produce one. Keep a hand on
  the emergency stop for the first run.
- Tune `probe_accel` and `rest_time` **before** running it. The result is only
  valid for the settings in force during the search.
- Run it over the part of the bed you actually probe. The middle is the usual
  answer, which is why it is the default, but a bed that flexes differently at
  the edges can want a different threshold there.
- **Homing uses the current `tap_thresh`.** If the probe is so badly tuned that
  `G28` itself fails, the command cannot get started. Raise the threshold by
  hand first with `SET_ACCEL_PROBE TAP_THRESH=...`, or home once with a Z
  endstop, then run this.
- The bed gets tapped several hundred times. Use a spot you don't mind marking.
- The top of the default range, 30 mm/s, is fast for a nozzle tap. If the high
  speeds come back unusable, that is the answer for your machine, not a fault —
  the run skips them and carries on. A machine that wants a gentler probe can
  sweep below the default floor with `SPEED_START=2`.

### Parameters

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `X` | middle of the bed | Where to probe. The default is the midpoint of the travel the kinematics report, less `x_offset`. |
| `Y` | middle of the bed | As `X`, less `y_offset`. |
| `Z` | `10` | Height each probe starts its descent from. Must clear anything on the bed. The move to the probing point also happens at this height — on a delta the reachable radius near the top of the travel is nil, so traversing at the height `G28` finishes at is out of range. |
| `TEST_TAP_DEVIATION` | `0` | Half-width in mm of the square around `X`/`Y` the taps are scattered over. `0` taps the same spot every time. Anything above `0` picks a random point in `X±dev`, `Y±dev` before every probe, so a few hundred taps do not all land in one place. |
| `TRAVEL_SPEED` | `50` | mm/s for the move to the probing point. Not the probing speed — that is what the command is measuring. |
| `SPEED_START` | `10` | First probing speed in mm/s. |
| `SPEED_END` | `30` | Last probing speed in mm/s. |
| `SPEED_STEP` | `2` | Increment. The defaults test 10, 12, 14, … 30 mm/s — eleven speeds. Capped at 20 speeds per run. |
| `THRESHOLD_START` | `10000` | Bottom of the `tap_thresh` search range in mm/s². The misspelling `THRESSHOLD_START` this command shipped with is still accepted. |
| `THRESHOLD_END` | `100000` | Top of the `tap_thresh` search range in mm/s². The misspelling `THRESSHOLD_END` is still accepted. |
| `THRESHOLD_STEP` | `1` | How much the walk raises `tap_thresh` after a misfire, in register steps (612.9 mm/s² each, like `MARGIN` and `WINDOW`). `1` tries every register and cannot miss a band. A bigger step gets to the band in fewer probes; if it lands past a narrow band, the registers it skipped are re-walked one at a time. |
| `TRIALS` | `1` | Probes at each candidate on the way up. `1` moves on at the first success, which is the point of walking upwards. Raise it only if your misfires are intermittent enough that one success can be luck — every extra probe here is another bed contact. |
| `SAMPLES` | `10` | Probes used to score each speed's repeatability, once its threshold is chosen. This is the number the ranking is built on — don't set it below about 5. |
| `MARGIN` | `2` | Register steps (612.9 mm/s² each) added to the edge for headroom, so the recommendation does not sit exactly where misfiring begins. Not probed beforehand — the accuracy run happens at edge + `MARGIN` and drops the margin if that value misses the bed. `MARGIN=0` uses the bare edge. |
| `WINDOW` | `16` | Register steps below the previous speed's edge where the next speed starts walking, instead of at `THRESHOLD_START`. This is what keeps the run from re-walking the whole range at every speed. `WINDOW=255` effectively disables the shortcut. |
| `SAVE` | `1` | `1` writes `speed` and `tap_thresh` to the config and runs `SAVE_CONFIG`, which **restarts Klipper**. `0` applies them for this session and writes nothing — the lines to paste are printed either way. |
| `CHIP` | — | Accelerometer name, matching `chip:`. Optional; only useful if you have named your `[adxl345]` section. |

### How it works

For each speed in turn:

**1. Walk up to the band.** The chip stores the threshold in an 8-bit register
at 612.9 mm/s² per step, so the search runs over register steps rather than raw
mm/s² — the default range is 147 of them. Starting at `THRESHOLD_START`, it
probes, and while the probe misfires it raises `tap_thresh` by `THRESHOLD_STEP`
registers and probes again. Each attempt ends one of three ways:

| Result | Meaning | What the nozzle does |
| ------ | ------- | -------------------- |
| `sensitive` | Misfired: the tap latched while arming, or fired on the start-of-move acceleration. Threshold too low. | Stops in the first fraction of a millimetre — **never reaches the bed** |
| `pass` | Descended past `min_probe_travel` and triggered. | A normal tap |
| `deaf` | Ran the whole move without triggering. Threshold too high. | **Drives into the bed** and keeps pushing to the descent floor |

The walk stops at the **first** `pass` — the bottom of the working band — and
nothing above it is probed to see how much further the band goes. Finding the
top of a band means probing until the bed is missed, and every `deaf` attempt
drives the nozzle into the bed for the whole descent, which is the one thing
worth avoiding. Everything below the edge cost one misfire each and never
touched the bed.

**2. Measure the accuracy there.** `MARGIN` register steps are added to the edge
for headroom, and `SAMPLES` probes run at that value. The Z spread they produce
is the same measurement `PROBE_ACCURACY` reports, and it is what the speeds are
ranked by — so the accuracy is always measured at the exact `tap_thresh` being
recommended.

The margin is added without being probed first. If it lands past the top of the
band, the first probe of the accuracy run misses the bed, and the run drops the
margin and re-measures at the bare edge. That costs one hard contact, and it
only happens on a band narrower than `MARGIN` — `MARGIN=0` avoids it entirely.

**3. Carry the floor.** The next speed starts its walk `WINDOW` registers below
this speed's edge rather than at the bottom of the range — the band moves with
speed, but not usually far, and the walk is where nearly all the probes go. If
that floor turns out to be above the band, or the edge is found at the floor
itself, the walk restarts from `THRESHOLD_START`, so the shortcut costs
accuracy nowhere.

A speed with no usable band at all is reported and skipped, not treated as
fatal. The run only fails if *no* speed works.

Finally the pairs are ranked by Z spread, ties broken by the lower sigma, since
the spread is only the two extreme samples. The winner's `speed` and
`tap_thresh` are applied, and saved if `SAVE=1`. The value written for
`tap_thresh` is the smallest whole mm/s² that maps back onto the winning
register, so what is written reproduces exactly what was tested.

Only `pass`, `sensitive` and `deaf` are verdicts. Anything else — an SPI
readback mismatch, an MCU homing timeout, a printer shutdown, a move out of
range — aborts the run and reports itself, rather than being recorded as "this
threshold misfires" and sending you off tuning the wrong thing.

The walk assumes misfiring is monotonic in the threshold, which tap detection
only roughly is. Confirm the winner with `PROBE_ACCURACY` before trusting it.

### Sparing the bed

Two things protect the bed, and they are independent.

**The search direction.** Walking up from the sensitive end means a wrong
threshold misfires in the first fraction of a millimetre instead of pressing the
nozzle into the bed. Stopping at the first success means never probing past the
band to find out where it ends. On a simulated eleven-speed run whose band
drifts with speed:

| | Total probes | Misfires (no contact) | Taps | Drove into the bed |
| --- | --- | --- | --- | --- |
| Bisecting from `THRESHOLD_END` down | 385 | 26 | 341 | 18 |
| Walking up, probing the whole band | 478 | 269 | 209 | 0 |
| Walking up, stopping at the first success | 390 | 269 | **121** | **0** |

121 bed contacts instead of 341, and nothing driven into the bed. The misfires
cost a second each and never make contact, so a longer walk is nearly free.

**The spot.** The taps that do land still land in the same place by default, and
on smooth PEI or glass that eventually shows. `TEST_TAP_DEVIATION` spreads them
out: with `TEST_TAP_DEVIATION=5` each probe first moves to a random point within
5 mm of `X`/`Y` in both axes, at `TRAVEL_SPEED`, then descends.

```
TEST_TAP_TUNE TEST_TAP_DEVIATION=5
```

The area is clipped to the travel the kinematics report, so a point near an
edge still works — the square is just cut short, and the run says so. On a
delta, where the reported range is the square bounding a round bed, the taps
are also kept inside the circle.

The cost is that the scoring in step 2 no longer measures one spot. Bed tilt
and unevenness across the area go straight into the Z spread the speeds are
ranked by, so the ranking gets noisier as the area grows. Keep the deviation
small enough that the bed is flat within it — a few millimetres is plenty to
spread the wear, and 5 mm of a typical bed is flat to well under the spread
being measured.

### Running during a print

`TEST_TAP_TUNE` homes the toolhead, drives to the middle of the bed and taps it
a few hundred times. Doing that partway through a print would destroy it, so
the command checks first and refuses:

```
!! TEST_TAP_TUNE: not starting - a print job is printing. This command homes
the toolhead, drives to the middle of the bed and taps it a few hundred times,
which would wreck the print. Run it when the printer is idle. Nothing has been
changed.
```

It refuses by **returning**, not by raising an error. An error raised inside an
SD print makes Klipper break out of the print loop and stop the job — which is
the outcome the check exists to prevent. So a stray `TEST_TAP_TUNE` in a macro
or a queued console command warns and does nothing; the print carries on.

A job is considered active when `[print_stats]` reports `printing` or `paused`,
or when `[virtual_sdcard]` is streaming a file. `paused` counts: the part is
still on the bed. `complete`, `cancelled` and `standby` do not.

One limitation worth knowing: a job streamed line by line over the serial port
by a host that drives neither of those objects cannot be detected from inside
Klipper, and the check will not catch it.

### Reading the output

```
  speed  10.0  tap_thresh   9807 (reg  16): sensitive Probe triggered prior to movement
  speed  10.0  tap_thresh  10420 (reg  17): sensitive Probe triggered prior to movement
  ... one line per register step ...
  speed  10.0  tap_thresh  16549 (reg  27): sensitive Probe triggered prior to movement
  speed  10.0  tap_thresh  17162 (reg  28): pass      z min 0.0193 max 0.0193 range 0.0000
  speed  10.0  tap_thresh  18388 (reg  30): works from reg 28, 10 samples, range 0.0040 sigma 0.0015
TEST_TAP_TUNE: results, best first
  1. speed  10.0  tap_thresh  18388  range 0.0040  sigma 0.0015  works from reg 28
  2. speed  12.0  tap_thresh  22065  range 0.0120  sigma 0.0041  works from reg 34
```

The `sensitive` lines are the walk climbing towards the band — each one is a
probe that misfired without touching the bed. The single `pass` after them is
the first success, and the walk stops there: `works from reg 28`. The line after
it is the accuracy run, 10 samples at reg 30 (28 plus the 2-step `MARGIN`),
which is the pair being recommended.

`range` is what the speeds are ranked by, `sigma` breaks ties. One tap ends the
walk and ten measure the result, so a speed costs eleven bed contacts however
far the walk had to climb.

### Failure messages

| Message | Meaning |
| ------- | ------- |
| `no speed produced a usable tap_thresh` | No speed had a working band. `probe_accel` is too high, or something is vibrating — a fan (see `disable_fans`), or a stepper. Check the pin with `QUERY_PROBE` first. |
| `speed N: unusable - tap_thresh M (the top of the range) still misfires` | At this speed even the least sensitive setting misfires. Skipped. |
| `speed N: unusable - nothing in ... detected the bed` | At this speed nothing in the range felt the contact. Try a lower `THRESHOLD_START` or a higher speed. |
| `speed N: unusable - M already misfires and P, the next value tried, misses the bed` | The misfire threshold and the deaf threshold meet with no gap at this speed. Lower `probe_accel` to open one up. |
| `tap_thresh M: ... - dropping the N step margin` | Edge + `MARGIN` missed the bed, so the band is narrower than the margin. The accuracy run repeats at the bare edge. |
| `failed the accuracy run` | The threshold that passed on the way up failed the longer `SAMPLES` run. Results at that speed are marginal; raise `TRIALS` to confirm the pass, or `MARGIN` to sit further from the misfire edge. |

An aborted run restores the previous `tap_thresh`, lifts back to the height it
started from, and writes nothing to the config.

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
| `ADXL345 probe triggered after only N mm of travel` | False trigger on the start-of-move acceleration. Lower `probe_accel`, then raise `tap_thresh` — or let `TEST_TAP_TUNE` find it. |
| `TEST_TAP_TUNE: G28 did not home X, Y and Z` | The command homed for you, but an axis is still unhomed afterwards. Usually a failed Z home — fix that first. |
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
