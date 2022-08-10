from opendbc.can.packer import CANPacker
from common.realtime import DT_CTRL
from selfdrive.car import apply_toyota_steer_torque_limits
from selfdrive.car.chrysler.chryslercan import create_lkas_hud, create_lkas_command, create_cruise_buttons, acc_command
from selfdrive.car.chrysler.values import CAR, RAM_CARS, CarControllerParams
from common.conversions import Conversions as CV
from common.params import Params, put_nonblocking
from cereal import car
import math

LongCtrlState = car.CarControl.Actuators.LongControlState

# braking
BRAKE_CHANGE = 0.06

ACCEL_MIN = -3.5


class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.apply_steer_last = 0
    self.frame = 0
    self.hud_count = 0
    self.last_lkas_falling_edge = 0
    self.lkas_control_bit_prev = False
    self.last_button_frame = 0

    self.packer = CANPacker(dbc_name)
    self.params = CarControllerParams(CP)

    # long
    self.last_das_3_counter = -1
    self.accel_steady = 0
    self.last_brake = None
    self.vehicleMass = CP.mass
    self.max_gear = None

  def update(self, CC, CS):
    can_sends = []

    # TODO: can we make this more sane? why is it different for all the cars?
    lkas_control_bit = self.lkas_control_bit_prev
    if CS.out.vEgo > self.CP.minSteerSpeed:
      lkas_control_bit = True
    elif self.CP.carFingerprint in (CAR.PACIFICA_2019_HYBRID, CAR.PACIFICA_2020, CAR.JEEP_CHEROKEE_2019):
      if CS.out.vEgo < (self.CP.minSteerSpeed - 3.0):
        lkas_control_bit = False
    elif self.CP.carFingerprint in RAM_CARS:
      if CS.out.vEgo < (self.CP.minSteerSpeed - 0.5):
        lkas_control_bit = False

    # EPS faults if LKAS re-enables too quickly
    lkas_control_bit = lkas_control_bit and (self.frame - self.last_lkas_falling_edge > 200)
    lkas_active = CC.latActive and self.lkas_control_bit_prev

    # *** control msgs ***
    # cruise buttons
    if (self.frame - self.last_button_frame)*DT_CTRL > 0.05:
      das_bus = 2 if self.CP.carFingerprint in RAM_CARS else 0

      # ACC cancellation
      if CC.cruiseControl.cancel:
        self.last_button_frame = self.frame
        can_sends.append(create_cruise_buttons(self.packer, CS.button_counter + 1, das_bus, cancel=True))

      # ACC resume from standstill
      elif CC.cruiseControl.resume:
        self.last_button_frame = self.frame
        can_sends.append(create_cruise_buttons(self.packer, CS.button_counter + 1, das_bus, resume=True))

    # HUD alerts
    if self.frame % 25 == 0:
      if CS.lkas_car_model != -1:
        can_sends.append(create_lkas_hud(self.packer, self.CP, lkas_active, CC.hudControl.visualAlert, self.hud_count, CS.lkas_car_model, CS.auto_high_beam))
        self.hud_count += 1

    # steering
    if self.frame % 2 == 0:
      # steer torque
      new_steer = int(round(CC.actuators.steer * self.params.STEER_MAX))
      apply_steer = apply_toyota_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorqueEps, self.params)
      if not lkas_active:
        apply_steer = 0
      self.apply_steer_last = apply_steer

      idx = self.frame // 2
      can_sends.append(create_lkas_command(self.packer, self.CP, int(apply_steer), lkas_control_bit, idx))

    #LONG
      das_3_counter = CS.das_3['COUNTER']

      if not CC.enabled:
        self.last_brake = None

      max_gear = 8
      if CC.actuators.accel <= 0:
        go_req = False
        stop_req = False
        torque = None
        brake = self.acc_brake(CC.actuators.accel)
      else:
        self.last_brake = None
        go_req = False
        stop_req = False
        torque = (self.vehicleMass * CC.actuators.accel * CS.out.vEgo) / (.105 *  CS.gasRpm)
        # torque = (self.vehicleMass * CC.actuators.accel * CC.actuators.accel) / (.105 *  CS.gasRpm)
        # torque += CS.enginetorque
        torque = max(CS.torqMin + 1, min(CS.torqMax, torque)) # limits
        brake = None

      can_sends.append(acc_command(self.packer, das_3_counter, CC.enabled,
                                   go_req,
                                   torque,
                                   max_gear,
                                   stop_req,
                                   brake,
                                   CS.das_3))

    self.frame += 1
    if not lkas_control_bit and self.lkas_control_bit_prev:
      self.last_lkas_falling_edge = self.frame
    self.lkas_control_bit_prev = lkas_control_bit

    new_actuators = CC.actuators.copy()
    new_actuators.steer = self.apply_steer_last / self.params.STEER_MAX

    return new_actuators, can_sends

  def acc_brake(self, aTarget):
    brake_target = max(ACCEL_MIN, round(aTarget, 2))
    if self.last_brake is None:
      self.last_brake = min(0., brake_target / 2)
    else:
      tBrake = brake_target
      lBrake = self.last_brake
      if tBrake < lBrake:
        diff = min(BRAKE_CHANGE, (lBrake - tBrake) / 2)
        self.last_brake = max(lBrake - diff, tBrake)
      elif tBrake - lBrake > 0.01:  # don't let up unless it's a big enough jump
        diff = min(BRAKE_CHANGE, (tBrake - lBrake) / 2)
        self.last_brake = min(lBrake + diff, tBrake)
    return self.last_brake