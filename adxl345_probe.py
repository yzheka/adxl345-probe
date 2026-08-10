# ADXL345 tap-detection Z probe
#
# Ported from https://github.com/jniebuhr/adxl345-probe for Klipper versions
# after the probe.py refactor that removed ProbeSessionHelper.
#
# Delta-friendly additions over upstream:
#   approach_z        - descend to this Z before arming tap detection, so a
#                       false trigger cannot ask the caller to retract above
#                       the machine's max_z (a delta homes AT max_z)
#   min_probe_travel  - reject a trigger that happens before the effector has
#                       actually descended, with a message that says why
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
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


# Equivalent of probe.ProbeEndstopWrapper, but arms ADXL345 tap detection
# around each probing move instead of deploying/stowing a physical probe.
class ADXL345EndstopWrapper:
    def __init__(self, config, probe_offsets, param_helper):
        self.printer = config.get_printer()
        self.param_helper = param_helper
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
                                          minval=TAP_SCALE, maxval=100000.)
        self.tap_dur = config.getfloat('tap_dur', 0.01,
                                       above=DUR_SCALE, maxval=0.1)
        # Approach height - on a delta the effector homes at max_z, so probing
        # from the home position leaves no headroom for the caller's retract
        self.approach_z = config.getfloat('approach_z', None)
        self.min_probe_travel = config.getfloat('min_probe_travel', 0.5,
                                                minval=0.)
        self.disable_fans = [f.strip()
                             for f in config.get('disable_fans', '').split(',')
                             if f.strip()]
        adxl345_name = config.get('chip', 'adxl345')
        self.adxl345 = self.printer.lookup_object(adxl345_name)
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
        # Session state
        self.is_measuring = False
        self.in_session = False
        self.printer.register_event_handler('klippy:connect', self._init_adxl)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('SET_ACCEL_PROBE', 'CHIP', None,
                                   self.cmd_SET_ACCEL_PROBE,
                                   desc=self.cmd_SET_ACCEL_PROBE_help)

    # --- ADXL345 register handling ---------------------------------------
    def _init_adxl(self):
        chip = self.adxl345
        chip.set_reg(adxl345.REG_POWER_CTL, 0x00)
        chip.set_reg(adxl345.REG_DATA_FORMAT, 0x2B if self.inverted else 0x0B)
        chip.set_reg(REG_INT_MAP, self.int_map)
        chip.set_reg(REG_TAP_AXES, 0x07)
        chip.set_reg(REG_THRESH_TAP, int(self.tap_thresh / TAP_SCALE))
        chip.set_reg(REG_DUR, int(self.tap_dur / DUR_SCALE))

    def _try_clear_tap(self):
        chip = self.adxl345
        for _ in range(8):
            if not (chip.read_reg(REG_INT_SOURCE) & 0x40):
                return True
        return False

    def _control_fans(self, disable):
        for name in self.disable_fans:
            fan = self.printer.lookup_object(name)
            if disable:
                fan._fan_speed = fan.fan_speed
                fan.fan_speed = 0.
            else:
                fan.fan_speed = fan._fan_speed
                fan._fan_speed = 0.

    def _approach(self, gcmd):
        # Get the effector down to a sane height before arming, so a false
        # trigger cannot leave the caller trying to retract past max_z
        if self.approach_z is None:
            return
        toolhead = self.printer.lookup_object('toolhead')
        if toolhead.get_position()[2] <= self.approach_z:
            return
        speed = self.param_helper.get_probe_params(gcmd)['lift_speed']
        toolhead.manual_move([None, None, self.approach_z], speed)

    def _arm_tap(self):
        self.activate_gcode.run_gcode_from_command()
        chip = self.adxl345
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        toolhead.dwell(ADXL345_REST_TIME)
        print_time = toolhead.get_last_move_time()
        clock = chip.mcu.print_time_to_clock(print_time)
        chip.set_reg(REG_INT_ENABLE, 0x00, minclock=clock)
        chip.read_reg(REG_INT_SOURCE)
        chip.set_reg(REG_INT_ENABLE, 0x40, minclock=clock)
        self.is_measuring = (chip.read_reg(adxl345.REG_POWER_CTL) == 0x08)
        if not self.is_measuring:
            chip.set_reg(adxl345.REG_POWER_CTL, 0x08, minclock=clock)
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

    def _disarm_tap(self):
        chip = self.adxl345
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.dwell(ADXL345_REST_TIME)
        print_time = toolhead.get_last_move_time()
        clock = chip.mcu.print_time_to_clock(print_time)
        chip.set_reg(REG_INT_ENABLE, 0x00, minclock=clock)
        if not self.is_measuring:
            chip.set_reg(adxl345.REG_POWER_CTL, 0x00)
        self.deactivate_gcode.run_gcode_from_command()
        if not self._try_clear_tap():
            raise self.printer.command_error(
                "ADXL345 tap triggered after move,"
                " it may be set too sensitive.")

    # --- Hardware probe session interface --------------------------------
    def start_probe_session(self, gcmd):
        self.homing_helper.clear_trigger_positions()
        self._control_fans(True)
        self.in_session = True
        return self

    def run_probe(self, gcmd):
        self._approach(gcmd)
        toolhead = self.printer.lookup_object('toolhead')
        start_z = toolhead.get_position()[2]
        self._arm_tap()
        try:
            self.homing_helper.descend_until_trigger(gcmd)
        except self.printer.command_error:
            try:
                self._disarm_tap()
            except Exception:
                logging.exception("adxl345_probe: error disarming tap")
            raise
        travel = start_z - toolhead.get_position()[2]
        self._disarm_tap()
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
        if self.in_session:
            self.in_session = False
            self._control_fans(False)

    # --- Commands ---------------------------------------------------------
    cmd_SET_ACCEL_PROBE_help = "Configure ADXL345 parameters related to probing"

    def cmd_SET_ACCEL_PROBE(self, gcmd):
        chip = self.adxl345
        self.tap_thresh = gcmd.get_float('TAP_THRESH', self.tap_thresh,
                                         minval=TAP_SCALE, maxval=100000.)
        self.tap_dur = gcmd.get_float('TAP_DUR', self.tap_dur,
                                      above=DUR_SCALE, maxval=0.1)
        chip.set_reg(REG_THRESH_TAP, int(self.tap_thresh / TAP_SCALE))
        chip.set_reg(REG_DUR, int(self.tap_dur / DUR_SCALE))


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
