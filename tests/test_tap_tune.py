# Offline harness for TEST_TAP_TUNE.
#
# Stubs enough of Klipper to drive cmd_TEST_TAP_TUNE against a simulated
# printer whose tap detection misfires below one register value and misses the
# bed above another, with both edges moving as probing speed changes.
# Verifies that the search lands on the true band at every speed, that the
# scoring picks the most repeatable pair, and that faults abort rather than
# being recorded as tuning verdicts.
import os
import shutil
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'adxl345_probe.py')
if not os.path.exists(SRC):
    SRC = os.path.join(os.path.dirname(HERE), 'adxl345_probe.py')
PKG = '/tmp/klippystub/extras'
os.makedirs(PKG, exist_ok=True)
open(os.path.join(PKG, '__init__.py'), 'w').close()
open('/tmp/klippystub/__init__.py', 'w').close()
shutil.copy(SRC, os.path.join(PKG, 'adxl345_probe.py'))

adxl345_stub = types.ModuleType('extras.adxl345')
adxl345_stub.FREEFALL_ACCEL = 9806.65
adxl345_stub.REG_POWER_CTL = 0x2D
adxl345_stub.REG_DATA_FORMAT = 0x31

probe_stub = types.ModuleType('extras.probe')


class CommandError(Exception):
    pass


Coord = types.SimpleNamespace


class Toolhead:
    def __init__(self, sim):
        self.sim = sim
        self.pos = [0., 0., 20.]
        self.max_accel = 3000.
        self.homed_axes = 'xyz'
        self.moves = []

    def get_position(self):
        return list(self.pos)

    def get_status(self, eventtime):
        return {'max_accel': self.max_accel,
                'homed_axes': self.homed_axes,
                'axis_minimum': Coord(x=0., y=0., z=0.),
                'axis_maximum': Coord(x=300., y=220., z=250.)}

    def manual_move(self, coord, speed):
        for i, v in enumerate(coord):
            if v is not None:
                self.pos[i] = v
        self.moves.append((list(self.pos), speed))

    def flush_step_generation(self):
        pass

    def dwell(self, t):
        pass

    def get_last_move_time(self):
        return 0.

    def set_max_velocities(self, vel, accel, scv, saccel):
        if accel is not None:
            self.max_accel = accel


class Chip:
    def __init__(self):
        self.regs = {}
        self.mcu = types.SimpleNamespace(print_time_to_clock=lambda t: 0)

    def set_reg(self, reg, val, minclock=0):
        self.regs[reg] = val

    def read_reg(self, reg):
        return self.regs.get(reg, 0)


class DescendHelper:
    def __init__(self, sim):
        self.sim = sim
        self.results = []

    def clear_trigger_positions(self):
        self.results = []

    def pull_trigger_positions(self):
        res = self.results
        self.results = []
        return res

    def descend_until_trigger(self, gcmd):
        sim = self.sim
        code = sim.chip.regs.get(0x1D, 0)
        speed = float(gcmd.params.get('PROBE_SPEED', 5.))
        sim.tested.append((speed, code))
        th = sim.toolhead
        sim.probe_points.append((round(th.pos[0], 6), round(th.pos[1], 6)))
        if sim.fault is not None and len(sim.tested) >= sim.fault_after:
            raise CommandError(sim.fault)
        sensitive, deaf = sim.band(speed)
        if code < sensitive:
            # misfires on the start-of-move acceleration: no travel
            raise CommandError("Probe triggered prior to movement")
        if code > deaf:
            th.pos[2] = sim.z_min
            raise CommandError("No trigger on probe after full movement")
        th.pos[2] = sim.trigger_z + sim.jitter(speed)
        self.results.append(list(th.pos))


class ConfigFile:
    def __init__(self):
        self.saved = {}
        self.save_config_calls = 0

    def set(self, section, option, value):
        self.saved[(section, option)] = value


class GCode:
    def __init__(self, sim):
        self.sim = sim

    def register_mux_command(self, *a, **k):
        pass

    def create_gcode_command(self, command, commandline, params):
        return GCmd(dict(params), [], quiet=True)

    def run_script_from_command(self, script):
        if script == 'SAVE_CONFIG':
            self.sim.configfile.save_config_calls += 1
        elif script == 'G28':
            self.sim.homing_calls += 1
            self.sim.toolhead.homed_axes = self.sim.home_result
            self.sim.toolhead.pos = [0., 0., 250.]


class ConfigError(Exception):
    pass


