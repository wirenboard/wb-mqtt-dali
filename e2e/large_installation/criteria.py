# Pure checks of the criteria: device state as wb-mqtt-dali reports it against the profile, and the
# journal records that count as violations.

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

from e2e.large_installation.installation_profile import ExpectedDevice, GearSpec
from e2e.large_installation.log_records import LogLevel, LogRecord, message_class


@dataclass(frozen=True)
class ReportedDevice:
    """A device of `Editor/GetList` with the groups wb-mqtt-dali read from it at startup."""

    name: str
    groups: frozenset[int]


def log_violations(records: list[LogRecord]) -> list[LogRecord]:
    """Criterion 2: every WARNING and worse."""
    return [r for r in records if r.level >= LogLevel.WARNING]


class ViolationClass(NamedTuple):
    identifier: str
    message_class: str
    count: int


def violation_classes(records: list[LogRecord]) -> list[ViolationClass]:
    """Records grouped by service and normalised message class, the most frequent first."""
    counts = Counter((r.identifier, message_class(r)) for r in records)
    return [ViolationClass(identifier, cls, count) for (identifier, cls), count in counts.most_common()]


def bus_state_violations(  # pylint: disable=too-many-arguments, too-many-positional-arguments
    number: int,
    reported: list[ReportedDevice],
    bus_groups: set[int],
    gear: tuple[GearSpec, ...],
    configured_groups: set[int],
    devices: list[ExpectedDevice],
) -> list[str]:
    """Criterion 1 for one bus: groups of each gear, and the bus group devices."""
    by_name = {d.name: d for d in devices if d.bus == number}
    fake_groups = {g.address: g.groups for g in gear}
    violations = []
    for device in reported:
        expected = by_name.get(device.name)
        if expected is None:
            violations.append(f"bus {number}: unexpected device {device.name!r}")
        elif expected.dali2:
            continue
        elif expected.short not in fake_groups:
            violations.append(f"bus {number} gear {expected.short}: no such fake")
        elif device.groups != fake_groups[expected.short]:
            violations.append(
                f"bus {number} gear {expected.short}: groups {sorted(device.groups)}, "
                f"fake {sorted(fake_groups[expected.short])}"
            )
    expected_groups = set().union(*fake_groups.values()) | configured_groups
    if bus_groups != expected_groups:
        violations.append(
            f"bus {number}: group devices {sorted(bus_groups)}, expected {sorted(expected_groups)}"
        )
    return violations
