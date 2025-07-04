#!/usr/bin/python
# -*- coding: utf-8 -*-
import math
import os
import signal
import sys
from datetime import datetime
from time import sleep
from typing import Union

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib as gobject

from battery import Battery
from dbushelper import DbusHelper
from utils import (
    BATTERY_ADDRESSES,
    BMS_TYPE,
    bytearray_to_string,
    DRIVER_VERSION,
    EXCLUDED_DEVICES,
    EXTERNAL_SENSOR_DBUS_DEVICE,
    EXTERNAL_SENSOR_DBUS_PATH_CURRENT,
    EXTERNAL_SENSOR_DBUS_PATH_SOC,
    get_venus_os_version,
    get_venus_os_image_type,
    get_venus_os_device_type,
    logger,
    POLL_INTERVAL,
    validate_config_values,
)

# import battery classes
# TODO: import only the classes that are needed
from bms.daly import Daly
from bms.daren_485 import Daren485
from bms.ecs import Ecs
from bms.eg4_lifepower import EG4_Lifepower
from bms.eg4_ll import EG4_LL
from bms.felicity import Felicity
from bms.heltecmodbus import HeltecModbus
from bms.hlpdatabms4s import HLPdataBMS4S
from bms.jkbms import Jkbms
from bms.jkbms_pb import Jkbms_pb
from bms.ks48100 import KS48100
from bms.lltjbd import LltJbd
from bms.pace import Pace
from bms.renogy import Renogy
from bms.seplos import Seplos
from bms.seplosv3 import Seplosv3

# add ext folder to sys.path
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext"))

# enabled only if explicitly set in config under "BMS_TYPE"
if "ANT" in BMS_TYPE:
    from bms.ant import ANT
if "MNB" in BMS_TYPE:
    from bms.mnb import MNB
if "Sinowealth" in BMS_TYPE:
    from bms.sinowealth import Sinowealth

supported_bms_types = [
    {"bms": Daly, "baud": 9600, "address": b"\x40"},
    {"bms": Daly, "baud": 9600, "address": b"\x80"},
    {"bms": Daren485, "baud": 9600, "address": b"\x01"},
    {"bms": Daren485, "baud": 19200, "address": b"\x01"},
    {"bms": Ecs, "baud": 19200},
    {"bms": EG4_Lifepower, "baud": 9600, "address": b"\x01"},
    {"bms": EG4_LL, "baud": 9600, "address": b"\x01"},
    {"bms": Felicity, "baud": 9600, "address": b"\x01"},
    {"bms": HeltecModbus, "baud": 9600, "address": b"\x01"},
    {"bms": HLPdataBMS4S, "baud": 9600},
    {"bms": Jkbms, "baud": 115200},
    {"bms": Jkbms_pb, "baud": 115200, "address": b"\x01"},
    {"bms": KS48100, "baud": 9600, "address": b"\x01"},
    {"bms": LltJbd, "baud": 9600, "address": b"\x00"},
    {"bms": Pace, "baud": 9600, "address": b"\x00"},
    {"bms": Renogy, "baud": 9600, "address": b"\x30"},
    {"bms": Renogy, "baud": 9600, "address": b"\xf7"},
    {"bms": Seplos, "baud": 19200, "address": b"\x00"},
    {"bms": Seplosv3, "baud": 19200},
]

# enabled only if explicitly set in config under "BMS_TYPE"
if "ANT" in BMS_TYPE:
    supported_bms_types.append({"bms": ANT, "baud": 19200})
if "MNB" in BMS_TYPE:
    supported_bms_types.append({"bms": MNB, "baud": 9600})
if "Sinowealth" in BMS_TYPE:
    supported_bms_types.append({"bms": Sinowealth, "baud": 9600})

expected_bms_types = [battery_type for battery_type in supported_bms_types if battery_type["bms"].__name__ in BMS_TYPE or len(BMS_TYPE) == 0]

