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
#   TEST_TAP_TUNE     - sweeps probing speed, binary-searches the working
#                       tap_thresh band at each one, scores the pairs by
#                       probe repeatability, and saves the winner
#   disable_fans      - works with every fan section type, not only the ones
#                       that happen to expose a fan_speed attribute
#
# The accelerometer is also powered up once per probe session rather than
# once per sample, which removes two blocking SPI round-trips per sample.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import math
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

# TEST_TAP_TUNE defaults
TUNE_THRESSHOLD_START = 10000.
TUNE_THRESSHOLD_END = 100000.
TUNE_SPEED_START = 2.
TUNE_SPEED_END = 20.
TUNE_SPEED_STEP = 2.
TUNE_MAX_SPEEDS = 20
TUNE_TRIALS = 3
TUNE_SAMPLES = 10
TUNE_MARGIN = 2  # register steps of headroom added to the found edge
TUNE_Z = 10.  # height the search probes from
TUNE_TRAVEL_SPEED = 50.  # mm/s for the move to the probing point
TUNE_WINDOW = 16  # register steps searched around the previous speed's band

# Probe failures that are a tuning result rather than a fault. Everything
# else - an SPI readback mismatch, an MCU homing timeout, a shutdown - aborts
# the search instead of being recorded as "this threshold misfires", which
# would send the user off tuning a threshold that was never the problem.
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
                    ('TEST_TAP_TUNE', self.cmd_TEST_TAP_TUNE,
                     self.cmd_TEST_TAP_TUNE_help)]
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
            # TEST_TAP_TUNE owns this session and ends it in its own
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

    cmd_TEST_TAP_TUNE_help = (
        "Find the best probing speed and tap_thresh pair")

    # Raised when a threshold range contains no usable value. Distinct from
    # command_error so the caller can widen the search and try again, while a
    # real fault still propagates.
    class NoBand(Exception):
        pass

    # Return the toolhead to the height the search started from. Called after
    # a failed trial too: a 'deaf' probe ran the move to its end, which leaves
    # the nozzle loaded against the bed at the descent floor.
    def _lift_to(self, start_z, lift_speed):
        try:
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.manual_move([None, None, start_z], lift_speed)
        except Exception:
            logging.exception("adxl345_probe: cannot lift to %.3f", start_z)

    # A probe attempt at a given THRESH_TAP register value is classified as:
    #   pass      - the probe descended past min_probe_travel and triggered
    #   sensitive - it misfired (tap latched while arming, or triggered on the
    #               start-of-move acceleration): the threshold is too low
    #   deaf      - the move ran to the end without a trigger: too high
    # Returns (verdict, detail, trigger positions).
    def _test_tap_code(self, probe_gcmd, code, trials, start_z, lift_speed):
        toolhead = self.printer.lookup_object('toolhead')
        self.tap_thresh = self._code_thresh(code)
        self._write_tap_regs()
        zs = []
        for _ in range(trials):
            toolhead.manual_move([None, None, start_z], lift_speed)
            self.homing_helper.clear_trigger_positions()
            try:
                self.run_probe(probe_gcmd)
            except self.printer.command_error as e:
                msg = str(e)
                if any(t in msg for t in PROBE_DEAF_ERRORS):
                    # The move ran to the descent floor - get off the bed
                    self._lift_to(start_z, lift_speed)
                    return 'deaf', msg, zs
                if any(t in msg for t in PROBE_SENSITIVE_ERRORS):
                    return 'sensitive', msg, zs
                # Anything else is a fault, not a verdict: an SPI readback
                # mismatch, an MCU homing timeout, a shutdown, a move out of
                # range. Recording it as 'sensitive' would abort the search
                # blaming the threshold.
                self._lift_to(start_z, lift_speed)
                raise
            try:
                positions = self.homing_helper.pull_trigger_positions()
            except Exception:
                positions = None
            if positions:
                zs.append(positions[-1][2])
        if not zs:
            return 'pass', "triggered, no position reported", zs
        return 'pass', ("z min %.4f max %.4f range %.4f"
                        % (min(zs), max(zs), max(zs) - min(zs))), zs

    # Lowest and highest register value that works, found by bisection. The
    # chip stores the threshold at 612.9 mm/s**2 per step, so the search runs
    # over register steps - anything finer would re-test the same register.
    def _find_band(self, test, lo, hi):
        if test(hi) == 'sensitive':
            raise self.NoBand(
                "tap_thresh %.0f (the top of the range) still misfires"
                % (self._code_thresh(hi),))
        if test(hi) == 'pass':
            top = hi
        else:
            # hi is deaf. Find the lowest deaf value; the one below it is the
            # most insensitive setting that still reaches the bed. Halving
            # towards lo instead would step over a narrow working band.
            low, high = lo, hi
            while low < high:
                mid = (low + high) // 2
                if test(mid) == 'deaf':
                    high = mid
                else:
                    low = mid + 1
            if low <= lo:
                raise self.NoBand(
                    "nothing in %.0f - %.0f mm/s^2 detected the bed"
                    % (self._code_thresh(lo), self._code_thresh(hi)))
            top = low - 1
            if test(top) != 'pass':
                raise self.NoBand(
                    "%.0f already misfires and %.0f, one register step"
                    " higher, misses the bed"
                    % (self._code_thresh(top), self._code_thresh(low)))
        # Bottom of the band. `edge` only ever moves to a value that has been
        # verified passing, so a stray result cannot produce a recommendation
        # that was never tested.
        edge = top
        low, high = lo, top
        while low < high:
            mid = (low + high) // 2
            if test(mid) == 'pass':
                edge = mid
                high = mid
            else:
                low = mid + 1
        return edge, top

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
    # at, which is the height every probe in the search starts from.
    def _move_to_probe_point(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        z = gcmd.get_float('Z', TUNE_Z, above=0.)
        travel = gcmd.get_float('TRAVEL_SPEED', TUNE_TRAVEL_SPEED, above=0.)
        reactor = self.printer.get_reactor()
        status = toolhead.get_status(reactor.monotonic())
        if not all(a in status['homed_axes'] for a in 'xyz'):
            gcmd.respond_info("TEST_TAP_TUNE: homing first")
            self.printer.lookup_object('gcode').run_script_from_command("G28")
            status = toolhead.get_status(reactor.monotonic())
            if not all(a in status['homed_axes'] for a in 'xyz'):
                raise gcmd.error("TEST_TAP_TUNE: G28 did not home X, Y and Z")
        center_x, center_y = self._bed_center(status)
        if center_x is None and (gcmd.get('X', None) is None
                                 or gcmd.get('Y', None) is None):
            raise gcmd.error(
                "TEST_TAP_TUNE: the kinematics do not report a travel range,"
                " so the middle of the bed cannot be worked out. Pass X= and"
                " Y= explicitly.")
        x = gcmd.get_float('X', center_x)
        y = gcmd.get_float('Y', center_y)
        # Lift before traversing: the nozzle may be sitting on the bed, or in
        # a print, from whatever ran before this
        if toolhead.get_position()[2] < z:
            toolhead.manual_move([None, None, z], travel)
        toolhead.manual_move([x, y, None], travel)
        toolhead.manual_move([None, None, z], travel)
        gcmd.respond_info("TEST_TAP_TUNE: probing at X%.3f Y%.3f from Z%.3f"
                          % (x, y, z))
        return z

    def _tune_speeds(self, gcmd):
        start = gcmd.get_float('SPEED_START', TUNE_SPEED_START, above=0.)
        end = gcmd.get_float('SPEED_END', TUNE_SPEED_END, above=0.)
        step = gcmd.get_float('SPEED_STEP', TUNE_SPEED_STEP, above=0.)
        if end < start:
            raise gcmd.error("SPEED_END must not be below SPEED_START")
        speeds = []
        speed = start
        while speed <= end + 1e-9:
            speeds.append(round(speed, 6))
            speed += step
            if len(speeds) > TUNE_MAX_SPEEDS:
                raise gcmd.error(
                    "SPEED_START/SPEED_END/SPEED_STEP asks for more than %d"
                    " speeds. Raise SPEED_STEP or narrow the range."
                    % (TUNE_MAX_SPEEDS,))
        return speeds

    def cmd_TEST_TAP_TUNE(self, gcmd):
        # Refuse before parsing anything, and refuse by returning rather than
        # raising: an error raised inside an SD print aborts the print
        # (virtual_sdcard breaks out of its work loop on gcode.error), which
        # is exactly what this guard exists to avoid.
        busy = self._active_print()
        if busy is not None:
            gcmd.respond_info(
                "!! TEST_TAP_TUNE: not starting - %s. This command homes the"
                " toolhead, drives to the middle of the bed and taps it a few"
                " hundred times, which would wreck the print. Run it when the"
                " printer is idle. Nothing has been changed." % (busy,))
            return
        # Both spellings accepted for the threshold range
        lo_thresh = gcmd.get_float(
            'THRESSHOLD_START',
            gcmd.get_float('THRESHOLD_START', TUNE_THRESSHOLD_START,
                           minval=TAP_SCALE, maxval=TAP_THRESH_MAX),
            minval=TAP_SCALE, maxval=TAP_THRESH_MAX)
        hi_thresh = gcmd.get_float(
            'THRESSHOLD_END',
            gcmd.get_float('THRESHOLD_END', TUNE_THRESSHOLD_END,
                           minval=TAP_SCALE, maxval=TAP_THRESH_MAX),
            minval=TAP_SCALE, maxval=TAP_THRESH_MAX)
        speeds = self._tune_speeds(gcmd)
        trials = gcmd.get_int('TRIALS', TUNE_TRIALS, minval=1, maxval=20)
        samples = gcmd.get_int('SAMPLES', TUNE_SAMPLES, minval=2, maxval=100)
        margin = gcmd.get_int('MARGIN', TUNE_MARGIN, minval=0, maxval=32)
        window = gcmd.get_int('WINDOW', TUNE_WINDOW, minval=0, maxval=255)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        lo = self._tap_code(lo_thresh)
        hi = self._tap_code(hi_thresh)
        if hi <= lo:
            raise gcmd.error("THRESSHOLD_END must be at least one register"
                             " step (%.0f mm/s^2) above THRESSHOLD_START"
                             % (TAP_SCALE,))
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
        scored = []

        gcmd.respond_info(
            "TEST_TAP_TUNE: speeds %s mm/s, tap_thresh %.0f - %.0f mm/s^2"
            " (reg %d - %d), %d trial(s) per candidate, %d sample(s) per"
            " speed. Roughly %d-%d probes - this takes a while."
            % (", ".join("%g" % s for s in speeds), self._code_thresh(lo),
               self._code_thresh(hi), lo, hi, trials, samples,
               len(speeds) * (5 * trials + samples),
               len(speeds) * (10 * trials + samples)))

        self.start_probe_session(gcmd)
        self.managed_session = True
        try:
            search_lo, search_hi = lo, hi
            for speed in speeds:
                params = dict(base_params)
                params['PROBE_SPEED'] = "%.6f" % (speed,)
                probe_gcmd = gcode.create_gcode_command("", "", params)
                results = {}

                def test(code, _pg=probe_gcmd, _res=results):
                    if code in _res:
                        return _res[code][0]
                    verdict, detail, _zs = self._test_tap_code(
                        _pg, code, trials, start_z, lift_speed)
                    _res[code] = (verdict, detail)
                    gcmd.respond_info(
                        "  speed %5.1f  tap_thresh %6.0f (reg %3d): %-9s %s"
                        % (speed, self._code_thresh(code), code, verdict,
                           detail))
                    return verdict

                try:
                    edge, top = self._find_band(test, search_lo, search_hi)
                except self.NoBand as e:
                    if (search_lo, search_hi) == (lo, hi):
                        gcmd.respond_info("  speed %5.1f: unusable - %s"
                                          % (speed, e))
                        continue
                    # The window carried over from the previous speed was too
                    # narrow. Widen to the full range before giving up on it.
                    gcmd.respond_info(
                        "  speed %5.1f: nothing in the carried-over window"
                        " (%s) - widening to the full range" % (speed, e))
                    search_lo, search_hi = lo, hi
                    try:
                        edge, top = self._find_band(test, lo, hi)
                    except self.NoBand as e2:
                        gcmd.respond_info("  speed %5.1f: unusable - %s"
                                          % (speed, e2))
                        continue
                # A band that reaches the edge of the carried-over window
                # probably extends past it. Re-search that side so the
                # reported band is the real one, not the window.
                if edge == search_lo and search_lo > lo:
                    edge, top = self._find_band(test, lo, top)
                if top == search_hi and search_hi < hi:
                    edge, top = self._find_band(test, edge, hi)
                candidate = min(edge + margin, top)
                verdict, detail, zs = self._test_tap_code(
                    probe_gcmd, candidate, samples, start_z, lift_speed)
                if verdict != 'pass' or len(zs) < 2:
                    gcmd.respond_info(
                        "  speed %5.1f  tap_thresh %6.0f: failed the"
                        " repeatability run (%s) - discarded"
                        % (speed, self._code_thresh(candidate), detail))
                    continue
                spread = max(zs) - min(zs)
                mean = sum(zs) / len(zs)
                sigma = math.sqrt(sum((z - mean) ** 2 for z in zs) / len(zs))
                scored.append({'speed': speed, 'code': candidate,
                               'edge': edge, 'top': top, 'width': top - edge,
                               'spread': spread, 'sigma': sigma,
                               'samples': len(zs)})
                gcmd.respond_info(
                    "  speed %5.1f  tap_thresh %6.0f (reg %3d): band reg"
                    " %d-%d (%d steps), %d samples, range %.4f sigma %.4f"
                    % (speed, self._code_thresh(candidate), candidate,
                       edge, top, top - edge, len(zs), spread, sigma))
                # Carry a window around this band into the next speed
                search_lo = max(lo, edge - window)
                search_hi = min(hi, top + window)
                if search_hi <= search_lo:
                    search_lo, search_hi = lo, hi
            if not scored:
                raise gcmd.error(
                    "TEST_TAP_TUNE: no speed produced a usable tap_thresh."
                    " Lower probe_accel, check the wiring with QUERY_PROBE,"
                    " and confirm the probe triggers when you tap the nozzle"
                    " by hand during a PROBE.")
            # Best repeatability wins; a wider working band breaks ties, since
            # it is the pair with the most room before it starts misfiring
            scored.sort(key=lambda r: (round(r['spread'], 4), -r['width']))
            best = scored[0]
            self.tap_thresh = self._code_thresh(best['code'])
            self._write_tap_regs()
            if saved_speed is not None:
                self.param_helper.speed = best['speed']
            keep = True
            self._lift_to(start_z, lift_speed)
            gcmd.respond_info("TEST_TAP_TUNE: results, best first")
            for rank, r in enumerate(scored):
                gcmd.respond_info(
                    "  %d. speed %5.1f  tap_thresh %6.0f  range %.4f"
                    "  sigma %.4f  band %d steps"
                    % (rank + 1, r['speed'], self._code_thresh(r['code']),
                       r['spread'], r['sigma'], r['width']))
        finally:
            self.managed_session = False
            if not keep:
                # Best effort: an SPI fault here must not replace the error
                # that actually stopped the search, nor skip the teardown
                self.tap_thresh = saved_thresh
                try:
                    self._write_tap_regs()
                except Exception:
                    logging.exception("adxl345_probe: cannot restore"
                                      " tap_thresh")
                self._lift_to(start_z, lift_speed)
            self.end_probe_session()
        gcmd.respond_info("TEST_TAP_TUNE: put this in [%s]:\n"
                          "speed: %g\ntap_thresh: %.0f"
                          % (self.config_name, best['speed'], self.tap_thresh))
        if not save:
            # Deliberately not staged with configfile.set(): that sets
            # save_config_pending, and if [adxl345_probe] lives in an included
            # file every later SAVE_CONFIG - including one from
            # BED_MESH_CALIBRATE - would then fail with an include conflict
            gcmd.respond_info("TEST_TAP_TUNE: active for this session only,"
                              " nothing written")
            return
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.config_name, 'tap_thresh',
                       "%.0f" % (self.tap_thresh,))
        configfile.set(self.config_name, 'speed', "%g" % (best['speed'],))
        gcmd.respond_info("TEST_TAP_TUNE: saving and restarting")
        gcode.run_script_from_command("SAVE_CONFIG")


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
