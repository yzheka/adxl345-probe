# Offline harness for ADXL_PROBE_CALIBRATE.
#
# Stubs enough of Klipper to drive cmd_ADXL_PROBE_CALIBRATE against a simulated
# printer whose tap detection misfires below one register value and misses the
# bed above another, with both edges moving as probing speed changes.
# Verifies that the threshold walk stops at the first setting that taps, that
# the accuracy measurement picks the best speed, and that faults abort the run
# rather than being recorded as tuning verdicts.
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
                'axis_minimum': self.sim.axis_minimum,
                'axis_maximum': self.sim.axis_maximum}

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
        if code == sim.flaky_code:
            # Works the first time, misfires after that: a threshold right on
            # the edge of usable
            sim.flaky_taps += 1
            if sim.flaky_taps > 1:
                raise CommandError("Probe triggered prior to movement")
        if code > deaf:
            th.pos[2] = sim.z_min
            raise CommandError("No trigger on probe after full movement")
        th.pos[2] = sim.trigger(speed) + sim.jitter(speed)
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
        # speed -> trigger height, for measuring the average the ranking uses
        self.trigger_heights = {}
        self.z_min = -2.
        # Cartesian travel by default. A delta reports the square that bounds
        # its round bed, symmetric about the origin.
        self.axis_minimum = Coord(x=0., y=0., z=0.)
        self.axis_maximum = Coord(x=300., y=220., z=250.)
        self.tested = []
        # XY the toolhead was at for every descent
        self.probe_points = []
        # A register that taps once and misfires from then on
        self.flaky_code = None
        self.flaky_taps = 0
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

    def trigger(self, speed):
        """Height the tap latches at. A harder-hitting probe carries further
        past the surface before the chip sees it, so this moves with speed."""
        return self.trigger_heights.get(speed, self.trigger_z)

    def jitter(self, speed):
        noise = self.noise.get(speed, 0.)
        self.noise_step += 1
        if isinstance(noise, (list, tuple)):
            # Explicit offsets, cycled. Lets two speeds share a spread while
            # differing in sigma, which is what breaks a tie.
            return noise[self.noise_step % len(noise)]
        # deterministic triangle: hits +/- amplitude/2 across any 4 samples
        return noise * ((self.noise_step % 4) - 1.5) / 3.


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
                'SAMPLES': 4, 'DEVIATION': 0}