class FanDriver:
    """Faithful stand-in for fan.Fan: requests are queued, and _apply_speed
    scales by max_power and clamps by off_below before storing the result in
    last_req_value (which is what get_status reports)."""

    def __init__(self, speed=0., max_power=1., off_below=0.):
        self.max_power = max_power
        self.off_below = off_below
        self.last_req_value = 0.
        self.gcrq = types.SimpleNamespace(rqueue=[])
        self.set_speed_from_command(speed)
        self.flush()

    def set_speed_from_command(self, value):
        self.gcrq.rqueue.append((0., value))

    def flush(self):
        for _, value in self.gcrq.rqueue:
            if value < self.off_below:
                value = 0.
            self.last_req_value = max(0., min(self.max_power,
                                              value * self.max_power))
        self.gcrq.rqueue = []

    @property
    def speed(self):
        """The physical output."""
        return self.last_req_value


class PrinterFan:
    """[fan] - no fan_speed attribute at all (the crash upstream hits)."""

    def __init__(self, speed=0., max_power=1., off_below=0.):
        self.fan = FanDriver(speed, max_power, off_below)

    def get_status(self, eventtime):
        return {'speed': self.fan.last_req_value}


class TemplatedFan(PrinterFan):
    """[fan_generic] with SET_FAN_SPEED TEMPLATE - re-requests its speed
    every 0.5s through fan.Fan directly, with no section-level attribute."""

    def __init__(self, speed=0.7):
        PrinterFan.__init__(self, speed)
        self.template_value = speed

    def callback(self):
        self.fan.set_speed_from_command(self.template_value)


class HeaterFan(PrinterFan):
    """[heater_fan x] - re-drives itself from a periodic callback."""

    def __init__(self, speed=1.):
        PrinterFan.__init__(self, speed)
        self.fan_speed = 1.
        self.heating = True

    def callback(self):
        speed = self.fan_speed if self.heating else 0.
        self.fan.set_speed_from_command(speed)


class ControllerFan(PrinterFan):
    def __init__(self, speed=0.6):
        PrinterFan.__init__(self, speed)
        self.fan_speed = 1.
        self.idle_speed = 0.6


class Printer:
    def __init__(self, sim):
        self.sim = sim
        self.command_error = CommandError
        self.config_error = ConfigError

    def lookup_objects(self, module=None):
        return list(self.sim.fans.items())

    def lookup_object(self, name, default='\x00'):
        objs = {'toolhead': self.sim.toolhead, 'adxl345': self.sim.chip,
                'configfile': self.sim.configfile,
                'gcode': self.sim.gcode,
                'pins': self.sim.pins}
        objs.update(self.sim.fans)
        objs.update({k: v for k, v in self.sim.printer_objects.items()
                     if v is not None})
        if name in objs:
            return objs[name]
        if default == '\x00':
            raise CommandError("unknown object %s" % name)
        return default

    def load_object(self, config, name):
        return self.sim.gcode_macro

    def register_event_handler(self, name, cb):
        pass

    def get_reactor(self):
        return types.SimpleNamespace(monotonic=lambda: 0.)

    def add_object(self, name, obj):
        pass


class Template:
    def run_gcode_from_command(self):
        pass


class Config:
    def __init__(self, sim, values):
        self.sim = sim
        self.values = values
        self.error = CommandError

    def get_printer(self):
        return self.sim.printer

    def get_name(self):
        return 'adxl345_probe'

    def get(self, name, default='\x00'):
        if name in self.values:
            return self.values[name]
        if default == '\x00':
            raise CommandError("missing %s" % name)
        return default

    def getfloat(self, name, default=None, **kw):
        return float(self.values.get(name, default)) \
            if self.values.get(name, default) is not None else None


class GCmd:
    def __init__(self, params, log, quiet=False):
        self.params = {k: str(v) for k, v in params.items()}
        self.log = log
        self.quiet = quiet
        self.error = CommandError

    def get_command_parameters(self):
        return dict(self.params)

    def get(self, name, default=None, **kw):
        return self.params.get(name, default)

    def get_float(self, name, default=None, **kw):
        if name in self.params:
            return float(self.params[name])
        if default is None:
            raise CommandError("missing %s" % name)
        return float(default)

    def get_int(self, name, default=None, **kw):
        if name in self.params:
            return int(self.params[name])
        return int(default)

    def respond_info(self, msg):
        self.log.append(msg)
        if not self.quiet:
            print(msg)


