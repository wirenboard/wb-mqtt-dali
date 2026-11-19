import asyncio
from typing import Optional, Set

from dali.address import GearShort
from dali.gear.colour import Activate, QueryColourValue
from dali.gear.general import DAPC, Off, QueryActualLevel

from wb.mqtt_dali.common_dali_device import EVENT_RESYNC_BASE_INTERVAL
from wb.mqtt_dali.control_ids import ACTUAL_LEVEL, ON_OFF, SET_RGB, WANTED_LEVEL
from wb.mqtt_dali.on_off_control import (
    OffAction,
    OffActionMode,
    OnAction,
    OnActionMode,
    OnOffConfig,
    on_off_config_to_json,
)
from wb.mqtt_dali.wbdali_error_response import NoPowerOnBus, NoResponseFromGateway

from ._bus_doubles import (
    BUS_UID,
    SETTLE_S,
    FakeWBDALIDriver,
    control_error,
    control_errors,
    control_values,
    running_bus,
    settle_init,
    write,
)
from ._virtual_time import VirtualTimeTestCase

ADDR = GearShort(5)
OTHER_ADDR = GearShort(6)
GROUP = 3
LIGHT_ID = f"{BUS_UID}_5"
GROUP_ID = f"{BUS_UID}_group_{GROUP:02d}"
BROADCAST_ID = f"{BUS_UID}_broadcast"

DT8 = 8
# Every colour query answers this: the RGBWAF colour type, and then each component at that value.
RGBWAF_COLOUR_VALUE = 0x80

ON_OFF_CONFIG = OnOffConfig(
    on_action=OnAction(OnActionMode.DAPC, value=200), off_action=OffAction(OffActionMode.DAPC)
)

# Longer than the settle of a no-fade command plus the polling loop's idle ceiling.
NO_READ_WINDOW_S = 2.0
# Longer than the longest jittered interval of the regular level poll.
REGULAR_POLL_WINDOW_S = 2 * EVENT_RESYNC_BASE_INTERVAL


def _light(short: int, on_off: Optional[OnOffConfig] = None) -> dict:
    entry = {"short": short, "random": 0x123456 + short}
    if on_off is not None:
        entry["on_off"] = on_off_config_to_json(on_off)
    return entry


LIGHT = {"devices": [_light(5)]}
SWITCH = {"devices": [_light(5, ON_OFF_CONFIG)]}
# The driver puts both lights in GROUP.
TWO_LIGHTS = {"devices": [_light(5), _light(6)]}
GROUP_SWITCH = {
    "devices": [_light(5), _light(6)],
    "groups": [{"number": GROUP, "on_off": on_off_config_to_json(ON_OFF_CONFIG)}],
}


def _driver() -> FakeWBDALIDriver:
    return FakeWBDALIDriver(answers={"QueryActualLevel": 0}, groups={5: {GROUP}, 6: {GROUP}})


def _fail_next(driver: FakeWBDALIDriver, command_type: type, failure: type) -> None:
    """The next ``command_type`` command on ``driver`` fails with ``failure``, every retry of it too."""
    failures = [failure] * 3
    send = driver.send

    async def send_failing(cmd, source=None, priority=None, logger=None):
        if failures and isinstance(cmd, command_type):
            response = failures.pop()()
            driver.report(cmd, response, source)
            return response
        return await send(cmd, source, priority, logger)

    driver.send = send_failing


def _silenced(driver: FakeWBDALIDriver) -> Set[type]:
    """Queries of the types put in the returned set go unanswered on ``driver``."""
    silenced: Set[type] = set()
    send = driver.send

    async def send_unless_silenced(cmd, source=None, priority=None, logger=None):
        if type(cmd) in silenced:
            response = cmd.response(None)
            driver.report(cmd, response, source)
            return response
        return await send(cmd, source, priority, logger)

    driver.send = send_unless_silenced
    return silenced


def _level_reads(driver: FakeWBDALIDriver) -> int:
    return sum(isinstance(command, QueryActualLevel) for command in driver.sent)