def walk_lands_on(wrapper, sensitive, deaf, lo=10420., hi=100000., step=613.):
    """The threshold the walk has to stop at: the first rung it actually probes
    whose register is at or above the misfire edge. None if that one already
    misses the bed, which is the whole band gone. Rungs at or below 1g are
    reported and skipped rather than probed, and a rung landing on a register
    already tried is dropped."""
    import extras.adxl345_probe as mod
    thresh, seen = lo, set()
    while thresh <= hi + 1e-9:
        code = wrapper._tap_code(thresh)
        if code > mod.TAP_GRAVITY_CODE and code not in seen:
            seen.add(code)
            if code >= sensitive:
                return None if code > deaf else thresh
        thresh += step
    return None


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
    print("\n=== %s (misfires below reg %d, deaf above %d) ==="
          % (name, sensitive_edge, deaf_edge))
    try:
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
    except CommandError as e:
        print("  ERROR: %s" % e)
        assert expect_error and expect_error in str(e), \
            "unexpected error: %s" % e
        # an aborted run must leave the configured threshold untouched
        assert sim.chip.regs[0x1D] == wrapper._tap_code(5000.), \
            "threshold not restored after abort: reg %d" % sim.chip.regs[0x1D]
        assert not sim.configfile.saved, "aborted run staged config values"
        assert sim.configfile.save_config_calls == 0
        print("  -> expected error, config untouched, ok")
        return
    assert expect_error is None, "expected error %r, got success" % expect_error
    want = walk_lands_on(
        wrapper, sensitive_edge, deaf_edge,
        float(args.get('THRESHOLD_START', 10420.)),
        float(args.get('THRESHOLD_END', 100000.)),
        float(args.get('THRESHOLD_STEP', 613.)))
    assert want is not None, "the case itself expects no usable threshold"
    staged = sim.configfile.saved.get(('adxl345_probe', 'tap_thresh'))
    print("  taps: %d, thresholds tried: %d"
          % (len(sim.tested), len(set(c for _s, c in sim.tested))))
    print("  live reg: %d  staged: %s  SAVE_CONFIG calls: %d"
          % (sim.chip.regs[0x1D], staged, sim.configfile.save_config_calls))
    assert sim.chip.regs[0x1D] == wrapper._tap_code(want), \
        "landed on reg %d, expected %d (tap_thresh %.0f)" \
        % (sim.chip.regs[0x1D], wrapper._tap_code(want), want)
    # Staged for the user's own SAVE_CONFIG, never written by the command
    assert sim.configfile.save_config_calls == 0, "the command ran SAVE_CONFIG"
    assert staged is not None, "nothing staged for SAVE_CONFIG"
    assert wrapper._tap_code(float(staged)) == sim.chip.regs[0x1D], \
        "staged %s re-reads as reg %d, not %d" \
        % (staged, wrapper._tap_code(float(staged)), sim.chip.regs[0x1D])
    assert sim.configfile.saved[('adxl345_probe', 'speed')] == '5'
    assert wrapper.param_helper.speed == 5., "live speed not applied"
    print("  -> ok")


probe_stub.ProbeOffsetsHelper = object
probe_stub.ProbeParameterHelper = object
probe_stub.SampleAveragingHelper = object
probe_stub.ProbeCommandHelper = object
probe_stub.HomingViaProbeHelper = object

run("first threshold already taps", 1, 200, {})
run("misfire edge just above the floor", 18, 200, {})
run("misfire edge mid range", 90, 200, {})
run("misfire edge near the end", 160, 200, {})
# The default step is one register, so even a one-register band is found
run("narrow band, one register wide", 62, 62, {})
# A coarser step advances one or two registers at a time and can step clean
# over such a band - 1000 mm/s^2 misses register 19, among 56 others
run("a coarse step steps over it", 19, 19, {'THRESHOLD_STEP': 1000},
    expect_error="no speed produced a usable tap_thresh")
run("...and the default step finds it", 19, 19, {})
# Below 1g the chip can never latch a tap, so the walk starts above it however
# low it is asked to start
run("a start below 1g is raised to the floor", 90, 200,
    {'THRESHOLD_START': 1000})
run("coarser threshold step", 90, 200, {'THRESHOLD_STEP': 5000})
run("range given explicitly", 90, 200,
    {'THRESHOLD_START': 40000, 'THRESHOLD_END': 80000})
run("whole range misfires", 250, 300, {},
    expect_error="no speed produced a usable tap_thresh")
run("nothing detects the bed", 1, 0, {},
    expect_error="no speed produced a usable tap_thresh")


# --- defaults ---------------------------------------------------------------

def defaults_case():
    """The bare command's sweep and threshold ladder, from the module
    defaults."""
    import extras.adxl345_probe as mod
    wrapper = build(Sim(60, 200), mod)
    gcmd = GCmd({}, [], quiet=True)
    speeds = wrapper._speeds(gcmd)
    thresholds = wrapper._thresholds(gcmd)
    print("\n=== defaults ===")
    print("  speeds: %s" % (", ".join("%g" % s for s in speeds),))
    print("  thresholds: %g, %g, %g ... %g (%d of them)"
          % (thresholds[0], thresholds[1], thresholds[2], thresholds[-1],
             len(thresholds)))
    assert (speeds[0], speeds[-1], len(speeds)) == (10., 30., 11), \
        "speeds %s" % (speeds,)
    assert wrapper._tap_code(thresholds[0]) == mod.TAP_FLOOR_CODE, \
        "starts at %g, register %d" \
        % (thresholds[0], wrapper._tap_code(thresholds[0]))
    assert thresholds[-1] <= 100000., "ends at %g" % thresholds[-1]
    # one register per rung, so every register in the range is tried
    codes = [wrapper._tap_code(t) for t in thresholds]
    assert codes == list(range(codes[0], codes[-1] + 1)), \
        "the default step skips registers"
    assert mod.TAP_GRAVITY_CODE == 16, \
        "1g is register %d, expected 16" % mod.TAP_GRAVITY_CODE
    print("  -> ok")