class Sim:
    def __init__(self, sensitive_edge, deaf_edge, bands=None, noise=None):
        self.sensitive_edge = sensitive_edge
        self.deaf_edge = deaf_edge
        # speed -> (misfire edge, deaf edge); falls back to the fixed pair
        self.bands = bands or {}
        # speed -> trigger height spread, cycled deterministically
        self.noise = noise or {}
        self.noise_step = 0
        self.trigger_z = 0.02
        self.z_min = -2.
        self.tested = []
        # XY the toolhead was at for every descent
        self.probe_points = []
        self.fault = None
        self.fault_after = 3
        self.homing_calls = 0
        self.home_result = 'xyz'
        # print_stats / virtual_sdcard are optional in a Klipper config
        self.printer_objects = {}
        self.fans = {}
        self.chip = Chip()
        self.toolhead = Toolhead(self)
        self.configfile = ConfigFile()
        self.gcode = GCode(self)
        self.gcode_macro = types.SimpleNamespace(
            load_template=lambda c, n, d: Template())
        self.pins = types.SimpleNamespace(
            lookup_pin=lambda p, can_invert=False, can_pullup=False: {
                'chip': types.SimpleNamespace(
                    setup_pin=lambda t, pp: types.SimpleNamespace(
                        query_endstop=lambda pt: False))})
        self.printer = Printer(self)

    def band(self, speed):
        return self.bands.get(speed, (self.sensitive_edge, self.deaf_edge))

    def jitter(self, speed):
        amplitude = self.noise.get(speed, 0.)
        self.noise_step += 1
        # deterministic triangle: hits +/- amplitude/2 across any 4 samples
        return amplitude * ((self.noise_step % 4) - 1.5) / 3.


def build(sim, mod, extra_config=None):
    probe_stub.DescendToEndstopHelper = \
        lambda config, es, offs, ph: DescendHelper(sim)
    offsets = types.SimpleNamespace(get_offsets=lambda gcmd=None: (0., 0., 0.))
    values = {'int_pin': 'int1', 'probe_pin': 'gpio21',
              'tap_thresh': 5000., 'tap_dur': 0.01,
              'min_probe_travel': 0.5, 'rest_time': 0.1,
              'probe_accel': 1000.}
    values.update(extra_config or {})
    cfg = Config(sim, values)
    param_helper = types.SimpleNamespace(
        speed=5.,
        get_probe_params=lambda gcmd=None: {'lift_speed': 5.,
                                            'probe_speed': 5.})
    return mod.ADXL345EndstopWrapper(cfg, offsets, param_helper)


SINGLE_SPEED = {'SPEED_START': 5, 'SPEED_END': 5, 'SPEED_STEP': 1,
                'SAMPLES': 4}


def run(name, sensitive_edge, deaf_edge, params, expect_error=None):
    sim = Sim(sensitive_edge, deaf_edge)
    sys.modules['extras.probe'] = probe_stub
    sys.modules['extras.adxl345'] = adxl345_stub
    sys.path.insert(0, '/tmp/klippystub')
    import extras.adxl345_probe as mod
    wrapper = build(sim, mod)
    log = []
    args = dict(SINGLE_SPEED)
    args.update(params)
    gcmd = GCmd(args, log)
    print("\n=== %s (edge reg %d, deaf above %d) ==="
          % (name, sensitive_edge, deaf_edge))
    try:
        wrapper.cmd_TEST_TAP_TUNE(gcmd)
    except CommandError as e:
        print("  ERROR: %s" % e)
        assert expect_error and expect_error in str(e), \
            "unexpected error: %s" % e
        # an aborted search must leave the configured threshold untouched
        assert sim.chip.regs[0x1D] == wrapper._tap_code(5000.), \
            "threshold not restored after abort: reg %d" % sim.chip.regs[0x1D]
        assert not sim.configfile.saved, "aborted search wrote to the config"
        assert sim.configfile.save_config_calls == 0
        print("  -> expected error, config untouched, ok")
        return
    assert expect_error is None, "expected error %r, got success" % expect_error
    reg = sim.chip.regs[0x1D]
    saved = sim.configfile.saved.get(('adxl345_probe', 'tap_thresh'))
    print("  probe attempts: %d, distinct codes: %d"
          % (len(sim.tested), len(set(c for _s, c in sim.tested))))
    print("  final register: %d  saved: %s  SAVE_CONFIG: %d"
          % (reg, saved, sim.configfile.save_config_calls))
    margin = int(args.get('MARGIN', 2))
    lo = int(float(args.get('THRESSHOLD_START', 10000.)) / mod.TAP_SCALE)
    hi = int(float(args.get('THRESSHOLD_END', 100000.)) / mod.TAP_SCALE)
    want = min(max(sensitive_edge, lo) + margin, min(deaf_edge, hi))
    assert reg == want, "expected reg %d, got %d" % (want, reg)
    if not int(args.get('SAVE', 1)):
        # SAVE=0 must not stage anything: configfile.set() would leave
        # save_config_pending set and break later SAVE_CONFIG calls
        assert saved is None, "SAVE=0 still wrote %s" % saved
        assert sim.configfile.save_config_calls == 0
    else:
        # the saved value must re-read to the same register on next boot
        assert wrapper._tap_code(float(saved)) == reg, \
            "saved %s re-reads as reg %d, not %d" \
            % (saved, wrapper._tap_code(float(saved)), reg)
        assert sim.configfile.saved[('adxl345_probe', 'speed')] == '5'
        assert sim.configfile.save_config_calls == 1
    print("  -> ok")