logger.info("")
logger.info("Starting dbus-serialbattery")


# count loops
count_for_loops = 5
delayed_loop_count = 0


def main():
    global expected_bms_types, supported_bms_types

    def exit_driver(sig, frame, code: int = 0) -> None:
        """
        Gracefully exit the driver.
        Handles also signal for SIGINT and SIGTERM.

        :return: None
        """
        logger.info("Exit signal received, exiting gracefully...")

        port = get_port()

        # Stop the main loop, if set
        if "mainloop" in globals() and mainloop is not None:
            mainloop.quit()

        # For BLE connections, disconnect from the BLE device
        if port.endswith("_Ble"):
            if battery and len(battery) > 0 and hasattr(battery[0], "disconnect") and callable(battery[0].disconnect):
                battery[0].disconnect()

        # Stop the CanReceiverThread
        elif port.startswith(("can", "vecan", "vcan")):
            if "can_thread" in globals() and can_thread is not None:
                can_thread.stop()

        # Close the serial connection
        else:
            # Currently not feasible to close the serial connection
            # TODO: Is it worth implementing this?
            pass

        logger.info(f"Stopped dbus-serialbattery with exit code {code}")
        sys.exit(code)

    # Register the signal handler
    signal.signal(signal.SIGINT, exit_driver)
    signal.signal(signal.SIGTERM, exit_driver)

    def poll_battery(loop) -> bool:
        """
        Polls the battery for data and updates it on the dbus.
        Calls `publish_battery` from DbusHelper for each battery instance which
        then calls `refresh_data` from the battery instance to update the data.

        :param loop: The main event loop
        :return: Always returns True
        """
        global delayed_loop_count

        # count execution time in milliseconds
        start = datetime.now()

        for key_address in battery:
            helper[key_address].publish_battery(loop)

        runtime = (datetime.now() - start).total_seconds()
        logger.debug(f"Polling data took {runtime:.3f} seconds")

        # check if polling took too long and adjust poll interval, but only after 5 loops
        # since the first polls are always slower
        if runtime > battery[first_key].poll_interval / 1000:
            delayed_loop_count += 1
            if delayed_loop_count > 1:
                logger.warning(f"Polling data took {runtime:.3f} seconds. Automatically increase interval in {count_for_loops - delayed_loop_count} cycles.")
        else:
            delayed_loop_count = 0

        if delayed_loop_count >= count_for_loops:
            # round up to the next half second
            new_poll_interval = math.ceil((runtime + 0.05) * 2) / 2 * 1000

            # limit max poll interval to 60 seconds
            if new_poll_interval > 60000:
                new_poll_interval = 60000

            battery[first_key].poll_interval = new_poll_interval
            logger.warning(f"Polling took too long for the last {count_for_loops} cycles. Set to {new_poll_interval/1000:.3f} s")

            delayed_loop_count = 0

        return True

    def get_battery(_port: str, _bus_address: hex = None, can_transport_interface: object = None) -> Union[Battery, None]:
        """
        Attempts to establish a connection to the battery and returns the battery object if successful.

        :param _port: The port to connect to.
        :param _bus_address: The Modbus/CAN address to connect to (optional).
        :return: The battery object if a connection is established, otherwise None.
        """
        # Try to establish communications with the battery 3 times, else exit
        retry = 1
        retries = 3
        while retry <= retries:
            if retry > 1:
                logger.info("")

            logger.info("-- Testing BMS: " + str(retry) + " of " + str(retries) + " rounds")
            # Create a new battery object that can read the battery and run connection test
            for test in expected_bms_types:
                # noinspection PyBroadException
                try:
                    if _bus_address is not None:
                        # Convert hex string to bytes
                        _bms_address = bytes.fromhex(_bus_address.replace("0x", ""))
                    elif "address" in test:
                        _bms_address = test["address"]
                    else:
                        _bms_address = None

                    logger.info(
                        "  Testing "
                        + test["bms"].__name__
                        + (' at address "' + bytearray_to_string(_bms_address) + '"' if _bms_address is not None else "")
                        + (" with " + str(test["baud"]) + " baud" if "baud" in test else "")
                    )
                    batteryClass = test["bms"]
                    baud = test["baud"] if "baud" in test else None
                    battery: Battery = batteryClass(port=_port, baud=baud, address=_bms_address)
                    battery.set_can_transport_interface(can_transport_interface)
                    if battery.test_connection() and battery.validate_data():
                        logger.info("-- Connection established to " + battery.__class__.__name__)
                        return battery
                except KeyboardInterrupt:
                    return None
                except Exception:
                    (
                        exception_type,
                        exception_object,
                        exception_traceback,
                    ) = sys.exc_info()
                    file = exception_traceback.tb_frame.f_code.co_filename
                    line = exception_traceback.tb_lineno
                    logger.error("Non blocking exception occurred: " + f"{repr(exception_object)} of type {exception_type} in {file} line #{line}")
                    # Ignore any malfunction test_function()
                    pass
            retry += 1
            sleep(0.5)

        return None

    def get_port() -> str:
        """
        Retrieves the port to connect to from the command line arguments.

        :return: The port to connect to.
        """
        if len(sys.argv) > 1:
            port = sys.argv[1]
            if port not in EXCLUDED_DEVICES:
                return port
            else:
                logger.debug("Stopping dbus-serialbattery: " + str(port) + " is excluded through the config file")
                sleep(60)
                # Exit with error so that the serialstarter continues
                exit_driver(None, None, 1)
        elif "MNB" in BMS_TYPE:
            # Special case for MNB-SPI
            logger.info("No Port needed")
            return "/dev/ttyUSB9"
        else:
            logger.error(">>> No port specified in the command line arguments")
            sleep(60)
            exit_driver(None, None, 1)

    def check_bms_types(supported_bms_types, type) -> None:
        """
        Checks if BMS_TYPE is not empty and all specified BMS types are supported.

        :param supported_bms_types: List of supported BMS types.
        :param type: The type of BMS connection (ble, can, or serial).
        :return: None
        """
        # Get only BMS_TYPE that end with "_Ble"
        if type == "ble":
            bms_types = [type for type in BMS_TYPE if type.endswith("_Ble")]

        # Get only BMS_TYPE that end with "_Can"
        if type == "can":
            bms_types = [type for type in BMS_TYPE if type.endswith("_Can")]

        # Get only BMS_TYPE that do not end with "_Ble" or "_Can"
        if type == "serial":
            bms_types = [type for type in BMS_TYPE if not type.endswith("_Ble") and not type.endswith("_Can")]

        if len(bms_types) > 0:
            for bms_type in bms_types:
                if bms_type not in [bms["bms"].__name__ for bms in supported_bms_types]:
                    logger.error(
                        f'>>> BMS type "{bms_type}" is not supported. Supported BMS types are: '
                        + f"{', '.join([bms['bms'].__name__ for bms in supported_bms_types])}"
                        + "; Disabled by default: ANT, MNB, Sinowealth"
                    )
                    exit_driver(None, None, 1)

    # show Venus OS version and device type
    logger.info("Venus OS " + get_venus_os_version() + " (" + get_venus_os_image_type() + ") running on " + get_venus_os_device_type())

    # show the version of the driver
    logger.info("dbus-serialbattery v" + str(DRIVER_VERSION))

    port = get_port()
    battery = {}

    # BLUETOOTH
    if port.endswith("_Ble"):
        """
        Import BLE classes only if it's a BLE port; otherwise, the driver won't start due to missing Python modules.
        This prevents issues when using the driver exclusively with a serial connection.
        """

        if len(sys.argv) <= 2:
            logger.error(">>> Bluetooth address is missing in the command line arguments")
            sleep(60)
            exit_driver(None, None, 1)
        else:
            ble_address = sys.argv[2]

            if port == "Jkbms_Ble":
                # noqa: F401 --> ignore flake "imported but unused" error
                from bms.jkbms_ble import Jkbms_Ble  # noqa: F401

            elif port == "Kilovault_Ble":
                # noqa: F401 --> ignore flake "imported but unused" error
                from bms.kilovault_ble import Kilovault_Ble  # noqa: F401

            elif port == "LiTime_Ble":
                # noqa: F401 --> ignore flake "imported but unused" error
                from bms.litime_ble import LiTime_Ble  # noqa: F401

            elif port == "LltJbd_Ble":
                # noqa: F401 --> ignore flake "imported but unused" error
                from bms.lltjbd_ble import LltJbd_Ble  # noqa: F401

            else:
                logger.error(">>> Unknown Bluetooth BMS type: " + port)
                logger.error("Supported Bluetooth BMS types (CASE SENSITIVE!): Jkbms_Ble, Kilovault_Ble, LiTime_Ble, LltJbd_Ble")
                sleep(60)
                exit_driver(None, None, 1)

            class_ = eval(port)

            # do not remove ble_ prefix, since the dbus service cannot be only numbers
            testbms = class_("ble_" + ble_address.replace(":", "").lower(), 9600, ble_address)

            if testbms.test_connection():
                logger.info("-- Connection established to " + testbms.__class__.__name__)
                battery[0] = testbms

    # CAN
    elif port.startswith(("can", "vecan", "vcan")):
        """
        Import CAN classes only if it's a CAN port; otherwise, the driver won't start due to missing Python modules.
        This prevents issues when using the driver exclusively with a serial connection.

        can: Older GX devices and Raspberry Pi with CAN hat
        vecan: Newer Venus GX devices
        vcan: Virtual CAN interface for testing
        """
        from bms.daly_can import Daly_Can
        from bms.jkbms_can import Jkbms_Can
        from bms.rv_c_can import RV_C_Can
        from bms.ubms_can import Ubms_Can

        # only try CAN BMS on CAN port
        supported_bms_types = [
            {"bms": Daly_Can},
            {"bms": Jkbms_Can},
            {"bms": RV_C_Can},
            {"bms": Ubms_Can},
        ]

        # check if BMS_TYPE is not empty and all BMS types in the list are supported
        check_bms_types(supported_bms_types, "can")

        expected_bms_types = [battery_type for battery_type in supported_bms_types if battery_type["bms"].__name__ in BMS_TYPE or len(BMS_TYPE) == 0]

        # If no BMS type is supported, use all supported BMS types
        if len(expected_bms_types) == 0:
            logger.warning(f"No supported CAN BMS type found in BMS_TYPE: {', '.join(BMS_TYPE)}. Using all supported BMS types.")
            expected_bms_types = supported_bms_types

        # start the corresponding CanReceiverThread if BMS for this type found
        from utils_can import CanReceiverThread, CanTransportInterface

        try:
            can_thread = CanReceiverThread.get_instance(bustype="socketcan", channel=port)
        except Exception as e:
            logger.error(f"Error while accessing CAN interface: {e}")
            sleep(60)
            exit_driver(None, None, 1)

        # wait until thread has initialized
        if not can_thread.can_initialised.wait(2):
            logger.error("Timeout while accessing CAN interface")
            sleep(60)
            exit_driver(None, None, 1)

        can_transport_interface = CanTransportInterface()
        can_transport_interface.can_message_cache_callback = can_thread.get_message_cache
        can_transport_interface.can_bus = can_thread.can_bus
        logger.debug("Wait shortly to make sure that all needed data is in the cache")
        # Slowest message cycle transmission is every 1 second, wait a bit more for the first time to fetch all needed data (only jk bms)
        sleep(2)
        addresses = [None] if len(BATTERY_ADDRESSES) == 0 else BATTERY_ADDRESSES  # use default address, if not configured

        for busspeed in [250, 500]:
            for address in addresses:
                bat = get_battery(port, address, can_transport_interface)
                if bat:
                    battery[address] = bat
                    logger.info(f"Successful battery connection at {port} and this address {str(address)}")
                else:
                    logger.warning(f"No battery connection at {port} and this address {str(address)}")

            # if we've found at least 1 battery, stop the search here. otherwise retry with other bus speeds
            if len(battery) > 0:
                break

            logger.info(f"Found no devices on can bus, retrying with {busspeed} kbps")
            can_thread.setup_can(channel=port, bitrate=busspeed, force=True)
            sleep(2)

    # SERIAL
    else:
        # check if BMS_TYPE is not empty and all BMS types in the list are supported
        check_bms_types(supported_bms_types, "serial")

        # wait some seconds to be sure that the serial connection is ready
        # else the error throw a lot of timeouts
        sleep(16)

        # Check if BATTERY_ADDRESSES is not empty
        if BATTERY_ADDRESSES:
            for address in BATTERY_ADDRESSES:
                found_battery = get_battery(port, address)
                if found_battery:
                    battery[address] = found_battery
                    logger.info(f"Successful battery connection at {port} and this address {address}")
                else:
                    logger.warning(f"No battery connection at {port} and this address {address}")
        # Use default address
        else:
            battery[0] = get_battery(port)

    # check if at least one BMS was found
    battery_found = False

    for key_address in battery:
        if battery[key_address] is not None:
            battery_found = True

    if not battery_found:
        logger.error(
            f">>> No battery connection at {port}"
            + (" and this bus addresses: " + ", ".join(BATTERY_ADDRESSES) if BATTERY_ADDRESSES else "")
            + (f" {ble_address}" if port.endswith("_Ble") else "")
        )
        exit_driver(None, None, 1)

    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)
    if sys.version_info.major == 2:
        gobject.threads_init()
    mainloop = gobject.MainLoop()

    # Get the initial values for the battery used by setup_vedbus
    helper = {}

    for key_address in battery:
        helper[key_address] = DbusHelper(battery[key_address], key_address)
        if not helper[key_address].setup_vedbus():
            logger.error(
                f">>> Problem with battery set up at {port}"
                + (" and this bus address: " + ", ".join(BATTERY_ADDRESSES) if BATTERY_ADDRESSES else "")
                + (f" {ble_address}" if port.endswith("_Ble") else "")
            )
            exit_driver(None, None, 1)

        # Calculate the initial values for the battery
        battery[key_address].set_calculated_data()

    # get first key from battery dict
    first_key = list(battery.keys())[0]

    # try using active callback on this battery (normally only used for Bluetooth BMS)
    if not battery[first_key].use_callback(lambda: poll_battery(mainloop)):
        # change poll interval if set in config
        if POLL_INTERVAL is not None:
            battery[first_key].poll_interval = POLL_INTERVAL

        logger.info(f"Polling interval: {battery[first_key].poll_interval/1000:.3f} s")

        # if not possible, poll the battery every poll_interval milliseconds
        gobject.timeout_add(
            battery[first_key].poll_interval,
            lambda: poll_battery(mainloop),
        )
    else:
        logger.info("Polling interval: active callback used")

    # print log at this point, else not all data is correctly populated
    for key_address in battery:
        battery[key_address].log_settings()

    # check config, if there are any invalid values trigger "settings incorrect" error
    # and set the battery in error state to prevent chargin/discharging
    if not validate_config_values():
        for key_address in battery:
            battery[key_address].state = 10
            battery[key_address].error_code = 119

    # check, if external current sensor should be used
    if EXTERNAL_SENSOR_DBUS_DEVICE is not None and (EXTERNAL_SENSOR_DBUS_PATH_CURRENT is not None or EXTERNAL_SENSOR_DBUS_PATH_SOC is not None):
        for key_address in battery:
            battery[key_address].setup_external_sensor()

    # Run the main loop
    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