def floor_case():
    """The walk starts wherever THRESHOLD_START says, including below 1g. Those
    rungs are stepped over, never probed: a probe there would drive the nozzle
    to the descent floor to demonstrate what the register value already
    proves."""
    import extras.adxl345_probe as mod
    # The default starts on the first usable register, so nothing is dead
    # there; a low start walks through the whole dead zone
    for start, want_dead in ((None, 0), (1000, 16)):
        sim = Sim(30, 200)
        wrapper = build(sim, mod)
        log = []
        args = {'SPEED_START': 5, 'SPEED_END': 5, 'DEVIATION': 0}
        if start is not None:
            args['THRESHOLD_START'] = start
        gcmd = GCmd(args, log, quiet=True)
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
        dead = [c for _sp, c in sim.tested if c <= mod.TAP_GRAVITY_CODE]
        announced = [ln for ln in log
                     if 'at or below 1g and are skipped' in ln]
        print("\n=== 1g floor: THRESHOLD_START=%s ==="
              % (start if start is not None else 'default',))
        print("  first probed reg %d, %d dead rungs announced up front"
              % (sim.tested[0][1], want_dead))
        assert not dead, "probed registers %s, at or below 1g" % (dead,)
        if want_dead:
            assert announced, "the dead rungs were not announced"
            assert "the first %d are" % want_dead in announced[0], \
                "announced %r, expected %d dead rungs" \
                % (announced[0], want_dead)
        else:
            assert not announced, "announced dead rungs it does not have"
        assert sim.tested[0][1] == mod.TAP_FLOOR_CODE, \
            "first probe was reg %d" % sim.tested[0][1]
    print("  -> ok")


floor_case()


def dedupe_case():
    """A step finer than one register (612.9 mm/s^2) would re-probe the same
    chip setting, so those thresholds are dropped."""
    import extras.adxl345_probe as mod
    wrapper = build(Sim(60, 200), mod)
    thresholds = wrapper._thresholds(
        GCmd({'THRESHOLD_START': 11000, 'THRESHOLD_END': 14000,
              'THRESHOLD_STEP': 100}, [], quiet=True))
    codes = [wrapper._tap_code(t) for t in thresholds]
    print("\n=== threshold ladder: 100 mm/s^2 step over 11000-14000 ===")
    print("  %d thresholds, registers %s" % (len(thresholds), codes))
    assert len(codes) == len(set(codes)), "the same register is probed twice"
    print("  -> ok")


defaults_case()
dedupe_case()


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


# --- exhaustive walk property test ------------------------------------------

