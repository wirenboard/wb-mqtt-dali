# Service journal records: parsing journalctl JSON, severity and normalisation into message classes.

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass

DALI_IDENTIFIER = "wb-mqtt-dali"
SERIAL_IDENTIFIER = "wb-mqtt-serial"


class LogLevel(enum.IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


PRIORITY_LEVELS = {
    0: LogLevel.CRITICAL,
    1: LogLevel.CRITICAL,
    2: LogLevel.CRITICAL,
    3: LogLevel.ERROR,
    4: LogLevel.WARNING,
    5: LogLevel.INFO,
    6: LogLevel.INFO,
    7: LogLevel.DEBUG,
}
LEVEL_NAMES = {"WARN": LogLevel.WARNING, **{level.name: level for level in LogLevel}}

# Python's BASIC_FORMAT; a logger name has no whitespace, unlike the text after "LEVEL: " of wb-mqtt-serial.
_PYTHON_PREFIX = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):([^\s:]*):(.*)$", re.DOTALL)
_PLAIN_PREFIX = re.compile(r"^(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\s*:\s*(.*)$", re.DOTALL)


@dataclass(frozen=True)
class LogRecord:
    identifier: str
    level: LogLevel
    logger: str
    message: str


def record_from_journal_json(line: str) -> LogRecord:
    entry = json.loads(line)
    message = entry.get("MESSAGE", "")
    if isinstance(message, list):
        message = bytes(message).decode("utf-8", errors="replace")
    priority = PRIORITY_LEVELS.get(int(entry.get("PRIORITY", 6)), LogLevel.INFO)
    return _split_message(
        str(entry.get("SYSLOG_IDENTIFIER", "")),
        str(message),
        priority,
        str(entry.get("LOGGER", "")),
    )


def _split_message(identifier: str, message: str, level: LogLevel, logger: str) -> LogRecord:
    python = _PYTHON_PREFIX.match(message)
    if python is not None:
        return LogRecord(identifier, LEVEL_NAMES[python.group(1)], python.group(2), python.group(3))
    plain = _PLAIN_PREFIX.match(message)
    if plain is not None:
        return LogRecord(identifier, LEVEL_NAMES[plain.group(1)], logger, plain.group(2))
    return LogRecord(identifier, level, logger, message)


_NORMALISERS: list[tuple[re.Pattern, str]] = [
    # wb-mqtt-dali command lists are Python lists of strings; "[serial device]"-style prefixes stay.
    (re.compile(r"\['.*?'\]"), "<cmds>"),
    (re.compile(r"(Failed to initialize device ).*?(: )"), r"\1<name>\2"),
    (re.compile(r"(No response to )\w+\((?:[^()]|\([^()]*\))*\)"), r"\1<cmd>"),
    (re.compile(r"'[^']*'"), "<str>"),
    (re.compile(r"/devices/\S+"), "<topic>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\d+"), "N"),
]


def message_class(record: LogRecord) -> str:
    text = record.message
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    return f"{record.level.name}:{text}"