probe_stub.ProbeOffsetsHelper = object
probe_stub.ProbeParameterHelper = object
probe_stub.SampleAveragingHelper = object
probe_stub.ProbeCommandHelper = object
probe_stub.HomingViaProbeHelper = object

run("edge just above start", 20, 200, {'SAVE': 1})
run("edge mid range", 90, 200, {'SAVE': 1})
run("edge near end", 160, 200, {'SAVE': 0})
run("start already passes", 5, 200, {'SAVE': 0})
run("deaf top, edge mid", 60, 100, {'SAVE': 0})
run("zero margin", 77, 200, {'MARGIN': 0, 'SAVE': 0})
run("whole range misfires", 250, 300, {'SAVE': 0},
    expect_error="no speed produced a usable tap_thresh")
run("nothing detects the bed", 5, 5, {'SAVE': 0},
    expect_error="no speed produced a usable tap_thresh")


# --- disable_fans -----------------------------------------------------------

def fan_sim(mod):
    sim = Sim(60, 200)
    sim.fans = {'fan': PrinterFan(0.8),
                'heater_fan hotend_fan': HeaterFan(1.0),
                'controller_fan electronics': ControllerFan(0.6),
                'fan_generic toolhead_fan': PrinterFan(0.4),
                # max_power scales every request; a naive save/restore of
                # get_status()['speed'] would shrink this one each session
                'fan_generic limited': PrinterFan(1.0, max_power=0.6),
                'fan_generic templated': TemplatedFan(0.7)}
    return sim


def tick(sim):
    """Advance the self-driving fans and flush the request queues."""
    for obj in sim.fans.values():
        if hasattr(obj, 'callback'):
            obj.callback()
    for obj in sim.fans.values():
        obj.fan.flush()


def fan_case(name, disable_fans, expect_off, expect_error=None):
    import extras.adxl345_probe as mod
    sim = fan_sim(mod)
    print("\n=== disable_fans: %s ===" % disable_fans)
    wrapper = build(sim, mod, {'disable_fans': disable_fans})
    try:
        wrapper._resolve_fans()
    except ConfigError as e:
        print("  CONFIG ERROR: %s" % e)
        assert expect_error and expect_error in str(e), \
            "unexpected error: %s" % e
        print("  -> expected error, ok")
        return
    assert expect_error is None, "expected error %r" % expect_error
    assert [e['name'] for e in wrapper.fan_objects] == expect_off, \
        "resolved %s" % [e['name'] for e in wrapper.fan_objects]
    before = {n: o.fan.speed for n, o in sim.fans.items()}
    # two sessions back to back: a value that shrinks per session shows up
    for run_no in (1, 2):
        wrapper._control_fans(True)
        tick(sim)
        tick(sim)
        for n, o in sim.fans.items():
            if n in expect_off:
                assert o.fan.speed == 0., \
                    "%s still at %s (run %d)" % (n, o.fan.speed, run_no)
            else:
                assert o.fan.speed == before[n], "%s was touched" % n
        if run_no == 1:
            print("  off while probing: %s"
                  % {n: o.fan.speed for n, o in sim.fans.items()})
        wrapper._control_fans(False)
        tick(sim)
        for n, o in sim.fans.items():
            assert abs(o.fan.speed - before[n]) < 1e-9, \
                "%s restored to %s, expected %s (run %d)" \
                % (n, o.fan.speed, before[n], run_no)
    print("  restored: %s" % {n: o.fan.speed for n, o in sim.fans.items()})
    print("  -> ok")


ALL_FANS = ['fan', 'heater_fan hotend_fan', 'controller_fan electronics',
            'fan_generic toolhead_fan', 'fan_generic limited',
            'fan_generic templated']

fan_case("plain [fan] plus short heater_fan name", 'fan, hotend_fan',
         ['fan', 'heater_fan hotend_fan'])
fan_case("every fan type", ', '.join(ALL_FANS), ALL_FANS)
fan_case("unknown name", 'nozzle_fan', [], expect_error="no fan named")
fan_case("empty", '', [])


def fan_pending_case():
    """A speed change issued just before the probe is still queued: its value
    must be what gets restored, not the stale last_req_value."""
    import extras.adxl345_probe as mod
    sim = fan_sim(mod)
    print("\n=== disable_fans: pending request not yet applied ===")
    wrapper = build(sim, mod, {'disable_fans': 'fan'})
    wrapper._resolve_fans()
    driver = sim.fans['fan'].fan
    driver.set_speed_from_command(1.0)   # queued, not flushed
    assert driver.last_req_value == 0.8, "precondition"
    wrapper._control_fans(True)
    tick(sim)
    assert driver.speed == 0., "fan not off"
    wrapper._control_fans(False)
    tick(sim)
    assert driver.speed == 1.0, "restored to %s, expected 1.0" % driver.speed
    print("  restored the queued 1.0, not the stale 0.8 -> ok")