def walk_property_test(step=1000.):
    """For every reachable (misfire edge, deaf edge) pair, the walk must stop
    at the first threshold it tries that taps, or report that speed unusable.
    Narrow bands are the interesting ones: a step of 1000 mm/s^2 advances the
    register by one or two, so a one-register band can be stepped over."""
    import extras.adxl345_probe as mod
    checked = failures = skipped = 0
    for sensitive in range(0, 164, 1):
        for deaf in range(max(0, sensitive - 2), 164, 5):
            sim = Sim(sensitive, deaf)
            wrapper = build(sim, mod)
            args = dict(SINGLE_SPEED)
            args['THRESHOLD_STEP'] = step
            gcmd = GCmd(args, [])
            gcmd.respond_info = lambda msg: None
            want = walk_lands_on(wrapper, sensitive, deaf, step=step)
            checked += 1
            try:
                wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
            except CommandError as e:
                msg = str(e)
                if want is None and 'no speed produced a usable' in msg:
                    skipped += 1
                else:
                    failures += 1
                    print("  MISMATCH s=%d d=%d: want %s, got %s"
                          % (sensitive, deaf, want, msg))
                continue
            if want is None:
                failures += 1
                print("  MISMATCH s=%d d=%d: succeeded with no usable band"
                      % (sensitive, deaf))
                continue
            got = sim.chip.regs[0x1D]
            if got != wrapper._tap_code(want):
                failures += 1
                print("  MISMATCH s=%d d=%d: got reg %d, want %d"
                      % (sensitive, deaf, got, wrapper._tap_code(want)))
            # the nozzle must never be left parked at the descent floor
            if sim.toolhead.pos[2] <= sim.z_min:
                failures += 1
                print("  MISMATCH s=%d d=%d: left at z %.3f"
                      % (sensitive, deaf, sim.toolhead.pos[2]))
    print("\n=== walk property test: %d bands, THRESHOLD_STEP=%g ==="
          % (checked, step))
    print("  %d had no usable threshold and were reported as such" % skipped)
    assert not failures, "%d mismatches" % failures
    print("  -> ok")


walk_property_test(step=1000.)
walk_property_test(step=613.)
walk_property_test(step=5000.)


# --- error classification ---------------------------------------------------

def classification_case(name, error_text, expect_abort):
    """A fault must abort the run, not be recorded as a tuning verdict."""
    import extras.adxl345_probe as mod
    sim = Sim(60, 200)
    sim.fault = error_text
    wrapper = build(sim, mod)
    log = []
    args = dict(SINGLE_SPEED)
    gcmd = GCmd(args, log)
    gcmd.respond_info = lambda msg: log.append(msg)
    print("\n=== classification: %s ===" % name)
    try:
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
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


# --- multi-speed measurement ------------------------------------------------

def speed_case(name, bands, noise, want_speed, params=None,
               expect_error=None, triggers=None):
    """Drive the full speed sweep against a printer whose working band and
    trigger height both move with probing speed."""
    import extras.adxl345_probe as mod
    sim = Sim(60, 200, bands=bands, noise=noise)
    sim.trigger_heights = triggers or {}
    wrapper = build(sim, mod)
    log = []
    args = {'SPEED_START': 2, 'SPEED_END': 8, 'SPEED_STEP': 2,
            'SAMPLES': 4, 'DEVIATION': 0}
    args.update(params or {})
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== speeds: %s ===" % name)
    try:
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
    except CommandError as e:
        assert expect_error and expect_error in str(e), \
            "unexpected error: %s" % e
        print("  ERROR: %s" % e)
        print("  -> expected error, ok")
        return
    assert expect_error is None, "expected error %r" % expect_error
    for line in log:
        if line.startswith("  1.") or 'lowest average' in line:
            print("  %s" % line.strip().split("\n")[0])
    got_speed = float(sim.configfile.saved[('adxl345_probe', 'speed')])
    got_reg = sim.chip.regs[0x1D]
    assert got_speed == want_speed, \
        "picked speed %s, expected %s" % (got_speed, want_speed)
    # The register kept is the first rung of the ladder at or above the winning
    # speed's misfire edge - derived, so the ladder can change under it
    want_reg = wrapper._tap_code(walk_lands_on(wrapper, *bands[want_speed]))
    assert got_reg == want_reg, "picked reg %d, expected %d" % (got_reg,
                                                                want_reg)
    assert wrapper.param_helper.speed == want_speed, "live speed not applied"
    taps_by_speed = {}
    for sp, _c in sim.tested:
        taps_by_speed[sp] = taps_by_speed.get(sp, 0) + 1
    print("  taps per speed: %s" % taps_by_speed)
    print("  -> picked speed %g, reg %d, ok" % (got_speed, got_reg))