class FailedCommandFeedbackTest(VirtualTimeTestCase):  # pylint: disable=too-many-public-methods

    async def test_failed_write_sets_write_error_and_republishes_the_level(self):
        """A level write the gateway fails, unanswered or with no power on the bus: ``w``, the
        unchanged ``wanted_level = 0`` goes out again, and nothing is read."""
        for failure in (NoResponseFromGateway, NoPowerOnBus):
            with self.subTest(failure=failure):
                driver = _driver()
                async with running_bus(LIGHT, driver) as (_bus, client):
                    await settle_init(client, driver)
                    _fail_next(driver, DAPC, failure)

                    write(client, LIGHT_ID, WANTED_LEVEL, "100")
                    await asyncio.sleep(NO_READ_WINDOW_S)

                    self.assertEqual(control_error(client, LIGHT_ID, WANTED_LEVEL), "w")
                    self.assertEqual(control_values(client, LIGHT_ID, WANTED_LEVEL), ["0"])
                    self.assertEqual(_level_reads(driver), 0)

    async def test_poll_finds_the_command_did_arrive(self):
        """The failed write did reach the light: the poll publishes the new levels and clears ``w``."""
        driver = _driver()
        async with running_bus(LIGHT, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoResponseFromGateway)
            driver.answers["QueryActualLevel"] = 254

            write(client, LIGHT_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(SETTLE_S)
            self.assertEqual(control_error(client, LIGHT_ID, WANTED_LEVEL), "w")
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)

            self.assertIsNone(control_error(client, LIGHT_ID, WANTED_LEVEL))
            self.assertEqual(control_values(client, LIGHT_ID, WANTED_LEVEL), ["0", "100"])
            self.assertEqual(control_values(client, LIGHT_ID, ACTUAL_LEVEL), ["100.000"])

    async def test_failed_poll_keeps_the_write_error(self):
        """The poll after a failed write fails as well, on a light and a group: ``w`` holds until a
        poll answers, which clears it without publishing the value again."""
        for bus_data, device_id in ((LIGHT, LIGHT_ID), (TWO_LIGHTS, GROUP_ID)):
            with self.subTest(device_id=device_id):
                driver = _driver()
                silenced = _silenced(driver)
                async with running_bus(bus_data, driver) as (_bus, client):
                    await settle_init(client, driver)
                    _fail_next(driver, DAPC, NoResponseFromGateway)
                    silenced.add(QueryActualLevel)

                    write(client, device_id, WANTED_LEVEL, "100")
                    await asyncio.sleep(REGULAR_POLL_WINDOW_S)
                    self.assertEqual(control_error(client, device_id, ACTUAL_LEVEL), "r")
                    self.assertEqual(control_error(client, device_id, WANTED_LEVEL), "w")

                    silenced.clear()
                    await asyncio.sleep(REGULAR_POLL_WINDOW_S)

                    self.assertIsNone(control_error(client, device_id, WANTED_LEVEL))
                    self.assertEqual(control_values(client, device_id, WANTED_LEVEL), ["0"])

    async def test_new_command_after_a_failed_one_clears_the_write_error(self):
        """A successful write of 50 after a failed one: the prediction is published and ``w`` is
        cleared at once; its confirming read publishes nothing new."""
        driver = _driver()
        async with running_bus(LIGHT, driver) as (bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoResponseFromGateway)
            level_50 = bus.dali_devices[0].get_mqtt_control(WANTED_LEVEL).get_setup_commands(ADDR, "50")[0]
            driver.answers["QueryActualLevel"] = level_50.power

            write(client, LIGHT_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(SETTLE_S)
            self.assertEqual(control_error(client, LIGHT_ID, WANTED_LEVEL), "w")
            write(client, LIGHT_ID, WANTED_LEVEL, "50")
            await asyncio.sleep(SETTLE_S)

            # The level-to-percent curve turns the 50 into the nearest percent the gear can show.
            published = control_values(client, LIGHT_ID, WANTED_LEVEL)
            self.assertEqual(len(published), 2)
            self.assertIsNone(control_error(client, LIGHT_ID, WANTED_LEVEL))
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(control_values(client, LIGHT_ID, WANTED_LEVEL), published)

    async def test_failed_command_does_not_become_the_control_value(self):
        """Two failed writes, then a poll: the control keeps 0 and 100 is never published."""
        driver = _driver()
        async with running_bus(LIGHT, driver) as (bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoPowerOnBus)
            write(client, LIGHT_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(SETTLE_S)
            self.assertEqual(control_error(client, LIGHT_ID, WANTED_LEVEL), "w")

            _fail_next(driver, DAPC, NoResponseFromGateway)
            write(client, LIGHT_ID, WANTED_LEVEL, "80")
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)

            self.assertIsNone(control_error(client, LIGHT_ID, WANTED_LEVEL))
            self.assertEqual(control_values(client, LIGHT_ID, WANTED_LEVEL), ["0", "0"])
            control = bus.dali_devices[0].get_mqtt_control(WANTED_LEVEL)
            self.assertEqual(control.control_info.state.value, "0")

    async def test_group_settles_on_the_poll_of_its_leading_member(self):
        """A failed group write: ``w`` and the old ``wanted_level`` on the group, no member is read,
        and the regular poll the group takes clears ``w``."""
        driver = _driver()
        async with running_bus(TWO_LIGHTS, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoResponseFromGateway)

            write(client, GROUP_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(control_error(client, GROUP_ID, WANTED_LEVEL), "w")
            self.assertEqual(control_values(client, GROUP_ID, WANTED_LEVEL), ["0"])
            self.assertEqual(_level_reads(driver), 0)

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            self.assertIsNone(control_error(client, GROUP_ID, WANTED_LEVEL))
            self.assertEqual(control_values(client, GROUP_ID, WANTED_LEVEL), ["0"])

    async def test_group_write_error_cleared_by_the_next_group_command(self):
        """A failed group write, then one that goes through: ``w`` is cleared at once, and the
        group takes the new level from its leading member."""
        driver = _driver()
        async with running_bus(TWO_LIGHTS, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoResponseFromGateway)

            write(client, GROUP_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(SETTLE_S)
            self.assertEqual(control_error(client, GROUP_ID, WANTED_LEVEL), "w")
            write(client, GROUP_ID, WANTED_LEVEL, "50")
            await asyncio.sleep(SETTLE_S)

            self.assertIsNone(control_error(client, GROUP_ID, WANTED_LEVEL))
            # The old level, then the new one from the leading member, on the curve's nearest percent.
            self.assertEqual(len(control_values(client, GROUP_ID, WANTED_LEVEL)), 2)

    async def test_button_write_error_cleared_only_by_the_next_command(self):
        """A failed ``off``: ``w`` on the button with no press and no read; the regular poll keeps
        ``w``, the next successful ``off`` clears it."""
        driver = _driver()
        async with running_bus(LIGHT, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, Off, NoResponseFromGateway)

            write(client, LIGHT_ID, "off", "1")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(control_error(client, LIGHT_ID, "off"), "w")
            self.assertEqual(control_values(client, LIGHT_ID, "off"), [])
            self.assertEqual(_level_reads(driver), 0)

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            self.assertGreater(_level_reads(driver), 0)
            self.assertEqual(control_error(client, LIGHT_ID, "off"), "w")
            self.assertEqual(control_values(client, LIGHT_ID, "off"), [])

            write(client, LIGHT_ID, "off", "1")
            await asyncio.sleep(SETTLE_S)
            self.assertIsNone(control_error(client, LIGHT_ID, "off"))

    async def test_successful_command_publishes_the_prediction(self):
        """A write that goes through: the predicted level is published at once with no ``w``,
        and the level is read once the fade is over."""
        driver = _driver()
        async with running_bus(LIGHT, driver) as (_bus, client):
            await settle_init(client, driver)
            driver.answers["QueryActualLevel"] = 254

            write(client, LIGHT_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(SETTLE_S)

            self.assertEqual(control_values(client, LIGHT_ID, WANTED_LEVEL), ["100"])
            self.assertIsNone(control_error(client, LIGHT_ID, WANTED_LEVEL))
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertGreater(_level_reads(driver), 0)
            self.assertEqual(control_values(client, LIGHT_ID, WANTED_LEVEL), ["100"])
            self.assertEqual(control_errors(client, LIGHT_ID, WANTED_LEVEL), [])

    async def test_successful_button_press_is_not_repeated_by_the_poll(self):
        """An ``off`` that goes through, then a regular poll: no ``w``, and the press the write
        published is not published again."""
        driver = _driver()
        async with running_bus(LIGHT, driver) as (_bus, client):
            await settle_init(client, driver)

            write(client, LIGHT_ID, "off", "1")
            await asyncio.sleep(SETTLE_S)
            self.assertEqual(control_values(client, LIGHT_ID, "off"), ["1"])
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)

            self.assertGreater(_level_reads(driver), 0)
            self.assertEqual(control_values(client, LIGHT_ID, "off"), ["1"])
            self.assertEqual(control_errors(client, LIGHT_ID, "off"), [])

    async def test_group_button_write_error_cleared_only_by_the_next_command(self):
        """A failed group ``off``: members' regular polls keep ``w`` on the group button, the next
        successful group ``off`` clears it."""
        driver = _driver()
        async with running_bus(TWO_LIGHTS, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, Off, NoResponseFromGateway)

            write(client, GROUP_ID, "off", "1")
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            self.assertGreater(_level_reads(driver), 0)
            self.assertEqual(control_error(client, GROUP_ID, "off"), "w")
            self.assertEqual(control_values(client, GROUP_ID, "off"), [])

            write(client, GROUP_ID, "off", "1")
            await asyncio.sleep(SETTLE_S)
            self.assertIsNone(control_error(client, GROUP_ID, "off"))

    async def test_failed_colour_is_settled_by_the_colour_poll(self):
        """A failed ``set_rgb`` on DT8 gear: ``w`` and the old ``set_rgb`` go out, the colour is not
        read until the regular poll, which clears ``w``."""
        driver = _driver()
        driver.answers.update(QueryDeviceType=DT8, QueryColourValue=RGBWAF_COLOUR_VALUE)
        async with running_bus(LIGHT, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, Activate, NoResponseFromGateway)

            write(client, LIGHT_ID, SET_RGB, "255;0;0")
            await asyncio.sleep(NO_READ_WINDOW_S)
            old_colour = ";".join([str(RGBWAF_COLOUR_VALUE)] * 3)
            self.assertEqual(control_error(client, LIGHT_ID, SET_RGB), "w")
            self.assertEqual(control_values(client, LIGHT_ID, SET_RGB), [old_colour])
            self.assertFalse(any(isinstance(command, QueryColourValue) for command in driver.sent))

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            self.assertIsNone(control_error(client, LIGHT_ID, SET_RGB))
            self.assertEqual(control_values(client, LIGHT_ID, SET_RGB), [old_colour])

    async def test_failed_broadcast_is_cleared_by_the_next_command(self):
        """A failed broadcast write: ``w`` with no light read, and the next successful broadcast
        write clears ``w``."""
        driver = _driver()
        async with running_bus(TWO_LIGHTS, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoResponseFromGateway)

            write(client, BROADCAST_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(_level_reads(driver), 0)
            self.assertEqual(control_error(client, BROADCAST_ID, WANTED_LEVEL), "w")

            write(client, BROADCAST_ID, WANTED_LEVEL, "100")
            await asyncio.sleep(SETTLE_S)
            self.assertIsNone(control_error(client, BROADCAST_ID, WANTED_LEVEL))

    async def test_switch_read_error_survives_a_successful_command(self):
        """``on_off`` in ``r`` takes a write that goes through: ``r`` stays with no prediction,
        and the read that answers clears ``r``."""
        driver = _driver()
        silenced = _silenced(driver)
        silenced.add(QueryActualLevel)
        async with running_bus(SWITCH, driver) as (_bus, client):
            await settle_init(client, driver)

            write(client, LIGHT_ID, ON_OFF, "1")
            await asyncio.sleep(SETTLE_S)

            self.assertEqual(control_values(client, LIGHT_ID, ON_OFF), [])
            self.assertEqual(control_errors(client, LIGHT_ID, ON_OFF), [])

            silenced.clear()
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(control_errors(client, LIGHT_ID, ON_OFF), [None])

    async def test_group_switch_failed_write_settles_on_the_member_poll(self):
        """A failed group ``on_off`` write: ``w`` and the old ``on_off`` go out, no member is read
        until the regular poll, which clears ``w``."""
        driver = _driver()
        async with running_bus(GROUP_SWITCH, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoResponseFromGateway)

            write(client, GROUP_ID, ON_OFF, "1")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(control_error(client, GROUP_ID, ON_OFF), "w")
            self.assertEqual(control_values(client, GROUP_ID, ON_OFF), ["0"])
            self.assertEqual(_level_reads(driver), 0)

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            self.assertIsNone(control_error(client, GROUP_ID, ON_OFF))
            self.assertEqual(control_values(client, GROUP_ID, ON_OFF), ["0"])

    async def test_switch_read_error_gains_the_write_error(self):
        """``on_off`` in ``r`` takes a write that fails: ``rw``, and the last known ``on_off`` goes
        out again; a poll that answers clears both."""
        driver = _driver()
        silenced = _silenced(driver)
        silenced.add(QueryActualLevel)
        async with running_bus(SWITCH, driver) as (_bus, client):
            await settle_init(client, driver)
            _fail_next(driver, DAPC, NoPowerOnBus)

            write(client, LIGHT_ID, ON_OFF, "1")
            await asyncio.sleep(SETTLE_S)

            self.assertEqual(control_error(client, LIGHT_ID, ON_OFF), "rw")
            self.assertEqual(control_values(client, LIGHT_ID, ON_OFF), ["0"])

            silenced.clear()
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            self.assertIsNone(control_error(client, LIGHT_ID, ON_OFF))