fan_pending_case()


# --- session teardown -------------------------------------------------------

def session_case(name, break_startup=False, abort_via='end'):
    import extras.adxl345_probe as mod
    sim = fan_sim(mod)
    print("\n=== teardown: %s ===" % name)
    wrapper = build(sim, mod, {'disable_fans': 'fan, hotend_fan'})
    wrapper._resolve_fans()
    before = {n: o.fan.speed for n, o in sim.fans.items()}
    if break_startup:
        def boom(reg):
            raise CommandError("Failed to read ADXL345 register")
        sim.chip.read_reg = boom
        try:
            wrapper.start_probe_session(None)
        except CommandError as e:
            print("  start_probe_session failed: %s" % e)
            tick(sim)
        else:
            raise AssertionError("expected the startup failure to propagate")
    else:
        wrapper.start_probe_session(None)
        tick(sim)
        assert sim.chip.regs[0x2D] == 0x08, "chip not powered up"
        for n in ('fan', 'heater_fan hotend_fan'):
            assert sim.fans[n].fan.speed == 0., "%s not switched off" % n
        if abort_via == 'command_error':
            wrapper._handle_command_error()
        else:
            wrapper.end_probe_session()
        assert sim.chip.regs[0x2D] == 0x00, "chip left powered"
        # a second teardown must be harmless
        wrapper.end_probe_session()
        wrapper._handle_command_error()
    tick(sim)
    after = {n: o.fan.speed for n, o in sim.fans.items()}
    assert after == before, "fans not restored: %s vs %s" % (after, before)
    assert all(e['saved'] is None for e in wrapper.fan_objects)
    print("  fans restored: %s" % after)
    print("  -> ok")


session_case("normal end_probe_session")
session_case("aborted probe (gcode:command_error)", abort_via='command_error')
session_case("failure inside start_probe_session", break_startup=True)


# --- exhaustive search property test ----------------------------------------

def search_property_test(margin=2):
    """For every reachable (misfire edge, deaf edge) pair, the command must
    either find the true lowest working register + margin, or fail with the
    error that matches the band it was given. The old phase-1 walk-down
    silently skipped narrow working bands, so this sweeps all of them."""
    import extras.adxl345_probe as mod
    lo, hi = 16, 163
    checked = failures = 0
    for sensitive in range(lo - 4, hi + 4):
        for deaf in range(lo - 4, hi + 4):
            if deaf < sensitive - 1:
                continue          # deafness always starts above misfiring
            sim = Sim(sensitive, deaf)
            wrapper = build(sim, mod)
            args = dict(SINGLE_SPEED)
            args.update({'MARGIN': margin, 'SAVE': 0})
            gcmd = GCmd(args, [])
            gcmd.respond_info = lambda msg: None
            band_low, band_high = max(sensitive, lo), min(deaf, hi)
            checked += 1
            try:
                wrapper.cmd_TEST_TAP_TUNE(gcmd)
            except CommandError as e:
                msg = str(e)
                ok = (band_low > band_high
                      and 'no speed produced a usable tap_thresh' in msg)
                if not ok:
                    failures += 1
                    print("  MISMATCH s=%d d=%d: %s" % (sensitive, deaf, msg))
                continue
            if band_low > band_high:
                failures += 1
                print("  MISMATCH s=%d d=%d: succeeded, band is empty"
                      % (sensitive, deaf))
                continue
            want = min(band_low + margin, band_high)
            got = sim.chip.regs[0x1D]
            if got != want:
                failures += 1
                print("  MISMATCH s=%d d=%d: got reg %d, want %d"
                      % (sensitive, deaf, got, want))
            # the nozzle must never be left parked at the descent floor
            if sim.toolhead.pos[2] <= sim.z_min:
                failures += 1
                print("  MISMATCH s=%d d=%d: left at z %.3f"
                      % (sensitive, deaf, sim.toolhead.pos[2]))
    print("\n=== search property test: %d bands, MARGIN=%d ==="
          % (checked, margin))
    assert not failures, "%d mismatches" % failures
    print("  -> ok")


search_property_test(margin=2)
search_property_test(margin=0)


# --- error classification ---------------------------------------------------