# The band drifts upward with speed - a faster tap hits harder, so it takes a
# higher threshold to stop misfiring. The register kept for each speed is the
# first rung of the ladder at or above its misfire edge, which the case derives
# rather than hard-coding.
DRIFT = {2.0: (20, 40), 4.0: (26, 50), 6.0: (34, 62), 8.0: (44, 78)}

# 4 mm/s latches closest to the surface, so it wins on average trigger height
speed_case("the lowest average trigger height wins", DRIFT, {}, 4.0,
           triggers={2.0: 0.030, 4.0: 0.012, 6.0: 0.020, 8.0: 0.040})

# Two speeds tie on the average - both latch at 0.020 - so the spread decides.
# The offset cycles are 4 long and SAMPLES is 4, so every measurement sees one
# whole cycle whatever the phase, and both average exactly 0.020.
speed_case("the spread breaks a tie on the average", DRIFT,
           {4.0: (-0.005, 0.005, -0.005, 0.005),
            6.0: (-0.001, 0.001, -0.001, 0.001)}, 6.0,
           triggers={2.0: 0.030, 4.0: 0.020, 6.0: 0.020, 8.0: 0.040})

# One speed has no usable threshold at all - it is skipped, not fatal
speed_case("a speed with no usable threshold is skipped",
           {2.0: (20, 40), 4.0: (60, 55), 6.0: (34, 62), 8.0: (44, 78)},
           {}, 6.0,
           triggers={2.0: 0.030, 4.0: 0.005, 6.0: 0.012, 8.0: 0.040})

# No speed works at all
speed_case("no speed works",
           {2.0: (200, 100), 4.0: (200, 100), 6.0: (200, 100),
            8.0: (200, 100)}, {}, None,
           expect_error="no speed produced a usable tap_thresh")


def accuracy_spot_case():
    """The accuracy taps all land on the same spot even with DEVIATION set:
    moving between them would fold the shape of the bed into the average."""
    import random
    import extras.adxl345_probe as mod
    random.seed(20250811)
    sim = Sim(22, 200)
    wrapper = build(sim, mod)
    log = []
    gcmd = GCmd({'SPEED_START': 5, 'SPEED_END': 5, 'SPEED_STEP': 1,
                 'SAMPLES': 6, 'DEVIATION': 20}, log, quiet=True)
    print("\n=== accuracy run: one spot, DEVIATION=20 ===")
    wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
    points = sim.probe_points
    walk, measured = points[:-6], points[-6:]
    print("  %d walk taps at %d distinct points, %d accuracy taps at %d"
          % (len(walk), len(set(walk)), len(measured), len(set(measured))))
    assert len(set(measured)) == 1, \
        "the accuracy run moved: %s" % (set(measured),)
    assert len(set(walk)) > 1, "the walk did not scatter: %s" % (set(walk),)
    # and it measured where the walk's successful tap landed
    assert measured[0] == walk[-1], \
        "measured at %s, tapped at %s" % (measured[0], walk[-1])
    print("  -> ok")


accuracy_spot_case()

