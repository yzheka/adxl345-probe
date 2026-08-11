# ADXL345 tap-detection Z probe
#
# Ported from https://github.com/jniebuhr/adxl345-probe for Klipper versions
# after the probe.py refactor that removed ProbeSessionHelper.
#
# Additions over upstream:
#   min_probe_travel  - reject a trigger that happens before the effector has
#                       actually descended, with a message that says why
#   probe_accel       - toolhead acceleration limit applied to the probing
#                       move only, restored afterwards
#   rest_time         - settle dwell before arming / after disarming tap
#                       detection (upstream hardcodes 0.1s each way)
#   ADXL_PROBE_CALIBRATE
#                     - walks tap_thresh up to the lowest value that taps at
#                       each probing speed, measures probe accuracy there, and
#                       keeps the most accurate pair. Only the accuracy runs
#                       touch the bed: a threshold too low to work misfires
#                       before the effector has descended, and the taps are
#                       scattered over an area so a run does not dent one spot
#   disable_fans      - works with every fan section type, not only the ones
#                       that happen to expose a fan_speed attribute
#
# The accelerometer is also powered up once per probe session rather than
# once per sample, which removes two blocking SPI round-trips per sample.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import math
import random
from . import probe, adxl345

REG_THRESH_TAP = 0x1D
REG_DUR = 0x21
REG_INT_MAP = 0x2F
REG_TAP_AXES = 0x2A
REG_INT_ENABLE = 0x2E
REG_INT_SOURCE = 0x30

DUR_SCALE = 0.000625  # 0.625 msec / LSB
TAP_SCALE = 0.0625 * adxl345.FREEFALL_ACCEL  # 62.5mg/LSB * g in mm/s**2

ADXL345_REST_TIME = .1

# THRESH_TAP is an 8 bit register, but the module has always clamped the
# configured value to 100000 mm/s**2 (register 163) - keep one constant for it
TAP_THRESH_MAX = 100000.

# THRESH_TAP is compared against total acceleration, gravity included - the
# ADXL345's tap detector is not AC coupled. A tap is only latched when the
# acceleration rises above the threshold and falls back below it inside the DUR
# window, so a threshold at or below 1g is permanently exceeded by the effector
# just sitting there and no tap can ever be registered: the probe reads as
# "no trigger", not as "too sensitive". 1g lands exactly on register 16
# (0.0625g per step), making 17 the lowest usable setting.
TAP_GRAVITY_CODE = int(adxl345.FREEFALL_ACCEL / TAP_SCALE)
TAP_FLOOR_CODE = TAP_GRAVITY_CODE + 1

# ADXL_PROBE_CALIBRATE defaults
# Both of these are the register grid rather than round decimals: 10420 mm/s**2
# is register 17, the lowest that can latch a tap at all, and a step of 613
# advances exactly one register. A round 1000 would advance one or two
# registers unevenly, skipping 57 of the 147 in the range - enough to step over
# a narrow band, and enough to land above the bottom of a wide one, which is a
# harder tap than the machine needs. Nothing finer than 613 is worth asking
# for: values landing on a register already tried are dropped, so a smaller
# step probes exactly the same settings.
CAL_THRESHOLD_START = math.ceil(TAP_FLOOR_CODE * TAP_SCALE)
CAL_THRESHOLD_END = 100000.
CAL_THRESHOLD_STEP = math.ceil(TAP_SCALE)  # one register per rung
CAL_SPEED_START = 10.
CAL_SPEED_END = 30.
CAL_SPEED_STEP = 2.
CAL_MAX_SPEEDS = 20
CAL_SAMPLES = 10  # taps per accuracy measurement
# An average trigger height further than this from nominal zero is not a
# measurement of the bed, so the threshold that produced it is treated as
# unusable and the walk carries on up.
CAL_ACCURACY_MAX = 0.1
# Probe faults in a row before a run gives up. A fault fails its own step and
# no more, but a machine that faults on every tap is not going to recover by
# being asked another hundred times.
CAL_MAX_FAULTS = 3
# The nozzle rises this far between those taps, and the descent that follows
# has to be longer than min_probe_travel or the trigger counts as a misfire.
# Twice that is the default, and this is the floor when it is 0.
CAL_LIFT_FLOOR = 1.
CAL_Z = 10.  # height the first tap of each threshold descends from
CAL_TRAVEL_SPEED = 50.  # mm/s for the move to the probing point
CAL_DEVIATION = 20.  # mm of X/Y scatter around the probing point, 0 = off
CAL_SCATTER_TRIES = 10  # draws before giving up on landing on a round bed

# Probe failures that are a tuning result rather than a fault. Everything else
# - a latch that would not clear, an SPI readback mismatch, an MCU homing
# timeout - fails its own step and is reported as itself rather than being
# recorded as "this threshold misfires", which would send the user off tuning a
# threshold that was never the problem. A shutdown still stops the run.
PROBE_DEAF_ERRORS = ('No trigger on',)
PROBE_SENSITIVE_ERRORS = ('triggered after only',
                          'tap triggered before move',
                          'tap triggered after move',
                          'Probe triggered prior to movement')