def classification_case(name, error_text, expect_abort):
    """A fault must abort the search, not be recorded as a tuning verdict."""
    import extras.adxl345_probe as mod
    sim = Sim(60, 200)
    sim.fault = error_text
    wrapper = build(sim, mod)
    log = []
    args = dict(SINGLE_SPEED)
    args['SAVE'] = 0
    gcmd = GCmd(args, log)
    gcmd.respond_info = lambda msg: log.append(msg)
    print("\n=== classification: %s ===" % name)
    try:
        wrapper.cmd_TEST_TAP_TUNE(gcmd)
        msg = "completed"
    except CommandError as e:
        msg = str(e)
    # A fault must surface as itself. A tuning result must be digested into
    # the command's own diagnosis instead of being echoed raw.
    # only the command's own final error counts: the per-candidate verdict
    # lines legitimately echo the probe message as their detail field
    leaked = error_text in msg
    assert leaked == expect_abort, \
        "raw error %s in output: %r" % ("missing from" if expect_abort
                                        else "leaked into", msg)
    print("  %s: %s" % ("propagated the real cause" if expect_abort
                        else "digested into a tuning verdict", msg))
    print("  -> ok")


classification_case(
    "ADXL345 SPI readback mismatch",
    "Failed to set ADXL345 register [0x1d] to 0x20: got 0x0.", True)
classification_case(
    "MCU homing comms timeout",
    "Error during homing probe: Communication timeout during homing", True)
classification_case("printer shutdown", "Probing failed due to printer"
                    " shutdown", True)
classification_case("move out of range", "Move out of range: 0 0 -422.2", True)
classification_case("genuine misfire", "ADXL345 probe triggered after only"
                    " 0.000mm of travel (minimum 0.500mm)", False)


# --- multi-speed tuning -----------------------------------------------------

def speed_case(name, bands, noise, want_speed, want_reg, params=None,
               expect_error=None):
    """Drive the full speed sweep against a printer whose working band and
    trigger repeatability both move with probing speed."""
    import extras.adxl345_probe as mod
    sim = Sim(60, 200, bands=bands, noise=noise)
    wrapper = build(sim, mod)
    log = []
    args = {'SPEED_START': 2, 'SPEED_END': 8, 'SPEED_STEP': 2,
            'SAMPLES': 4, 'SAVE': 1}
    args.update(params or {})
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== speeds: %s ===" % name)
    try:
        wrapper.cmd_TEST_TAP_TUNE(gcmd)
    except CommandError as e:
        assert expect_error and expect_error in str(e), \
            "unexpected error: %s" % e
        print("  ERROR: %s" % e)
        print("  -> expected error, ok")
        return
    assert expect_error is None, "expected error %r" % expect_error
    for line in log:
        if line.startswith("TEST_TAP_TUNE: results") or line.startswith("  1."):
            print("  %s" % line.strip())
    got_speed = float(sim.configfile.saved[('adxl345_probe', 'speed')])
    got_reg = sim.chip.regs[0x1D]
    assert got_speed == want_speed, \
        "picked speed %s, expected %s" % (got_speed, want_speed)
    assert got_reg == want_reg, "picked reg %d, expected %d" % (got_reg,
                                                                want_reg)
    assert wrapper.param_helper.speed == want_speed, "live speed not applied"
    probes_by_speed = {}
    for sp, _c in sim.tested:
        probes_by_speed[sp] = probes_by_speed.get(sp, 0) + 1
    print("  probes per speed: %s" % probes_by_speed)
    print("  -> picked speed %g, reg %d, ok" % (got_speed, got_reg))


# Band drifts upward with speed (a faster tap hits harder, so it takes a
# higher threshold to stop misfiring); 4 mm/s is the most repeatable.
DRIFT = {2.0: (20, 40), 4.0: (26, 50), 6.0: (34, 62), 8.0: (44, 78)}
speed_case("repeatability picks the winner", DRIFT,
           {2.0: 0.020, 4.0: 0.004, 6.0: 0.012, 8.0: 0.030}, 4.0, 28)

# Same bands, but two speeds tie on repeatability: the wider band wins
speed_case("band width breaks the tie", DRIFT,
           {2.0: 0.020, 4.0: 0.010, 6.0: 0.010, 8.0: 0.030}, 6.0, 36)

# One speed has no usable band at all - it is skipped, not fatal
speed_case("a speed with no usable band is skipped",
           {2.0: (20, 40), 4.0: (60, 55), 6.0: (34, 62), 8.0: (44, 78)},
           {2.0: 0.030, 4.0: 0.001, 6.0: 0.008, 8.0: 0.020}, 6.0, 36)

# No speed works at all
speed_case("no speed works",
           {2.0: (200, 100), 4.0: (200, 100), 6.0: (200, 100),
            8.0: (200, 100)}, {}, None, None,
           expect_error="no speed produced a usable tap_thresh")