def restart_case():
    """Every speed starts the walk over at THRESHOLD_START, so a band that
    moved a long way up is still found - and one that moved down is not
    missed."""
    import extras.adxl345_probe as mod
    bands = {2.0: (120, 150), 4.0: (20, 40)}
    sim = Sim(60, 200, bands=bands, noise={2.0: 0.02, 4.0: 0.01})
    wrapper = build(sim, mod)
    log = []
    gcmd = GCmd({'SPEED_START': 2, 'SPEED_END': 4, 'SPEED_STEP': 2,
                 'SAMPLES': 4, 'DEVIATION': 0}, log, quiet=True)
    print("\n=== speeds: the band moves down between speeds ===")
    wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
    for ln in log:
        if 'taps, range' in ln:
            print("  %s" % ln.strip())
    # Both walks start at the 1g floor, whatever the previous speed did
    for speed in (2.0, 4.0):
        first = [c for sp, c in sim.tested if sp == speed][0]
        assert first == mod.TAP_FLOOR_CODE, \
            "%g mm/s started at reg %d" % (speed, first)
    # 4 mm/s is both more accurate and lower in the range
    assert sim.chip.regs[0x1D] == wrapper._tap_code(
        walk_lands_on(wrapper, *bands[4.0])), \
        "reg %d" % sim.chip.regs[0x1D]
    assert float(sim.configfile.saved[('adxl345_probe', 'speed')]) == 4.
    print("  -> both walks restarted at 1000 mm/s^2, ok")


restart_case()


def accuracy_lift_case():
    """The accuracy taps lift LIFT mm between them, not back to Z, and the
    walk taps once at each threshold before that."""
    import extras.adxl345_probe as mod
    sim = Sim(28, 200)
    wrapper = build(sim, mod)
    sim.toolhead.moves = []
    log = []
    gcmd = GCmd({'SPEED_START': 5, 'SPEED_END': 5, 'SPEED_STEP': 1,
                 'SAMPLES': 5, 'DEVIATION': 0}, log, quiet=True)
    print("\n=== accuracy run: 1 mm lifts ===")
    wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
    lifts = [round(pos[2], 4) for pos, _sp in sim.toolhead.moves]
    print("  lift targets: %s" % lifts)
    # One tap per misfiring threshold from the start height, then the accuracy
    # run just above the bed
    assert lifts.count(10.0) >= 2, "walk did not descend from Z10: %s" % lifts
    near_bed = [z for z in lifts if z < 5.]
    assert len(near_bed) >= 3, "no 1 mm lifts: %s" % lifts
    for z in near_bed:
        assert 1.0 <= z <= 1.1, "lifted to %.4f, expected ~1 mm" % z
    print("  -> %d taps from ~1 mm, %d from Z10, ok"
          % (len(near_bed), lifts.count(10.0)))


accuracy_lift_case()


def lift_guard_case():
    """A LIFT at or below min_probe_travel would make every accuracy tap look
    like a misfire, so it is refused up front."""
    import extras.adxl345_probe as mod
    sim = Sim(28, 200)
    wrapper = build(sim, mod)
    log = []
    gcmd = GCmd({'SPEED_START': 5, 'SPEED_END': 5, 'LIFT': 0.4}, log,
                quiet=True)
    print("\n=== accuracy run: LIFT below min_probe_travel ===")
    try:
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
    except CommandError as e:
        print("  ERROR: %s" % e)
        assert 'min_probe_travel' in str(e), "unexpected error: %s" % e
        assert not sim.tested, "%d taps ran anyway" % len(sim.tested)
        print("  -> refused before tapping, ok")
        return
    raise AssertionError("LIFT=0.4 with min_probe_travel=0.5 was accepted")


lift_guard_case()


def lift_default_case():
    """LIFT defaults to twice min_probe_travel, so a machine that demands more
    travel than usual does not have to be told about it."""
    import extras.adxl345_probe as mod
    print("\n=== accuracy run: LIFT follows min_probe_travel ===")
    for travel, want_lift in ((0.5, 1.0), (1.5, 3.0), (0., 1.0)):
        sim = Sim(28, 200)
        wrapper = build(sim, mod, {'min_probe_travel': travel})
        sim.toolhead.moves = []
        gcmd = GCmd({'SPEED_START': 5, 'SPEED_END': 5, 'SPEED_STEP': 1,
                     'SAMPLES': 4, 'DEVIATION': 0}, [], quiet=True)
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
        # The accuracy taps lift from the last trigger, so the height they
        # start from is trigger + LIFT
        near_bed = [pos[2] for pos, _sp in sim.toolhead.moves if pos[2] < 5.]
        assert near_bed, "no accuracy taps ran"
        got = min(near_bed) - sim.trigger_z
        print("  min_probe_travel %.1f -> lifts to ~%.2f, so LIFT is %.2f"
              % (travel, min(near_bed), got))
        assert abs(got - want_lift) < 0.01, \
            "LIFT came out as %.3f, expected %.3f" % (got, want_lift)
    print("  -> ok")


