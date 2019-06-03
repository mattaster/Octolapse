# coding=utf-8
##################################################################################
# Octolapse - A plugin for OctoPrint used for making stabilized timelapse videos.
# Copyright (C) 2019  Brad Hochgesang
##################################################################################
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see the following:
# https://github.com/FormerLurker/Octolapse/blob/master/LICENSE
#
# You can contact the author either through the git-hub repository, or at the
# following email address: FormerLurker@pm.me
##################################################################################
from __future__ import unicode_literals
from threading import Thread
from six.moves import queue
from octoprint_octolapse.gcode import SnapshotPlan, SnapshotGcodeGenerator
from octoprint_octolapse.settings import PrinterProfile, TriggerProfile, StabilizationProfile
import GcodePositionProcessor

# create the module level logger
from octoprint_octolapse.log import LoggingConfigurator
logging_configurator = LoggingConfigurator()
logger = logging_configurator.get_logger(__name__)


class StabilizationPreprocessingThread(Thread):

    def __init__(
        self,
        timelapse_settings,
        progress_callback,
        start_callback,
        complete_callback,
        cancel_event,
        parsed_command,
        notification_period_seconds=1
    ):

        super(StabilizationPreprocessingThread, self).__init__()
        printer = timelapse_settings["settings"].profiles.current_printer()
        stabilization = timelapse_settings["settings"].profiles.current_stabilization()
        trigger = timelapse_settings["settings"].profiles.current_trigger()
        assert (isinstance(printer, PrinterProfile))
        assert (isinstance(stabilization, StabilizationProfile))
        assert (isinstance(trigger, TriggerProfile))
        assert (
            trigger.trigger_type in TriggerProfile.get_precalculated_trigger_types()
        )
        self.gcode_generator = SnapshotGcodeGenerator(timelapse_settings["settings"], timelapse_settings["octoprint_printer_profile"])
        self.progress_callback = progress_callback
        self.start_callback = start_callback
        self.complete_callback = complete_callback
        self.timelapse_settings = timelapse_settings
        self.daemon = True
        self.parsed_command = parsed_command
        self.cancel_event = cancel_event
        self.is_cancelled = False
        # make sure the event is set to start with
        if not self.cancel_event.is_set():
            self.cancel_event.set()

        self.notification_period_seconds = notification_period_seconds
        self.snapshot_plans = []
        self.printer_profile = printer
        self.stabilization_profile = stabilization
        self.trigger_profile = trigger
        self.error = None
        self.total_seconds = 0
        self.gcodes_processed = 0
        self.lines_processed = 0
        self.cpp_position_args = printer.get_position_args(timelapse_settings["g90_influences_extruder"])

    def run(self):
        try:
            # perform the start callback
            self.start_callback()
            ret_val, options = self._run_stabilization()
            logger.info(
                "Received %s snapshot plans from the GcodePositionProcessor stabilization in %s seconds.",
                len(ret_val[2]), ret_val[3]
            )
            results = (
                ret_val[0],  # success
                ret_val[1],  # errors
                ret_val[2],  # snapshot_plans
                ret_val[3],  # seconds_elapsed
                ret_val[4],  # gcodes processed
                ret_val[5],  # lines_processed
                options,
            )

        except Exception as e:
            logger.exception("Gcode preprocessing failed.")
            self.error = "There was a problem preprocessing your gcode file.  See plugin_octolapse.log for details"
            results = (
                False,  # success
                self.error,
                False,
                [],
                0,
                0,
                0
            )
        logger.info("Unpacking results")
        success = results[0]
        errors = results[1]
        # get snapshot plans
        cpp_snapshot_plans = results[2]
        if not cpp_snapshot_plans:
            snapshot_plans = []
            success = False
        else:
            snapshot_plans = SnapshotPlan.create_from_cpp_snapshot_plans(cpp_snapshot_plans)
        seconds_elapsed = results[3]
        gcodes_processed = results[4]
        lines_processed = results[5]
        self.complete_callback(
            success, errors, self.is_cancelled, snapshot_plans, seconds_elapsed, gcodes_processed, lines_processed,
            self.timelapse_settings, self.parsed_command
        )

    def _create_stabilization_args(self):
        # and vase mode is enabled, use the layer height setting if it exists
        # I'm keeping this out of the c++ routine so it can be more easily changed based on slicer
        # changes.  I might add this to the settings class.
        if self.trigger_profile.layer_trigger_height == 0:
            if (
                self.printer_profile.gcode_generation_settings.vase_mode and
                self.printer_profile.gcode_generation_settings.layer_height
            ):
                height_increment = self.printer_profile.gcode_generation_settings.layer_height
            else:
                # use the default height increment
                height_increment = self.printer_profile.minimum_layer_height
        else:
            height_increment = self.trigger_profile.layer_trigger_height

        stabilization_args = {
            'height_increment': height_increment,
            'notification_period_seconds': self.notification_period_seconds,
            'on_progress_received': self.on_progress_received,
            'file_path': self.timelapse_settings["gcode_file_path"],
            'gcode_generator': self.gcode_generator,
            "x_stabilization_disabled": (
                self.stabilization_profile.x_type == StabilizationProfile.STABILIZATION_AXIS_TYPE_DISABLED
            ),
            "y_stabilization_disabled": (
                self.stabilization_profile.y_type == StabilizationProfile.STABILIZATION_AXIS_TYPE_DISABLED
            )
        }
        return stabilization_args

    def _run_stabilization(self):
        options = {}
        stabilization_args = self._create_stabilization_args()
        # if this is a vase mode print, set the minimum layer height to the
        # height increment so we can get better layer change detection for vase mode
        if self.printer_profile.gcode_generation_settings.vase_mode:
            self.cpp_position_args["minimum_layer_height"] = stabilization_args["height_increment"]
        trigger_type = self.trigger_profile.trigger_type
        is_precalculated = (
            self.trigger_profile.trigger_type in TriggerProfile.get_precalculated_trigger_types()
        )
        # If the current trigger is not precalculated, return an error
        if not is_precalculated:
            self.error = "The current trigger is not a pre-calculated trigger."
            results = (
                False,  # success
                self.error,  # errors
                [],  # snapshot_plans
                0,  # seconds_elapsed
                0,  # gcodes_processed
                0  # lines_processed
            )
        elif trigger_type == TriggerProfile.TRIGGER_TYPE_SNAP_TO_PRINT:
            options = {
                'disable_z_lift': self.trigger_profile.snap_to_print_disable_z_lift
            }

            # run lock_to_print stabilization
            results = GcodePositionProcessor.GetSnapshotPlans_SnapToPrint(
                self.cpp_position_args,
                stabilization_args
            )
        elif trigger_type == TriggerProfile.TRIGGER_TYPE_SMART_LAYER:
            # run smart layer trigger
            smart_layer_args = {
                'trigger_type': int(self.trigger_profile.smart_layer_trigger_type),
                'distance_threshold_percent': (
                    self.trigger_profile.smart_layer_trigger_distance_threshold_percent / 100.0
                ),
                'speed_threshold': self.trigger_profile.smart_layer_trigger_speed_threshold,
                'snap_to_print': self.trigger_profile.smart_layer_snap_to_print
            }
            results = GcodePositionProcessor.GetSnapshotPlans_SmartLayer(
                self.cpp_position_args,
                stabilization_args,
                smart_layer_args
            )
        else:
            self.error = "Can't find a preprocessor named {0}, unable to preprocess gcode.".format(
                self.trigger_profile.trigger_type)
            results = (
                False,  # success
                self.error,  # errors
                [],  # snapshot_plans
                0,  # seconds_elapsed
                0,  # gcodes_processed
                0  # lines_processed
            )
        logger.info("Stabilization results received, returning.")
        return results, options

    def on_progress_received(self, percent_progress, seconds_elapsed, seconds_to_complete, gcodes_processed,
                             lines_processed):
        try:
            # Block if the we are finished processing to ensure the mail thread is always informed
            self.progress_callback(
                percent_progress, seconds_elapsed, seconds_to_complete, gcodes_processed, lines_processed
            )
        except queue.Full:
            pass

        if not self.cancel_event.is_set():
            self.is_cancelled = True
            self.cancel_event.set()

        # return true to continue processing
        return not self.is_cancelled