def window_case():
    """The carried-over window must not truncate the reported band: a band
    that drifts far between speeds has to be re-found, not clipped."""
    import extras.adxl345_probe as mod
    bands = {2.0: (20, 40), 4.0: (120, 150)}
    sim = Sim(60, 200, bands=bands, noise={2.0: 0.02, 4.0: 0.01})
    wrapper = build(sim, mod)
    log = []
    gcmd = GCmd({'SPEED_START': 2, 'SPEED_END': 4, 'SPEED_STEP': 2,
                 'SAMPLES': 4, 'SAVE': 0, 'WINDOW': 4}, log, quiet=True)
    print("\n=== speeds: band jumps outside the carried window ===")
    wrapper.cmd_TEST_TAP_TUNE(gcmd)
    lines = [ln for ln in log if 'band reg' in ln]
    for ln in lines:
        print("  %s" % ln.strip())
    assert any('band reg 20-40' in ln for ln in lines), "2 mm/s band wrong"
    assert any('band reg 120-150' in ln for ln in lines), \
        "4 mm/s band was clipped to the carried window"
    assert sim.chip.regs[0x1D] == 122, "reg %d" % sim.chip.regs[0x1D]
    print("  -> both bands found in full, ok")


window_case()


# --- positioning ------------------------------------------------------------

def position_case(name, homed, params, want_xy, want_z, want_homing,
                  start_pos=None, expect_error=None, home_result='xyz'):
    import extras.adxl345_probe as mod
    sim = Sim(60, 200)
    wrapper = build(sim, mod)
    sim.toolhead.homed_axes = homed
    sim.home_result = home_result
    if start_pos is not None:
        sim.toolhead.pos = list(start_pos)
    sim.toolhead.moves = []
    log = []
    args = dict(SINGLE_SPEED)
    args.update({'SAVE': 0})
    args.update(params)
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== positioning: %s ===" % name)
    try:
        wrapper.cmd_TEST_TAP_TUNE(gcmd)
    except CommandError as e:
        assert expect_error and expect_error in str(e), \
            "unexpected error: %s" % e
        print("  ERROR: %s" % e)
        print("  -> expected error, ok")
        return
    assert expect_error is None, "expected error %r" % expect_error
    assert sim.homing_calls == want_homing, \
        "homed %d times, expected %d" % (sim.homing_calls, want_homing)
    # the first moves are the travel to the probing point
    travel = sim.toolhead.moves[:3]
    print("  travel: %s" % [([round(c, 2) for c in p], sp)
                            for p, sp in travel])
    xy = [ln for ln in log if 'probing at' in ln]
    assert xy, "no probing point reported"
    print("  %s" % xy[0].strip())
    got_x = float(xy[0].split('X')[1].split()[0])
    got_y = float(xy[0].split('Y')[1].split()[0])
    got_z = float(xy[0].split('Z')[1].split()[0])
    assert (round(got_x, 3), round(got_y, 3)) == want_xy, \
        "went to X%.3f Y%.3f, expected %s" % (got_x, got_y, (want_xy,))
    assert round(got_z, 3) == want_z, "Z%.3f, expected %s" % (got_z, want_z)
    # every probe in the search must start from that Z
    assert sim.toolhead.pos[2] == want_z, \
        "finished at z %.3f" % sim.toolhead.pos[2]
    print("  -> ok")


# axis_maximum in the stub is 300 x 220, so the middle is 150, 110
position_case("bare command homes and centres itself", '', {},
              (150.0, 110.0), 10.0, 1)
position_case("already homed, no G28", 'xyz', {}, (150.0, 110.0), 10.0, 0)
position_case("Z overridden", 'xyz', {'Z': 15}, (150.0, 110.0), 15.0, 0)
position_case("X/Y overridden", 'xyz', {'X': 42, 'Y': 17, 'Z': 5},
              (42.0, 17.0), 5.0, 0)
position_case("partially homed still homes", 'xy', {}, (150.0, 110.0),
              10.0, 1)
position_case("nozzle starts on the bed - lifts before traversing", 'xyz',
              {}, (150.0, 110.0), 10.0, 0, start_pos=[5., 5., 0.2])
position_case("G28 that does not home everything is an error", '', {}, None,
              None, 1, expect_error="G28 did not home", home_result='xy')


# --- TEST_TAP_DEVIATION -----------------------------------------------------