lift_default_case()


def intermittent_case():
    """A threshold that taps once but breaks down during the accuracy run is
    not good enough: the walk carries on up instead of failing the speed."""
    import extras.adxl345_probe as mod
    sim = Sim(28, 200)
    # The ladder tries every register, so the first one at or above the
    # misfire edge is 28 itself. Make it work once and then misfire.
    sim.flaky_code = 28
    wrapper = build(sim, mod)
    log = []
    gcmd = GCmd({'SPEED_START': 5, 'SPEED_END': 5, 'SPEED_STEP': 1,
                 'SAMPLES': 4, 'DEVIATION': 0}, log, quiet=True)
    print("\n=== accuracy run: flaky threshold ===")
    wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
    raised = [ln for ln in log if 'raising tap_thresh' in ln]
    for ln in raised:
        print("  %s" % ln.strip())
    assert raised, "the flaky threshold was not reported"
    assert sim.chip.regs[0x1D] > 28, \
        "kept the flaky threshold (reg %d)" % sim.chip.regs[0x1D]
    print("  -> walked past it to reg %d, ok" % sim.chip.regs[0x1D])


intermittent_case()


# --- positioning ------------------------------------------------------------

def position_case(name, homed, params, want_xy, want_z, want_homing,
                  start_pos=None, expect_error=None, home_result='xyz',
                  axis_range=None):
    import extras.adxl345_probe as mod
    sim = Sim(60, 200)
    if axis_range is not None:
        sim.axis_minimum, sim.axis_maximum = axis_range
    wrapper = build(sim, mod)
    sim.toolhead.homed_axes = homed
    sim.home_result = home_result
    if start_pos is not None:
        sim.toolhead.pos = list(start_pos)
    sim.toolhead.moves = []
    log = []
    args = dict(SINGLE_SPEED)
    args.update(params)
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== positioning: %s ===" % name)
    try:
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
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
    # Z has to reach the probing height before anything traverses in XY. On a
    # delta the reachable radius at the height G28 finishes at is nil, so a
    # traverse up there is "Move out of range" for every point but one.
    first_pos, _first_speed = sim.toolhead.moves[0]
    assert round(first_pos[2], 3) == want_z, \
        "first move went to z %.3f, not the probing height %s" \
        % (first_pos[2], want_z)
    xy = [ln for ln in log if 'probing at' in ln]
    assert xy, "no probing point reported"
    print("  %s" % xy[0].strip())
    # Parse after "probing at", not by splitting on X/Y/Z - the command name
    # itself has an X in it
    fields = xy[0].split('probing at ')[1].replace('from ', '').split()
    got_x, got_y, got_z = (float(f[1:]) for f in fields[:3])
    assert (round(got_x, 3), round(got_y, 3)) == want_xy, \
        "went to X%.3f Y%.3f, expected %s" % (got_x, got_y, (want_xy,))
    assert round(got_z, 3) == want_z, "Z%.3f, expected %s" % (got_z, want_z)
    # the run must end back at that Z
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
# A delta homes at the top of the travel, where the reachable radius is nil.
# The bed is centred on the origin, so the probing point is 0, 0.
DELTA_RANGE = (Coord(x=-150., y=-150., z=-5.),
               Coord(x=150., y=150., z=419.685))
position_case("delta: descends before traversing", 'xyz', {},
              (0.0, 0.0), 10.0, 0, start_pos=[0.31, -0.44, 419.685],
              axis_range=DELTA_RANGE)