# Equivalent of probe.ProbeEndstopWrapper, but arms ADXL345 tap detection
# around each probing move instead of deploying/stowing a physical probe.
class ADXL345EndstopWrapper:
    def __init__(self, config, probe_offsets, param_helper):
        self.printer = config.get_printer()
        self.config_name = config.get_name()
        self.param_helper = param_helper
        self.probe_offsets = probe_offsets
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.activate_gcode = gcode_macro.load_template(
            config, 'activate_gcode', '')
        self.deactivate_gcode = gcode_macro.load_template(
            config, 'deactivate_gcode', '')
        # int_pin selects which ADXL345 interrupt output carries the tap
        int_pin = config.get('int_pin').strip()
        self.inverted = False
        if int_pin.startswith('!'):
            self.inverted = True
            int_pin = int_pin[1:].strip()
        if int_pin not in ('int1', 'int2'):
            raise config.error('int_pin must specify one of int1 or int2 pins')
        self.int_map = 0x40 if int_pin == 'int2' else 0x00
        # Tap detection parameters
        self.tap_thresh = config.getfloat('tap_thresh', 5000,
                                          minval=TAP_SCALE,
                                          maxval=TAP_THRESH_MAX)
        self.tap_dur = config.getfloat('tap_dur', 0.01,
                                       above=DUR_SCALE, maxval=0.1)
        self.min_probe_travel = config.getfloat('min_probe_travel', 0.5,
                                                minval=0.)
        # Acceleration limit applied to the probing move only. The start of
        # the move is what usually trips tap detection, so this is the main
        # knob for false triggers.
        self.probe_accel = config.getfloat('probe_accel', None, above=0.)
        self.saved_accel = None
        # Settle time before arming and after disarming tap detection. Too
        # short and residual ringing from the retract move trips the tap.
        self.rest_time = config.getfloat('rest_time', ADXL345_REST_TIME,
                                         minval=0., maxval=1.)
        self.disable_fans = [f.strip()
                             for f in config.get('disable_fans', '').split(',')
                             if f.strip()]
        # Resolved to (name, object, saved-state) on connect, once every
        # printer object exists
        self.fan_objects = []
        adxl345_name = config.get('chip', 'adxl345')
        self.adxl345 = self.printer.lookup_object(adxl345_name)
        # Short name, as ACCELEROMETER_MEASURE etc. expect it in CHIP=
        self.chip_name = adxl345_name.split()[-1]
        # Create an "endstop" object to handle the interrupt pin
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.lookup_pin(config.get('probe_pin'),
                                      can_invert=True, can_pullup=True)
        mcu = pin_params['chip']
        self.mcu_endstop = mcu.setup_pin('endstop', pin_params)
        self.query_endstop = self.mcu_endstop.query_endstop
        # Probing via homing to the endstop (also binds the Z steppers)
        self.homing_helper = probe.DescendToEndstopHelper(
            config, self.mcu_endstop, probe_offsets, param_helper)
        # Session state. managed_session marks a session this module opened
        # itself and will close in its own finally - the command-error hook
        # must not tear that one down early.
        self.is_measuring = False
        self.in_session = False
        self.managed_session = False
        # Probing area ADXL_PROBE_CALIBRATE works in, set up when it starts
        self.cal_point = None
        self.printer.register_event_handler('klippy:connect', self._init_adxl)
        self.printer.register_event_handler('gcode:command_error',
                                            self._handle_command_error)
        gcode = self.printer.lookup_object('gcode')
        # Each command is registered both under the chip name and as the
        # default (None). Registering only None makes CHIP=<name> fail with
        # an empty option list, because gcode._cmd_mux looks the supplied
        # value up in the registered values.
        commands = [('SET_ACCEL_PROBE', self.cmd_SET_ACCEL_PROBE,
                     self.cmd_SET_ACCEL_PROBE_help),
                    ('ADXL_PROBE_CALIBRATE', self.cmd_ADXL_PROBE_CALIBRATE,
                     self.cmd_ADXL_PROBE_CALIBRATE_help)]
        for name, func, desc in commands:
            for key in (self.chip_name, None):
                gcode.register_mux_command(name, 'CHIP', key, func, desc=desc)

    # --- ADXL345 register handling ---------------------------------------
    # THRESH_TAP truncates, but so does float division: 62 * TAP_SCALE fed
    # back through int(x / TAP_SCALE) can land on 61. The epsilon keeps exact
    # register multiples on their own step without changing truncation of
    # in-between values such as 12000 -> 19.
    def _tap_code(self, thresh):
        return max(0, min(255, int(thresh / TAP_SCALE + 1e-6)))

    # Smallest whole mm/s**2 that maps back onto `code`. Reporting and saving
    # this instead of code * TAP_SCALE means the number written to the config
    # reproduces the register that was actually tested.
    def _code_thresh(self, code):
        return float(math.ceil(code * TAP_SCALE))

    def _write_tap_regs(self):
        chip = self.adxl345
        chip.set_reg(REG_THRESH_TAP, self._tap_code(self.tap_thresh))
        chip.set_reg(REG_DUR, int(self.tap_dur / DUR_SCALE))

    def _init_adxl(self):
        self._resolve_fans()
        chip = self.adxl345
        chip.set_reg(adxl345.REG_POWER_CTL, 0x00)
        chip.set_reg(adxl345.REG_DATA_FORMAT, 0x2B if self.inverted else 0x0B)
        chip.set_reg(REG_INT_MAP, self.int_map)
        chip.set_reg(REG_TAP_AXES, 0x07)
        self._write_tap_regs()

    def _try_clear_tap(self):
        chip = self.adxl345
        for _ in range(8):
            if not (chip.read_reg(REG_INT_SOURCE) & 0x40):
                return True
        return False

    # --- Fan control ------------------------------------------------------
    # Every fan section wraps a fan.Fan in an object attribute named `fan`,
    # but only some of them (heater_fan, controller_fan, temperature_fan)
    # carry the speed attributes upstream used to poke. Resolve the sections
    # once at connect so a bad name is a startup error, not a probe-time
    # AttributeError.
    def _resolve_fans(self):
        self.fan_objects = []
        if not self.disable_fans:
            return
        candidates = [(n, o) for n, o in self.printer.lookup_objects()
                      if hasattr(o, 'fan') and hasattr(o, 'get_status')]
        for name in self.disable_fans:
            obj = self.printer.lookup_object(name, None)
            if obj is None:
                # Accept the bare section name too, so `hotend_fan` finds
                # [heater_fan hotend_fan]
                matches = [(n, o) for n, o in candidates
                           if n.split()[-1] == name]
                if len(matches) > 1:
                    raise self.printer.config_error(
                        "disable_fans: '%s' is ambiguous (%s). Use the full"
                        " object name."
                        % (name, ", ".join(n for n, _ in matches)))
                if not matches:
                    raise self.printer.config_error(
                        "disable_fans: no fan named '%s'. Known fans: %s"
                        % (name, ", ".join(n for n, _ in candidates)
                           or "none"))
                name, obj = matches[0]
            if not hasattr(obj, 'fan'):
                raise self.printer.config_error(
                    "disable_fans: '%s' is not a fan" % (name,))
            self.fan_objects.append({'name': name, 'obj': obj,
                                     'driver': obj.fan, 'saved': None})

    # The speed the fan has last been asked for, in the units set_speed()
    # takes. Not get_status()['speed']: fan.Fan reports last_req_value, which
    # _apply_speed has already multiplied by max_power, so feeding that back
    # in would scale it a second time (a max_power: 0.6 fan restored to 0.36),
    # once per probe session. A request that is queued but not yet applied has
    # not reached last_req_value at all, and is the one that matters.
    def _fan_requested_speed(self, driver):
        rqueue = getattr(getattr(driver, 'gcrq', None), 'rqueue', None)
        if rqueue:
            return rqueue[-1][1]
        value = getattr(driver, 'last_req_value', None)
        if value is None:
            return None
        max_power = getattr(driver, 'max_power', 1.) or 1.
        return value / max_power

    # Attributes that make a fan drive itself from a periodic callback. Zero
    # them or the callback puts the fan straight back on mid-probe.
    FAN_DRIVE_ATTRS = ('fan_speed', 'idle_speed', 'min_speed', 'max_speed')

    def _control_fans(self, disable):
        if not self.fan_objects:
            return
        if disable:
            # Flush the lookahead so a speed change issued just before the
            # probe has reached the fan's request queue and can be read back
            try:
                self.printer.lookup_object('toolhead').get_last_move_time()
            except Exception:
                logging.exception("adxl345_probe: cannot flush before fan off")
        for entry in self.fan_objects:
            obj, driver = entry['obj'], entry['driver']
            if disable:
                if entry['saved'] is not None:
                    continue
                saved = {'#speed': self._fan_requested_speed(driver),
                         '#max_power': getattr(driver, 'max_power', None)}
                for attr in self.FAN_DRIVE_ATTRS:
                    if hasattr(obj, attr):
                        saved[attr] = getattr(obj, attr)
                        setattr(obj, attr, 0.)
                # Gate the output as well. Zeroing the section attributes
                # covers heater_fan/controller_fan/temperature_fan, but a
                # [fan_generic] driven by SET_FAN_SPEED TEMPLATE re-requests
                # its speed every 0.5s through fan.Fan directly, and nothing
                # at the section level stops it. max_power is the one point
                # every request passes through.
                if saved['#max_power'] is not None:
                    driver.max_power = 0.
                entry['saved'] = saved
                self._set_fan_speed(driver, 0.)
            else:
                saved = entry['saved']
                if saved is None:
                    continue
                entry['saved'] = None
                speed = saved.pop('#speed', None)
                max_power = saved.pop('#max_power', None)
                if max_power is not None:
                    driver.max_power = max_power
                for attr, value in saved.items():
                    setattr(obj, attr, value)
                if speed:
                    self._set_fan_speed(driver, speed)

    def _set_fan_speed(self, driver, value):
        setter = getattr(driver, 'set_speed_from_command', None)
        if setter is not None:
            setter(value)
            return
        # Fall back to the low-level setter, by keyword: the positional order
        # of set_speed() changed between Klipper versions, and getting it
        # backwards would drive the fan to full power
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(
            lambda pt: driver.set_speed(value=value, print_time=pt))

    # --- Probing acceleration --------------------------------------------
    def _apply_accel(self, accel):
        toolhead = self.printer.lookup_object('toolhead')
        setter = getattr(toolhead, 'set_max_velocities', None)
        if setter is not None:
            setter(None, accel, None, None)
            return
        # Fallback for Klipper versions without the direct setter
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script_from_command("SET_VELOCITY_LIMIT ACCEL=%.6f"
                                      % (accel,))

    def _set_probe_accel(self):
        if self.probe_accel is None or self.saved_accel is not None:
            return
        toolhead = self.printer.lookup_object('toolhead')
        systime = self.printer.get_reactor().monotonic()
        cur_accel = toolhead.get_status(systime)['max_accel']
        if cur_accel <= self.probe_accel:
            return
        self.saved_accel = cur_accel
        self._apply_accel(self.probe_accel)

    def _restore_accel(self):
        if self.saved_accel is None:
            return
        accel = self.saved_accel
        self.saved_accel = None
        self._apply_accel(accel)

    def _arm_tap(self):
        chip = self.adxl345
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        if self.rest_time:
            toolhead.dwell(self.rest_time)
        print_time = toolhead.get_last_move_time()
        clock = chip.mcu.print_time_to_clock(print_time)
        chip.set_reg(REG_INT_ENABLE, 0x00, minclock=clock)
        chip.read_reg(REG_INT_SOURCE)
        chip.set_reg(REG_INT_ENABLE, 0x40, minclock=clock)
        if not self._try_clear_tap():
            raise self.printer.command_error(
                "ADXL345 tap triggered before move,"
                " it may be set too sensitive.")
        # The tap register is clean - if the endstop still reads triggered the
        # problem is the pin itself, not the accelerometer
        if self.query_endstop(toolhead.get_last_move_time()):
            raise self.printer.command_error(
                "ADXL345 probe pin reads TRIGGERED while the tap register is"
                " clear. Check probe_pin polarity - remove any '^' pullup, or"
                " add '!' if the interrupt idles high.")

    def _disarm_tap(self, check=True):
        chip = self.adxl345
        toolhead = self.printer.lookup_object('toolhead')
        if self.rest_time:
            toolhead.dwell(self.rest_time)
        print_time = toolhead.get_last_move_time()
        clock = chip.mcu.print_time_to_clock(print_time)
        chip.set_reg(REG_INT_ENABLE, 0x00, minclock=clock)
        self.deactivate_gcode.run_gcode_from_command()
        if check and not self._try_clear_tap():
            raise self.printer.command_error(
                "ADXL345 tap triggered after move,"
                " it may be set too sensitive.")

    # Teardown after a failed probe. The tap register is expected to be
    # latched here - that is usually why we are unwinding - so do not let it
    # raise a second error over the first one.
    def _abort_tap(self):
        try:
            self._disarm_tap(check=False)
        except Exception:
            logging.exception("adxl345_probe: error disarming tap")

    # --- Hardware probe session interface --------------------------------
    def start_probe_session(self, gcmd):
        self.homing_helper.clear_trigger_positions()
        self._control_fans(True)
        try:
            # Power the chip up once for the whole session instead of once
            # per sample - each set_reg is a blocking host<->MCU round-trip
            chip = self.adxl345
            self.is_measuring = (chip.read_reg(adxl345.REG_POWER_CTL) == 0x08)
            if not self.is_measuring:
                chip.set_reg(adxl345.REG_POWER_CTL, 0x08)
        except Exception:
            # The probe helper never saw a session start, so its error
            # handler will not end one - put the fans back ourselves
            self._control_fans(False)
            raise
        self.in_session = True
        return self

    def run_probe(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        start_z = toolhead.get_position()[2]
        self._set_probe_accel()
        try:
            # activate_gcode lives out here so that every path which has run
            # it also runs deactivate_gcode. _arm_tap can raise (tap latched
            # while arming), and that is the normal outcome of a threshold
            # that is set too low - it must not leak an unbalanced activate.
            self.activate_gcode.run_gcode_from_command()
            try:
                self._arm_tap()
                self.homing_helper.descend_until_trigger(gcmd)
            except Exception:
                self._abort_tap()
                raise
            travel = start_z - toolhead.get_position()[2]
            self._disarm_tap()
        finally:
            self._restore_accel()
        if travel < self.min_probe_travel:
            raise self.printer.command_error(
                "ADXL345 probe triggered after only %.3fmm of travel"
                " (minimum %.3fmm). The tap threshold is probably firing on"
                " the acceleration at the start of the move - raise"
                " tap_thresh, lower the probing speed, or reduce the Z"
                " acceleration." % (travel, self.min_probe_travel))

    def pull_probed_results(self):
        return self.homing_helper.pull_trigger_positions()

    def end_probe_session(self):
        self.homing_helper.clear_trigger_positions()
        self._restore_accel()
        in_session, self.in_session = self.in_session, False
        # Unconditional, and a no-op if nothing is switched off: the fans go
        # off before the session is marked open, so a failure in between must
        # not strand them
        self._control_fans(False)
        if in_session and not self.is_measuring:
            self.adxl345.set_reg(adxl345.REG_POWER_CTL, 0x00)

    # Klipper's probe commands call end_probe_session only on the success
    # path - probe.run_single_probe and cmd_PROBE_ACCURACY have no
    # try/finally - and rely on this event to clean up after an aborted
    # probe. Tap probes abort often while tuning, so hook it here too rather
    # than trusting that a session was open far enough for the probe helper's
    # own handler to fire.
    def _handle_command_error(self):
        if self.managed_session:
            # ADXL_PROBE_CALIBRATE owns this session and ends it in its own
            # finally. A macro in activate_gcode can raise - and therefore
            # fire this event - without the sweep being over; powering the
            # accelerometer down underneath it would make every remaining
            # probe miss the bed.
            return
        try:
            self.end_probe_session()
        except Exception:
            logging.exception("adxl345_probe: error ending probe session")

    # --- Commands ---------------------------------------------------------
    cmd_SET_ACCEL_PROBE_help = "Configure ADXL345 parameters related to probing"

    def cmd_SET_ACCEL_PROBE(self, gcmd):
        # Parse everything before touching any state: a rejected TAP_DUR must
        # not leave self.tap_thresh describing a register that was never
        # written
        tap_thresh = gcmd.get_float('TAP_THRESH', self.tap_thresh,
                                    minval=TAP_SCALE, maxval=TAP_THRESH_MAX)
        tap_dur = gcmd.get_float('TAP_DUR', self.tap_dur,
                                 above=DUR_SCALE, maxval=0.1)
        probe_accel = gcmd.get_float('ACCEL', self.probe_accel, above=0.)
        self.tap_thresh = tap_thresh
        self.tap_dur = tap_dur
        self.probe_accel = probe_accel
        self._write_tap_regs()
        gcmd.respond_info("tap_thresh: %.0f  tap_dur: %.5f  probe_accel: %s"
                          % (self.tap_thresh, self.tap_dur,
                             "none" if self.probe_accel is None
                             else "%.0f" % (self.probe_accel,)))

    cmd_ADXL_PROBE_CALIBRATE_help = (
        "Measure probe accuracy across probing speeds and keep the best"
        " speed / tap_thresh pair")

    # Raised when no threshold in the range works at a given speed. Distinct
    # from command_error so the run can skip that speed while a real fault
    # still propagates.
    class NoThreshold(Exception):
        pass

    # Return the toolhead to a height it can traverse and descend from again.
    # Called after a failed tap too: a probe that missed the bed ran its move
    # to the end, which leaves the nozzle loaded against the bed at the
    # descent floor.
    def _lift_to(self, z, lift_speed):
        try:
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.manual_move([None, None, z], lift_speed)
        except Exception:
            logging.exception("adxl345_probe: cannot lift to %.3f", z)

    # Every tap is the nozzle hitting the bed. DEVIATION spreads them over a
    # square of that half-width around the probing point instead of driving
    # them all into one spot. Returns (None, None) when it is off, so the
    # toolhead is left where it is and the move is skipped entirely.
    def _probe_xy(self):
        point = self.cal_point
        if point is None or not point['dev']:
            return None, None
        for _ in range(CAL_SCATTER_TRIES):
            x = random.uniform(point['x_lo'], point['x_hi'])
            y = random.uniform(point['y_lo'], point['y_hi'])
            if (point['radius2'] is None
                    or x * x + y * y <= point['radius2']):
                return x, y
        # The corner of the square this point sits in is off a round bed. Tap
        # the point that was asked for rather than one that cannot be reached.
        return point['x'], point['y']

    # A shutdown is not something the next tap can recover from, so it is the
    # one fault that still stops a run dead. Older Klipper may not expose the
    # query; assume it is fine rather than aborting on the check itself.
    def _is_shutdown(self):
        try:
            return bool(self.printer.is_shutdown())
        except Exception:
            return False

    # One tap, from `from_z`. `scatter` picks a fresh random point within
    # DEVIATION first; without it the tap lands wherever the last one did,
    # which is what an accuracy measurement needs - moving between taps would
    # fold the shape of the bed into the numbers. Classified as:
    #   pass      - descended past min_probe_travel and triggered
    #   sensitive - misfired (tap latched while arming, or triggered on the
    #               start-of-move acceleration): tap_thresh is too low
    #   deaf      - the move ran to its end without triggering: too high
    # Returns (verdict, detail, trigger z or None).
    def _tap(self, probe_gcmd, from_z, lift_speed, safe_z, scatter=True):
        toolhead = self.printer.lookup_object('toolhead')
        # Lift before traversing: the last tap left the nozzle on the bed, and
        # moving in XY from there would drag it across the surface
        toolhead.manual_move([None, None, from_z], lift_speed)
        x, y = self._probe_xy() if scatter else (None, None)
        if x is not None:
            toolhead.manual_move([x, y, None], self.cal_point['speed'])
        self.homing_helper.clear_trigger_positions()
        try:
            self.run_probe(probe_gcmd)
        except self.printer.command_error as e:
            msg = str(e)
            if any(t in msg for t in PROBE_DEAF_ERRORS):
                # The move ran to the descent floor - get off the bed
                self._lift_to(safe_z, lift_speed)
                return 'deaf', msg, None
            if any(t in msg for t in PROBE_SENSITIVE_ERRORS):
                return 'sensitive', msg, None
            # Anything else is a fault rather than a verdict: a latch that
            # would not clear, an SPI readback mismatch, an MCU homing
            # timeout, a move out of range. It says nothing about tap_thresh,
            # so it is reported as itself and the step is abandoned - the
            # caller decides whether to carry on or give up.
            self._lift_to(safe_z, lift_speed)
            if self._is_shutdown():
                # Nothing will work again until the user clears it, so there
                # is no point walking the rest of the range
                raise
            return 'fault', msg, None
        try:
            positions = self.homing_helper.pull_trigger_positions()
        except Exception:
            positions = None
        z = positions[-1][2] if positions else None
        return 'pass', ("triggered at z %.4f" % (z,) if z is not None
                        else "triggered, no position reported"), z

    # The sequence of thresholds tried at each speed, in mm/s**2. Two things
    # are dropped: anything at or below 1g, which cannot latch a tap at all
    # (see TAP_FLOOR_CODE), and values landing on a THRESH_TAP register already
    # tried, since the register is 612.9 mm/s**2 per step and a finer step
    # would re-probe the same setting.
    def _thresholds(self, gcmd):
        lo = gcmd.get_float('THRESHOLD_START', CAL_THRESHOLD_START,
                            minval=TAP_SCALE, maxval=TAP_THRESH_MAX)
        hi = gcmd.get_float('THRESHOLD_END', CAL_THRESHOLD_END,
                            minval=TAP_SCALE, maxval=TAP_THRESH_MAX)
        step = gcmd.get_float('THRESHOLD_STEP', CAL_THRESHOLD_STEP, above=0.)
        if hi < lo:
            raise gcmd.error("THRESHOLD_END must not be below THRESHOLD_START")
        floor = self._code_thresh(TAP_FLOOR_CODE)
        if hi < floor:
            raise gcmd.error(
                "ADXL_PROBE_CALIBRATE: THRESHOLD_END=%.0f is at or below the"
                " 1g the effector carries (%.0f mm/s^2), where tap detection"
                " can never latch. Raise it above %.0f."
                % (hi, self._code_thresh(TAP_GRAVITY_CODE), floor))
        out, seen, thresh = [], set(), lo
        while thresh <= hi + 1e-9:
            code = self._tap_code(thresh)
            if code not in seen:
                seen.add(code)
                out.append(thresh)
            thresh += step
        return out

    # The probing speeds to measure, in mm/s
    def _speeds(self, gcmd):
        start = gcmd.get_float('SPEED_START', CAL_SPEED_START, above=0.)
        end = gcmd.get_float('SPEED_END', CAL_SPEED_END, above=0.)
        step = gcmd.get_float('SPEED_STEP', CAL_SPEED_STEP, above=0.)
        if end < start:
            raise gcmd.error("SPEED_END must not be below SPEED_START")
        out, speed = [], start
        while speed <= end + 1e-9:
            out.append(round(speed, 6))
            speed += step
            if len(out) > CAL_MAX_SPEEDS:
                raise gcmd.error(
                    "SPEED_START/SPEED_END/SPEED_STEP asks for more than %d"
                    " speeds. Raise SPEED_STEP or narrow the range."
                    % (CAL_MAX_SPEEDS,))
        return out

    # A fault fails the step it happened on and nothing more: a latch that
    # would not clear, or an SPI hiccup, is often gone by the next tap, and
    # losing a twenty minute run to one of them is worse than losing a rung.
    # `faults` carries the count across speeds so a machine that faults on
    # everything still stops rather than grinding through the whole range.
    def _note_fault(self, gcmd, speed, thresh, detail, faults):
        faults['run'] += 1
        faults['total'] += 1
        faults['last'] = detail
        gcmd.respond_info(
            "  speed %5.1f  tap_thresh %6.0f  probe fault, step failed: %s"
            % (speed, thresh, detail))
        if faults['run'] >= CAL_MAX_FAULTS:
            raise gcmd.error(
                "ADXL_PROBE_CALIBRATE: %d probe faults in a row, so this is"
                " not a passing glitch - stopping. The last one was: %s"
                % (faults['run'], detail))

    # Walk tap_thresh up until a tap works, then measure how repeatable that
    # tap is. A threshold too low misfires before the effector has descended -
    # one probe, no bed contact - so walking up from the sensitive end is what
    # keeps the bed intact. Returns the accuracy measurement for this speed.
    def _measure_speed(self, gcmd, probe_gcmd, speed, thresholds, samples,
                       lift, worst, faults, start_z, lift_speed):
        probed = []
        for thresh in thresholds:
            if self._tap_code(thresh) <= TAP_GRAVITY_CODE:
                # Below 1g nothing can latch, and it is arithmetic rather than
                # anything the machine has to demonstrate. Probing it would
                # cost a full-depth descent with the nozzle loaded against the
                # bed for the whole of it, to learn what the register value
                # already says.
                continue
            self.tap_thresh = thresh
            self._write_tap_regs()
            probed.append(thresh)
            verdict, detail, z = self._tap(probe_gcmd, start_z, lift_speed,
                                           start_z)
            # Nothing is logged for the climb itself. A misfire is the expected
            # outcome of a threshold that is still too low, and one line per
            # register step buries the two lines that matter in hundreds.
            if verdict == 'fault':
                self._note_fault(gcmd, speed, thresh, detail, faults)
                continue
            faults['run'] = 0
            if verdict == 'deaf':
                # Nothing higher can be more sensitive than this was. If even
                # the first one probed missed, the fault is upstream of
                # tap_thresh - say so rather than blaming the range.
                if len(probed) == 1:
                    raise self.NoThreshold(
                        "the most sensitive usable tap_thresh (%.0f) did not"
                        " feel the bed at all. That is not a threshold"
                        " problem: check the wiring with QUERY_PROBE, check"
                        " that tap_dur (%.4f s) is long enough for the"
                        " contact, and confirm a hand tap on the nozzle stops"
                        " a PROBE" % (thresh, self.tap_dur))
                raise self.NoThreshold(
                    "tap_thresh %.0f already misses the bed" % (thresh,))
            if verdict == 'sensitive':
                continue
            # It tapped, and that tap is #1 of the measurement: it landed on
            # the spot the rest will use, so it belongs in the average. The
            # remaining `samples` - 1 taps go to the same place, lifting `lift`
            # mm between them rather than returning to the start height. No
            # scatter from here on - moving between taps would measure the
            # shape of the bed as well as the probe.
            zs = [] if z is None else [z]

            def report(ordinal, _s=speed, _t=thresh):
                if not zs:
                    detail = "tapped, no position reported"
                elif len(zs) == 1:
                    detail = "tapped at z %.4f" % (zs[0],)
                else:
                    detail = "accuracy %.4f" % (abs(sum(zs) / len(zs)),)
                gcmd.respond_info(
                    "  tap %-4s speed %5.1f  tap_thresh %6.0f  %s"
                    % ("#%d:" % (ordinal,), _s, _t, detail))

            report(1)
            from_z = (z if z is not None
                      else self.printer.lookup_object(
                          'toolhead').get_position()[2]) + lift
            failed = None
            for n in range(2, samples + 1):
                verdict, detail, z = self._tap(probe_gcmd, from_z, lift_speed,
                                               start_z, scatter=False)
                if verdict != 'pass':
                    failed = (verdict, detail)
                    break
                faults['run'] = 0
                if z is None:
                    continue
                zs.append(z)
                from_z = z + lift
                report(n)
            if failed is None and len(zs) >= 2:
                mean = sum(zs) / len(zs)
                spread = max(zs) - min(zs)
                sigma = math.sqrt(sum((v - mean) ** 2 for v in zs)
                                  / len(zs))
                self._lift_to(start_z, lift_speed)
                if abs(mean) > worst:
                    # Triggering that far from the bed is not a measurement of
                    # anything: the tap is latching on something other than the
                    # contact, or the effector is deflecting that far
                    # before the chip sees it. Treat it like any other
                    # unusable threshold.
                    gcmd.respond_info(
                        "  speed %5.1f  tap_thresh %6.0f  accuracy %.4f is"
                        " worse than %.4f - raising tap_thresh"
                        % (speed, thresh, abs(mean), worst))
                    continue
                # The trigger height is signed - it is a toolhead position, and
                # contact below nominal zero is the normal case. What ranks the
                # pairs is its distance from zero, so keep both.
                return {'speed': speed, 'thresh': thresh, 'mean': mean,
                        'accuracy': abs(mean), 'spread': spread,
                        'sigma': sigma, 'samples': len(zs)}
            # The accuracy run broke down, so this threshold only works
            # intermittently. Carry on up.
            self._lift_to(start_z, lift_speed)
            if failed is not None and failed[0] == 'fault':
                self._note_fault(gcmd, speed, thresh, failed[1], faults)
                continue
            if failed is not None and failed[0] == 'deaf':
                raise self.NoThreshold(
                    "tap_thresh %.0f misses the bed part way through the"
                    " accuracy run" % (thresh,))
            gcmd.respond_info(
                "  speed %5.1f  tap_thresh %6.0f  only %d of %d taps worked -"
                " raising tap_thresh" % (speed, thresh, len(zs), samples))
        raise self.NoThreshold(
            "every tap_thresh from %.0f to %.0f mm/s^2 misfired"
            % (probed[0], probed[-1]))

    def cmd_ADXL_PROBE_CALIBRATE(self, gcmd):
        # Refuse before parsing anything, and refuse by returning rather than
        # raising: an error raised inside an SD print aborts the print
        # (virtual_sdcard breaks out of its work loop on gcode.error), which
        # is exactly what this guard exists to avoid.
        busy = self._active_print()
        if busy is not None:
            gcmd.respond_info(
                "!! ADXL_PROBE_CALIBRATE: not starting - %s. This command"
                " homes the toolhead, drives to the middle of the bed and taps"
                " it a few hundred times, which would wreck the print. Run it"
                " when the printer is idle. Nothing has been changed."
                % (busy,))
            return
        thresholds = self._thresholds(gcmd)
        speeds = self._speeds(gcmd)
        samples = gcmd.get_int('SAMPLES', CAL_SAMPLES, minval=2, maxval=100)
        worst = gcmd.get_float('ACCURACY_MAX', CAL_ACCURACY_MAX, above=0.)
        lift = gcmd.get_float('LIFT', max(2. * self.min_probe_travel,
                                          CAL_LIFT_FLOOR), above=0.)
        if lift <= self.min_probe_travel:
            # Every tap of the accuracy run would trigger inside
            # min_probe_travel and be read as a misfire, so the walk would
            # climb to the top of the range and report the machine unusable.
            raise gcmd.error(
                "ADXL_PROBE_CALIBRATE: LIFT=%.3f is not above"
                " min_probe_travel=%.3f, so every tap of the accuracy run"
                " would look like a misfire. Raise LIFT above %.3f, or leave"
                " it out and it defaults to twice min_probe_travel."
                % (lift, self.min_probe_travel, self.min_probe_travel))
        start_z = self._move_to_probe_point(gcmd)
        lift_speed = self.param_helper.get_probe_params(gcmd)['lift_speed']
        saved_thresh = self.tap_thresh
        saved_speed = getattr(self.param_helper, 'speed', None)
        gcode = self.printer.lookup_object('gcode')
        base_params = dict(gcmd.get_command_parameters())
        # One probe per run_probe call - the averaging helper is not in this
        # path, this module's own session is
        base_params['SAMPLES'] = '1'
        keep = False
        measured = []
        # 'run' is the current unbroken streak, reset by any tap that answers
        faults = {'run': 0, 'total': 0, 'last': None}

        dead = sum(1 for t in thresholds
                   if self._tap_code(t) <= TAP_GRAVITY_CODE)
        gcmd.respond_info(
            "ADXL_PROBE_CALIBRATE: speeds %s mm/s, tap_thresh %.0f - %.0f"
            " mm/s^2 in %d step(s)%s, %d taps per measurement. The climb is"
            " quiet; every measuring tap reports the average so far."
            % (", ".join("%g" % s for s in speeds), thresholds[0],
               thresholds[-1], len(thresholds),
               "" if not dead else " (the first %d are at or below 1g and are"
               " skipped)" % (dead,), samples))

        self.start_probe_session(gcmd)
        self.managed_session = True
        try:
            for speed in speeds:
                params = dict(base_params)
                params['PROBE_SPEED'] = "%.6f" % (speed,)
                probe_gcmd = gcode.create_gcode_command("", "", params)
                try:
                    measured.append(self._measure_speed(
                        gcmd, probe_gcmd, speed, thresholds, samples, lift,
                        worst, faults, start_z, lift_speed))
                except self.NoThreshold as e:
                    gcmd.respond_info("  speed %5.1f: unusable - %s"
                                      % (speed, e))
            if not measured:
                raise gcmd.error(
                    "ADXL_PROBE_CALIBRATE: no speed produced a usable"
                    " tap_thresh. Lower probe_accel, check the wiring with"
                    " QUERY_PROBE, and confirm the probe triggers when you tap"
                    " the nozzle by hand during a PROBE.%s"
                    % ("" if not faults['total'] else
                       " %d step(s) also failed on probe faults, the last"
                       " being: %s" % (faults['total'], faults['last'])))
            # The pair whose average trigger height sits closest to nominal
            # zero wins - the one that felt the bed with the least travel
            # past it. The spread breaks ties.
            measured.sort(key=lambda r: (round(r['accuracy'], 4),
                                         r['spread']))
            best = measured[0]
            self.tap_thresh = best['thresh']
            self._write_tap_regs()
            if saved_speed is not None:
                self.param_helper.speed = best['speed']
            keep = True
            self._lift_to(start_z, lift_speed)
            gcmd.respond_info("ADXL_PROBE_CALIBRATE: results, best first")
            for rank, r in enumerate(measured):
                gcmd.respond_info(
                    "  %d. speed %5.1f  tap_thresh %6.0f  accuracy %.4f"
                    "  (average z %+.4f)  range %.4f  sigma %.4f  (%d taps)"
                    % (rank + 1, r['speed'], r['thresh'], r['accuracy'],
                       r['mean'], r['spread'], r['sigma'], r['samples']))
        finally:
            self.managed_session = False
            self.cal_point = None
            if not keep:
                # Best effort: an SPI fault here must not replace the error
                # that actually stopped the run, nor skip the teardown
                self.tap_thresh = saved_thresh
                try:
                    self._write_tap_regs()
                except Exception:
                    logging.exception("adxl345_probe: cannot restore"
                                      " tap_thresh")
                self._lift_to(start_z, lift_speed)
            self.end_probe_session()
        # Staged, not written: SAVE_CONFIG is the user's call, and it restarts
        # Klipper. The values are live in the meantime.
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.config_name, 'tap_thresh',
                       "%.0f" % (self.tap_thresh,))
        configfile.set(self.config_name, 'speed', "%g" % (best['speed'],))
        gcmd.respond_info(
            "ADXL_PROBE_CALIBRATE: best accuracy was %.4f mm (average trigger"
            " height %+.4f, range %.4f) at speed %g with tap_thresh %.0f. Both"
            " are applied now and staged for [%s]:\n"
            "speed: %g\ntap_thresh: %.0f\n"
            "Run SAVE_CONFIG to keep them - it restarts Klipper."
            % (best['accuracy'], best['mean'], best['spread'], best['speed'],
               self.tap_thresh, self.config_name, best['speed'],
               self.tap_thresh))

    # A print job in progress means the bed is occupied and the toolhead is
    # part way through someone's work. print_stats covers Moonraker-driven
    # prints, virtual_sdcard covers a file streamed by Klipper itself. A job
    # streamed line by line over the serial port from a host that does
    # neither cannot be detected from here.
    PRINT_BUSY_STATES = ('printing', 'paused')

    def _active_print(self):
        eventtime = self.printer.get_reactor().monotonic()
        stats = self.printer.lookup_object('print_stats', None)
        if stats is not None:
            try:
                state = stats.get_status(eventtime).get('state')
            except Exception:
                logging.exception("adxl345_probe: cannot read print_stats")
                state = None
            if state in self.PRINT_BUSY_STATES:
                return "a print job is %s" % (state,)
        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if sdcard is not None:
            try:
                if sdcard.get_status(eventtime).get('is_active'):
                    return "the virtual sdcard is streaming a file"
            except Exception:
                logging.exception("adxl345_probe: cannot read virtual_sdcard")
        return None

    # Middle of the travel the kinematics report, less the probe offsets so
    # that the probe - not the toolhead origin - ends up in the middle.
    # Returns (None, None) if the kinematics do not report a range.
    def _bed_center(self, status):
        low, high = status.get('axis_minimum'), status.get('axis_maximum')
        if low is None or high is None:
            return None, None
        offsets = self.probe_offsets.get_offsets()
        return ((low.x + high.x) / 2. - offsets[0],
                (low.y + high.y) / 2. - offsets[1])

    # Home if needed and travel to the probing point, so the command is
    # usable on its own without a G28/G1 preamble. Returns the Z it settled
    # at, which is the height the first tap at each threshold descends from.
    def _move_to_probe_point(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        z = gcmd.get_float('Z', CAL_Z, above=0.)
        travel = gcmd.get_float('TRAVEL_SPEED', CAL_TRAVEL_SPEED, above=0.)
        reactor = self.printer.get_reactor()
        status = toolhead.get_status(reactor.monotonic())
        if not all(a in status['homed_axes'] for a in 'xyz'):
            gcmd.respond_info("ADXL_PROBE_CALIBRATE: homing first")
            self.printer.lookup_object('gcode').run_script_from_command("G28")
            status = toolhead.get_status(reactor.monotonic())
            if not all(a in status['homed_axes'] for a in 'xyz'):
                raise gcmd.error("ADXL_PROBE_CALIBRATE: G28 did not home X,"
                                 " Y and Z")
        center_x, center_y = self._bed_center(status)
        if center_x is None and (gcmd.get('X', None) is None
                                 or gcmd.get('Y', None) is None):
            raise gcmd.error(
                "ADXL_PROBE_CALIBRATE: the kinematics do not report a travel"
                " range, so the middle of the bed cannot be worked out. Pass"
                " X= and Y= explicitly.")
        x = gcmd.get_float('X', center_x)
        y = gcmd.get_float('Y', center_y)
        dev = gcmd.get_float('DEVIATION', CAL_DEVIATION, minval=0.)
        self.cal_point = self._scatter_area(gcmd, status, x, y, dev, travel)
        # Reach the probing height first, then traverse - never the other way
        # round. On a delta the reachable radius collapses to nothing at the
        # top of the travel, so traversing at the height G28 leaves the
        # effector at is out of range for every point except the one homing
        # ended on, which on a calibrated delta is not the middle of the bed.
        # Z clears the bed by definition: it is the height the run descends
        # from.
        toolhead.manual_move([None, None, z], travel)
        toolhead.manual_move([x, y, None], travel)
        area = self.cal_point
        gcmd.respond_info(
            "ADXL_PROBE_CALIBRATE: probing at X%.3f Y%.3f from Z%.3f%s"
            % (x, y, z, "" if not dev else
               ", climbing over X%.3f-%.3f Y%.3f-%.3f"
               % (area['x_lo'], area['x_hi'], area['y_lo'], area['y_hi'])))
        return z

    # The square of half-width `dev` around (x, y), clipped to the travel the
    # kinematics report so a scattered tap can never ask for an out-of-range
    # move. A bed edge clips the square rather than failing the command: the
    # deviation is there to spread wear, not to define a specific area.
    def _scatter_area(self, gcmd, status, x, y, dev, travel):
        x_lo, x_hi = x - dev, x + dev
        y_lo, y_hi = y - dev, y + dev
        low, high = status.get('axis_minimum'), status.get('axis_maximum')
        if dev and low is not None and high is not None:
            x_lo, x_hi = max(x_lo, low.x), min(x_hi, high.x)
            y_lo, y_hi = max(y_lo, low.y), min(y_hi, high.y)
            if x_lo > x_hi or y_lo > y_hi:
                raise gcmd.error(
                    "ADXL_PROBE_CALIBRATE: X%.3f Y%.3f is outside the travel"
                    " range, so no probing area fits around it" % (x, y))
            if (x_hi - x_lo < 2 * dev) or (y_hi - y_lo < 2 * dev):
                gcmd.respond_info(
                    "ADXL_PROBE_CALIBRATE: DEVIATION=%g runs off the edge"
                    " of the travel range - the probing area was clipped"
                    % (dev,))
        return {'x': x, 'y': y, 'dev': dev, 'speed': travel,
                'x_lo': x_lo, 'x_hi': x_hi, 'y_lo': y_lo, 'y_hi': y_hi,
                'radius2': self._round_bed_radius2(status)}

    # Delta kinematics report the bounding square of a round bed, so a point
    # inside the reported range can still be off the edge of what the effector
    # can reach. A range symmetric about the origin in both axes, with the
    # same reach either way, is that shape; treat it as a circle. Returns the
    # squared radius, or None for a rectangular bed.
    def _round_bed_radius2(self, status):
        low, high = status.get('axis_minimum'), status.get('axis_maximum')
        if low is None or high is None:
            return None
        if low.x != -high.x or low.y != -high.y or high.x != high.y:
            return None
        return high.x ** 2


# Main external probe interface - mirrors probe.PrinterProbe
class ADXL345Probe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.probe_offsets = probe.ProbeOffsetsHelper(config)
        self.param_helper = probe.ProbeParameterHelper(config)
        self.mcu_probe = ADXL345EndstopWrapper(config, self.probe_offsets,
                                               self.param_helper)
        self.probe_session = probe.SampleAveragingHelper(
            config, self.param_helper, self.mcu_probe.start_probe_session)
        query_endstop = self.mcu_probe.query_endstop
        self.cmd_helper = probe.ProbeCommandHelper(config, self, query_endstop)
        probe.HomingViaProbeHelper(config,
                                   self.probe_offsets.get_offsets()[2],
                                   query_endstop)
        self.printer.add_object('probe', self)

    def get_probe_params(self, gcmd=None):
        return self.param_helper.get_probe_params(gcmd)

    def get_offsets(self, gcmd=None):
        return self.probe_offsets.get_offsets(gcmd)

    def get_status(self, eventtime):
        return self.cmd_helper.get_status(eventtime)

    def start_probe_session(self, gcmd):
        return self.probe_session.start_probe_session(gcmd)


def load_config(config):
    return ADXL345Probe(config)