def deviation_case(name, params, want_area, expect_error=None,
                   expect_clip=False):
    """Every tap must land inside the (clipped) square around the probing
    point, the taps must actually differ, and the nozzle must be at the start
    height before any traverse - dragging it across the bed at trigger height
    would do exactly the damage the deviation exists to avoid."""
    import random
    import extras.adxl345_probe as mod
    random.seed(20250811)
    sim = Sim(60, 200)
    wrapper = build(sim, mod)
    sim.toolhead.moves = []
    log = []
    args = dict(SINGLE_SPEED)
    args['SAVE'] = 0
    args.update(params)
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== deviation: %s ===" % name)
    try:
        wrapper.cmd_TEST_TAP_TUNE(gcmd)
    except CommandError as e:
        assert expect_error and expect_error in str(e), \
            "unexpected error: %s" % e
        print("  ERROR: %s" % e)
        assert not sim.tested, "%d probes ran anyway" % len(sim.tested)
        print("  -> expected error, ok")
        return
    assert expect_error is None, "expected error %r" % expect_error
    x_lo, x_hi, y_lo, y_hi = want_area
    points = sim.probe_points
    assert points, "no probes ran"
    for x, y in points:
        assert x_lo - 1e-9 <= x <= x_hi + 1e-9 \
            and y_lo - 1e-9 <= y <= y_hi + 1e-9, \
            "tapped X%.3f Y%.3f, outside %s" % (x, y, (want_area,))
    distinct = len(set(points))
    print("  %d taps, %d distinct, X %.3f-%.3f Y %.3f-%.3f"
          % (len(points), distinct, min(p[0] for p in points),
             max(p[0] for p in points), min(p[1] for p in points),
             max(p[1] for p in points)))
    if x_hi > x_lo:
        assert distinct > len(points) // 2, \
            "%d taps but only %d distinct points" % (len(points), distinct)
    else:
        assert distinct == 1, "%d distinct points, expected one spot" \
            % (distinct,)
    # Descents are not commanded moves, so every recorded move is a lift or a
    # traverse: none of them may happen below the start height
    start_z = float(params.get('Z', 10))
    for pos, _speed in sim.toolhead.moves:
        assert pos[2] >= start_z - 1e-9, \
            "moved to X%.3f Y%.3f at z %.3f, below the start height" \
            % (pos[0], pos[1], pos[2])
    clipped = [ln for ln in log if 'was clipped' in ln]
    assert bool(clipped) == expect_clip, \
        "clip warning %s" % ("missing" if expect_clip else "unexpected")
    if expect_clip:
        print("  %s" % clipped[0].strip())
    print("  -> ok")


deviation_case("default taps one spot", {}, (150., 150., 110., 110.))
deviation_case("DEVIATION=5 scatters around the centre",
               {'TEST_TAP_DEVIATION': 5}, (145., 155., 105., 115.))
deviation_case("area is clipped to the travel range",
               {'X': 2, 'Y': 3, 'TEST_TAP_DEVIATION': 5},
               (0., 7., 0., 8.), expect_clip=True)
deviation_case("point outside the travel range is an error",
               {'X': 400, 'Y': 110, 'TEST_TAP_DEVIATION': 5}, None,
               expect_error="outside the travel range")


# --- active print guard -----------------------------------------------------

class PrintStats:
    def __init__(self, state):
        self.state = state

    def get_status(self, eventtime):
        return {'state': self.state, 'filename': 'bench.gcode'}


class VirtualSD:
    def __init__(self, active):
        self.active = active

    def get_status(self, eventtime):
        return {'is_active': self.active, 'progress': 0.5}


def print_guard_case(name, objects, expect_blocked):
    import extras.adxl345_probe as mod
    sim = Sim(60, 200)
    sim.printer_objects = objects
    wrapper = build(sim, mod)
    sim.toolhead.moves = []
    log = []
    args = dict(SINGLE_SPEED)
    args['SAVE'] = 1
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== print guard: %s ===" % name)
    # must never raise: an error inside an SD print aborts the print
    wrapper.cmd_TEST_TAP_TUNE(gcmd)
    warned = [ln for ln in log if 'not starting' in ln]
    if expect_blocked:
        assert warned, "no warning printed"
        assert not sim.tested, "%d probes ran anyway" % len(sim.tested)
        assert not sim.toolhead.moves, "the toolhead was moved"
        assert sim.homing_calls == 0, "it homed during a print"
        assert not sim.configfile.saved, "it wrote to the config"
        assert sim.configfile.save_config_calls == 0, "it forced a restart"
        assert sim.chip.regs.get(0x1D, wrapper._tap_code(5000.)) \
            == wrapper._tap_code(5000.), "tap_thresh was changed"
        print("  %s" % warned[0].strip())
        print("  -> blocked, nothing touched, ok")
    else:
        assert not warned, "wrongly blocked: %s" % warned[0]
        assert sim.tested, "no probes ran"
        print("  -> ran normally (%d probes), ok" % len(sim.tested))


print_guard_case("printing via print_stats",
                 {'print_stats': PrintStats('printing')}, True)
print_guard_case("paused mid-print", {'print_stats': PrintStats('paused')},
                 True)
print_guard_case("virtual sdcard streaming",
                 {'virtual_sdcard': VirtualSD(True)}, True)
print_guard_case("both present, both idle",
                 {'print_stats': PrintStats('standby'),
                  'virtual_sdcard': VirtualSD(False)}, False)
print_guard_case("finished print", {'print_stats': PrintStats('complete')},
                 False)
print_guard_case("cancelled print", {'print_stats': PrintStats('cancelled')},
                 False)
print_guard_case("neither object configured", {}, False)

print("\nALL TESTS PASSED")