position_case("delta: homes, then descends before traversing", '', {},
              (0.0, 0.0), 10.0, 1, axis_range=DELTA_RANGE)


# --- DEVIATION -----------------------------------------------------

def deviation_case(name, params, want_area, expect_error=None,
                   expect_clip=False, axis_range=None, want_radius=None,
                   use_default=False):
    """Every tap must land inside the (clipped) square around the probing
    point, the taps must actually differ, and the nozzle must be clear of the
    bed before any traverse - dragging it sideways at trigger height would do
    exactly the damage the deviation exists to avoid."""
    import random
    import extras.adxl345_probe as mod
    random.seed(20250811)
    sim = Sim(60, 200)
    if axis_range is not None:
        sim.axis_minimum, sim.axis_maximum = axis_range
    wrapper = build(sim, mod)
    sim.toolhead.moves = []
    log = []
    args = dict(SINGLE_SPEED)
    args.update(params)
    if use_default:
        del args['DEVIATION']
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== deviation: %s ===" % name)
    try:
        wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
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
    # traverse. A traverse may only happen with the nozzle lifted clear: the
    # accuracy run works LIFT mm above the bed, so that is the floor.
    lift = float(args.get('LIFT', 1.))
    prev = None
    for pos, _speed in sim.toolhead.moves:
        if prev is not None and (round(pos[0], 6), round(pos[1], 6)) \
                != (round(prev[0], 6), round(prev[1], 6)):
            assert pos[2] >= lift - 1e-9, \
                "traversed to X%.3f Y%.3f at z %.3f, below the %g mm lift" \
                % (pos[0], pos[1], pos[2], lift)
        prev = pos
    if want_radius is not None:
        for x, y in points:
            assert x * x + y * y <= want_radius ** 2 + 1e-9, \
                "tapped X%.3f Y%.3f, off a bed of radius %g" \
                % (x, y, want_radius)
        print("  max radius %.3f of %g"
              % (max((p[0] ** 2 + p[1] ** 2) ** .5 for p in points),
                 want_radius))
    clipped = [ln for ln in log if 'was clipped' in ln]
    assert bool(clipped) == expect_clip, \
        "clip warning %s" % ("missing" if expect_clip else "unexpected")
    if expect_clip:
        print("  %s" % clipped[0].strip())
    print("  -> ok")


deviation_case("DEVIATION=0 taps one spot", {}, (150., 150., 110., 110.))
# The module default is 20 mm, so a bare command scatters
deviation_case("the 20 mm default scatters", {}, (130., 170., 90., 130.),
               use_default=True)
deviation_case("DEVIATION=5 scatters around the centre",
               {'DEVIATION': 5}, (145., 155., 105., 115.))
deviation_case("area is clipped to the travel range",
               {'X': 2, 'Y': 3, 'DEVIATION': 5},
               (0., 7., 0., 8.), expect_clip=True)
deviation_case("point outside the travel range is an error",
               {'X': 400, 'Y': 110, 'DEVIATION': 5}, None,
               expect_error="outside the travel range")
deviation_case("delta: scatters around the centre",
               {'DEVIATION': 5}, (-5., 5., -5., 5.),
               axis_range=DELTA_RANGE, want_radius=150.)
# The square around a point on the rim has corners off a round bed: the
# reported range allows them, the reachable area does not
deviation_case("delta: stays on a round bed at the rim",
               {'X': 149, 'Y': 0, 'DEVIATION': 5},
               (144., 150., -5., 5.), axis_range=DELTA_RANGE,
               want_radius=150., expect_clip=True)


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
    gcmd = GCmd(args, log, quiet=True)
    print("\n=== print guard: %s ===" % name)
    # must never raise: an error inside an SD print aborts the print
    wrapper.cmd_ADXL_PROBE_CALIBRATE(gcmd)
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
