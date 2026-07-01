"""DT8 colour scene membership (``enabled``) per IEC 62386-209 §9.11.

A control gear is a member of a colour scene unless BOTH the scene level and the scene
colour type are MASK. So a scene with a colour but MASK level ("change colour, keep
brightness") stays enabled, and disabling must REMOVE FROM SCENE (both registers MASK),
not just mask the level (which would leave a still-member colour-only scene).
"""

from unittest.mock import AsyncMock

import pytest
from dali.address import GearShort
from dali.gear.general import RemoveFromScene, SetScene

from wb.mqtt_dali.dali_type8_parameters import ColourSettings, ColourType, SceneSettings
from wb.mqtt_dali.dali_type8_tc import Type8TcLimits
from wb.mqtt_dali.wbdali_utils import MASK


def _scene(number: int = 3) -> SceneSettings:
    return SceneSettings(number, ColourType.RGBWAF, Type8TcLimits())


def _read_returning(value: ColourSettings):
    driver = AsyncMock()
    driver.run_sequence = AsyncMock(return_value=value)
    return driver


@pytest.mark.asyncio
async def test_colour_only_scene_reads_enabled():
    """A scene with a stored colour but MASK level is a colour-only scene: it is still a
    member, so it reads back enabled, and the level stays MASK (the editor's "keep
    brightness" state) rather than being reset to 0."""
    colour_only = ColourSettings(ColourType.RGBWAF, MASK)  # colour_masked stays False
    colour_only.colour.red = 10
    res = await _scene().read(_read_returning(colour_only), GearShort(5))
    assert res["enabled"] is True
    assert res["level"] == MASK


@pytest.mark.asyncio
async def test_level_only_scene_reads_enabled():
    """A scene with a level but MASK colour type (colour_masked) is a member by its level."""
    level_only = ColourSettings(ColourType.RGBWAF, 100, colour_masked=True)
    res = await _scene().read(_read_returning(level_only), GearShort(5))
    assert res["enabled"] is True


@pytest.mark.asyncio
async def test_removed_scene_reads_disabled():
    """Both level and colour type MASK = not a member: reads back disabled."""
    removed = ColourSettings(ColourType.RGBWAF, MASK, colour_masked=True)
    res = await _scene().read(_read_returning(removed), GearShort(5))
    assert res["enabled"] is False


@pytest.mark.asyncio
async def test_disable_removes_from_scene(monkeypatch):
    """Disabling a scene issues REMOVE FROM SCENE (clears both registers), not a level=MASK
    write that would keep the colour, and reads back disabled."""
    sent = AsyncMock()
    monkeypatch.setattr("wb.mqtt_dali.dali_type8_parameters.send_commands_with_retry", sent)
    driver = _read_returning(ColourSettings(ColourType.RGBWAF, MASK, colour_masked=True))

    res = await _scene(3).write(
        driver, GearShort(5), {"enabled": False, "rgb": "1;2;3", "white": 4, "level": 100}
    )

    cmds = sent.await_args.args[1]
    assert any(isinstance(c, RemoveFromScene) for c in cmds)
    assert res["enabled"] is False


@pytest.mark.asyncio
async def test_enable_with_mask_level_stores_colour_only_scene(monkeypatch):
    """Enabling with a colour but MASK level stores a colour-only scene (SetScene, no
    REMOVE FROM SCENE) and reads back enabled — the reported bug where enabling + MASK level
    turned the switch back off."""
    sent = AsyncMock()
    monkeypatch.setattr("wb.mqtt_dali.dali_type8_parameters.send_commands_with_retry", sent)
    driver = _read_returning(ColourSettings(ColourType.RGBWAF, MASK))  # colour-only read-back

    res = await _scene(3).write(
        driver, GearShort(5), {"enabled": True, "rgb": "1;2;3", "white": 4, "level": MASK}
    )

    cmds = sent.await_args.args[1]
    assert any(isinstance(c, SetScene) for c in cmds)
    assert not any(isinstance(c, RemoveFromScene) for c in cmds)
    assert res["enabled"] is True
    assert res["level"] == MASK
