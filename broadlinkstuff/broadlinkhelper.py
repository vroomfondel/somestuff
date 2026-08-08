#!/usr/bin/env python3
"""Broadlink RM4: address devices, learn IR/RF, send codes, compute Coolix.

The devices come from ``broadlink.yaml`` (sample values, shipped with the
package), overlaid with ``broadlink.local.yaml`` from the current working
directory (gitignored; the module directory is searched as a fallback). With
none configured, ``discover`` searches by broadcast and prints a ready-made
block to paste into ``broadlink.local.yaml``.

Usage::

    python3 -m broadlinkstuff.broadlinkhelper devices                    # what is reachable
    python3 -m broadlinkstuff.broadlinkhelper sensors                    # temperature/humidity, all of them
    python3 -m broadlinkstuff.broadlinkhelper sensors Lounge --json      # one device, machine-readable
    python3 -m broadlinkstuff.broadlinkhelper discover                   # broadcast + config block
    python3 -m broadlinkstuff.broadlinkhelper learn-ir Lounge            # press a button, hex comes out
    python3 -m broadlinkstuff.broadlinkhelper send Lounge off            # code from CODES
    python3 -m broadlinkstuff.broadlinkhelper send Lounge 2600ca00...    # raw hex
    python3 -m broadlinkstuff.broadlinkhelper climate Lounge --temp 23   # Coolix computed
    python3 -m broadlinkstuff.broadlinkhelper special Lounge swing       # swing on/off (toggle)
    python3 -m broadlinkstuff.broadlinkhelper special Lounge swing-v-step -n 3   # vane 3 steps
    python3 -m broadlinkstuff.broadlinkhelper rename Lounge Wohnzimmer   # write the device name
    python3 -m broadlinkstuff.broadlinkhelper decode 2600ca00938e...     # no device, just maths
    python3 -m broadlinkstuff.broadlinkhelper selftest

Logs go to stderr, payload to stdout — so ``learn-ir`` can be put into a pipe
without log lines coming along.

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/broadlinkstuff/broadlinkhelper.py
"""

import json
import logging
import os
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypedDict, runtime_checkable

import broadlink
import typer
from broadlink import Device
from broadlink import exceptions as e
from broadlink.remote import data_to_pulses, pulses_to_data
from pydantic import BaseModel, Field, ValidationError, field_validator
from ruamel.yaml import YAML

import Helper
from broadlinkstuff import configure_logging, print_banner

logger = logging.getLogger(__name__)

# Default configuration with sample values, shipped with the package — the base
# that the local file is merged over, mirroring ``config.yaml``.
CONFIG_FILE = Path(__file__).parent / "broadlink.yaml"

# Local configuration (gitignored via ``*.local.*``), searched in the current
# working directory and next to this module; the first file that exists wins.
CONFIG_LOCAL_FILES = (
    Path.cwd() / "broadlink.local.yaml",
    Path(__file__).parent / "broadlink.local.yaml",
)

# The files :func:`load_devices` actually read, in merge order. Filled at import
# time and logged by the CLI callback once a sink exists.
CONFIG_SOURCES: list[Path] = []

# Keep it short: this is the delay after which a dead config entry becomes
# apparent, before the broadcast runs as a fallback. The library default is
# 10 s per device.
PROBE_TIMEOUT = 2

# How long to wait for a button press while learning.
LEARN_TIMEOUT = 30.0


# Capabilities instead of models.
#
# The obvious check would be isinstance(dev, rmmini) — which even holds, because
# in broadlink.remote everything hangs below rmmini (rm4pro -> rm4mini ->
# rmminib -> rmpro -> rmmini), but it answers "which model is this" and not "can
# this thing learn IR". That the two coincide is upstream inheritance and not a
# contract; a refactor over there would be silent here.
#
# runtime_checkable only verifies at runtime that the members are THERE, not
# their signatures — it is a hasattr test with names and documentation, not a
# proof that the method does the same thing. Good enough: the names come from
# exactly one library. issubclass() is not allowed with protocols carrying data
# fields (id); isinstance() is, and that is all we need.


@runtime_checkable
class Authenticatable(Protocol):
    """A device that negotiates a session — what condauth/with_auth use."""

    id: int

    def auth(self) -> bool:
        """Log in to the device and store the session id and AES key."""
        ...


@runtime_checkable
class IRLearner(Authenticatable, Protocol):
    """A device that can learn IR codes — what learn_ir() needs."""

    def enter_learning(self) -> None:
        """Switch on IR learning mode."""
        ...

    def check_data(self) -> bytes:
        """Fetch the most recently learned code, or raise ReadError/StorageError."""
        ...


@runtime_checkable
class RFLearner(IRLearner, Protocol):
    """Additionally the RF part — the pro models only.

    Inherits from IRLearner on purpose: a device with RF can always do IR too.
    """

    def sweep_frequency(self) -> None:
        """Start searching for the carrier frequency."""
        ...

    def check_frequency(self) -> tuple[bool, float]:
        """Return whether a frequency was found, and which one (MHz)."""
        ...

    def find_rf_packet(self, frequency: float | None = None) -> None:
        """Switch on RF learning mode at this frequency."""
        ...

    def cancel_sweep_frequency(self) -> None:
        """Abort the frequency search."""
        ...


@runtime_checkable
class SensorReader(Authenticatable, Protocol):
    """A device with a temperature/humidity sensor — what sensors() needs.

    Sits on ``broadlink.remote.rm4mini`` and thus covers the whole RM4 line,
    while the RM3 generation below it has no sensors at all. Asking for the
    method rather than the model keeps that distinction where it belongs.
    """

    def check_sensors(self) -> dict[str, float]:
        """Read the sensors, e.g. ``{'temperature': 25.5, 'humidity': 49.5}``."""
        ...


@runtime_checkable
class Renameable(Authenticatable, Protocol):
    """A device whose name can be written — what rename() needs.

    ``update()`` deliberately belongs in this protocol and not just
    ``set_name()``: the two are only safely usable TOGETHER here (see
    :meth:`BroadlinkFleet.rename`). And it is not an academic case — ``update()``
    hangs off ``broadlink.remote.rmmini``, whereas ``set_name()`` already sits on
    ``Device``. So there are device classes with the write path but without the
    read path; those are precisely what this protocol excludes.
    """

    name: str
    is_locked: bool

    def update(self) -> None:
        """Pull name and lock status from the device."""
        ...

    def set_name(self, name: str) -> None:
        """Write the name onto the device."""
        ...


class DeviceConfig(TypedDict):
    """What is needed to address a device without a broadcast.

    ``devtype`` selects the class in ``gendevice()`` (0x648d -> rm4mini,
    0x649b -> rm4pro) and additionally goes into every packet (device.py:279).
    Just like ``mac`` it is fixed; only the IP can wander.

    There is NO named constant for it in the library, and there could not
    sensibly be one: the value identifies the hardware revision, not the
    product. "RM4 mini" alone has eight devtypes (0x51da 0x520c 0x5216 0x521c
    0x610e 0x62bc 0x648d 0x653a), "RM4 pro" six. Hence broadlink keeps a table
    instead of constants::

        >>> broadlink.SUPPORTED_TYPES[rm4mini][0x648D]
        ('RM4 mini', 'Broadlink')

    The right value comes from discovering the concrete device — guessing is not
    an option. For readability :meth:`BroadlinkFleet.config_block` writes the
    model as a comment next to each generated entry.
    """

    devtype: int
    host: tuple[str, int]
    mac: str


class BroadlinkDevice(BaseModel):
    """One configured device, as it is written in the YAML.

    Attributes:
        devtype: Hardware revision, see :class:`DeviceConfig`. YAML parses a
            bare ``0x649B`` as an int; a quoted ``"0x649B"`` is accepted too.
        host: IP address — the only field that moves on its own (DHCP).
        port: TCP port, 80 for every RM4 seen so far.
        mac: Lower-case hex without separators.
    """

    devtype: int
    host: str
    port: int = Field(default=80)
    mac: str

    @field_validator("devtype", mode="before")
    @classmethod
    def _hex_devtype(cls, v: Any) -> Any:
        """Accept a quoted hex string where YAML did not resolve one itself.

        Args:
            v: The raw value from the YAML document.

        Returns:
            The value unchanged, or the parsed int for a string — base 0 honours
            a ``0x`` prefix and leaves a decimal string decimal.
        """
        return int(v, 0) if isinstance(v, str) else v


class BroadlinkConfig(BaseModel):
    """The ``broadlink:`` section of the configuration."""

    devices: dict[str, BroadlinkDevice] = Field(default_factory=dict)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML document, tolerating an absent file.

    Args:
        path: The file to read.

    Returns:
        The parsed mapping; empty for a missing or empty file.

    Raises:
        ValueError: The file exists but is not parsable YAML.
    """
    if not path.is_file():
        return {}
    try:
        data = YAML(typ="safe").load(path)
    except Exception as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    return data or {}


def load_devices() -> dict[str, DeviceConfig]:
    """Read the device configuration from the YAML files.

    ``broadlink.yaml`` provides the base, the first existing file from
    :data:`CONFIG_LOCAL_FILES` is merged over it with ``Helper.update_deep`` —
    the same base/local layering ``config.yaml``/``config.local.yaml`` uses.

    Returns:
        Name -> device, with host and port flattened into the tuple the
        broadlink library wants. Empty when nothing is configured, which makes
        the fleet fall back to a broadcast search.

    Raises:
        ValueError: A file is unparsable, or an entry does not validate.
    """
    sources = [CONFIG_FILE]
    merged = _read_yaml(CONFIG_FILE)
    for path in CONFIG_LOCAL_FILES:
        if path.is_file():
            merged = Helper.update_deep(merged, _read_yaml(path))  # type: ignore[assignment]
            sources.append(path)
            break
    # Not logged here: this runs at import time, before the CLI has configured
    # any sink -- a DEBUG line would go nowhere. The callback logs it instead.
    CONFIG_SOURCES[:] = sources

    try:
        config = BroadlinkConfig.model_validate(merged.get("broadlink") or {})
    except ValidationError as exc:
        raise ValueError(f"broadlink.devices in {sources[-1]} is unusable: {exc}") from exc

    return {
        name: DeviceConfig(devtype=device.devtype, host=(device.host, device.port), mac=device.mac)
        for name, device in config.devices.items()
    }


# Known devices, read once at import time. Empty -> ``discover`` searches and
# prints a ready-made block to paste into ``broadlink.local.yaml``.
DEVICES: dict[str, DeviceConfig] = load_devices()


# ---------------------------------------------------------------------------
# Coolix — computing climate codes instead of learning them
# ---------------------------------------------------------------------------
#
# The three learned climate codes in CODES are not an opaque blob but the
# "Coolix" protocol (IRremoteESP8266: decode_type_t::COOLIX). That makes the
# complete matrix of mode × fan speed × temperature computable — 280
# combinations that would otherwise all have to be learned individually in
# front of the device.
#
# On the wire:
#
#   Header 4692 us mark / 4416 us space, then 48 bits as pulse distance (mark
#   always 552 us; space 552 us = 0, 1652 us = 1), MSB first. Those 48 bits are
#   THREE data bytes, each followed by its complement — hence 24 bits of payload
#   in 48 bits of transmission. The whole packet is sent TWICE, separated by
#   ~5.4 ms.
#
# The 24 bits as 0xB2_XX_YY:
#
#   byte 0  always 0xB2 (preamble, constant across all 280 combinations)
#   byte 1  fan nibble << 4 | 0xF
#   byte 2  temperature nibble << 4 | mode nibble
#
# Two pitfalls a naive generator gets wrong:
#
# 1) The temperature is GRAY-CODED, not binary (17 C -> 0x0, 18 -> 0x1,
#    19 -> 0x3, 20 -> 0x2, ...). Compute it linearly and you only hit 17 and 18.
# 2) "dry" and "heat_cool" drive the fan themselves and therefore carry a fixed
#    fan nibble 0x1 — a real remote NEVER sends 0xB/0x9/0x5/0x3 there.
#    "fan_only" in turn shares the mode nibble 0x4 with "dry" and is
#    distinguishable solely by the temperature field reading 0xE instead of a
#    temperature.
#
# The unit is a REMKO air conditioner; REMKO is a rebadger, the hardware is
# built by Midea. The remote is an RG57A4/BGEF — the RG5x/BGEF series is Midea
# OEM and speaks Coolix.
#
# CAREFUL when searching for the model number: the same number exists as
# RG57A4/BGEFU1 (US variant, Fahrenheit). That one does NOT speak Coolix but the
# other Midea protocol — 48 bits with preamble 0xA1, second frame being the
# bitwise inverse of the first instead of per-byte complement pairs. Go by the
# type plate rather than by the measured signal and you end up there wrongly.
#
# Verified against three independent sources, all three fully congruent:
#
#   - the three self-learned codes                          (coolix_selftest())
#   - SmartIR 1394.json, Midea RG70C/BGEF                        280 out of 280
#   - SmartIR fork litinoveweedle, 1395.json, RG57A6/BGEF        277 out of 277
#
# The second file is the closest sibling to the RG57A4/BGEF. Its fanModes carry
# a fifth "silent" — that is an error in the file and not a protocol property:
# under cool the silent codes are byte-identical with auto, and under fan_only
# it lists 0xB5F5B6, which is not a climate state at all but the special command
# "Clean". Four fan speeds are complete.


class CoolixMode(StrEnum):
    """Operating modes. StrEnum so typer shows the choices in --help."""

    COOL = "cool"
    DRY = "dry"
    HEAT = "heat"
    FAN_ONLY = "fan_only"
    HEAT_COOL = "heat_cool"


class CoolixFan(StrEnum):
    """Fan speeds. Without effect for dry and heat_cool, see coolix_word()."""

    AUTO = "auto"
    LOW = "low"
    MID = "mid"
    HIGH = "high"


# Timings in us per IRremoteESP8266 (ir_Coolix.cpp). Deliberately the NOMINAL
# values and not the ones measured at the RM4 (4827/4663/...): measured always
# has the receiver jitter baked in, and air conditioners tolerate broadly.
COOLIX_HDR_MARK = 4692
COOLIX_HDR_SPACE = 4416
COOLIX_BIT_MARK = 552
COOLIX_ONE_SPACE = 1652
COOLIX_ZERO_SPACE = 552

# Gap between the frames. The library adds it up from ticks (kCoolixMinGap,
# ir_Coolix.cpp:34) and arrives at 5244; the RM4 measures 5582..5681 while
# learning. The nominal value is used, for the same reason as the timings above.
COOLIX_GAP = 5244

# Trailing gap. The RM4 appends it to every learned code (0x0d05 ticks);
# without it the last mark lacks the edge that marks the end of the packet.
COOLIX_TRAILING_GAP = 109445

# Switching off is a word of its own and not a mode variant — the temperature
# within is meaningless. Identical to kCoolixOff from IRremoteESP8266.
COOLIX_OFF = 0xB27BE0

# Special commands. These are self-contained words sent INSTEAD of a state —
# IRremoteESP8266 calls them "special states" and restores the remembered normal
# state afterwards.
#
# The vane position sits in NO bit of the state word: the 24 bits are fully
# occupied by 8 preamble + 4 fan + 4 constant 0xF + 4 temperature + 4 mode.
# There is therefore no absolute vane position, only toggling and stepping — see
# the documentation at the constants.
#
# Values from ir_Coolix.h, BUT computed from the binary literals rather than
# copied from the hex comments: for kCoolixSwingH the two contradict each other.
# The literal yields 0xB2F5A2, the comment claims 0xB5F5A2 — which is the value
# of kCoolixTurbo, i.e. a copy-paste error in the comment. What gets compiled is
# the literal.

# Swing on/off. A TOGGLE without state query: sent twice it is off again, and
# where the vane stands is known only to the unit.
COOLIX_SWING = 0xB26BE0

# Vane one step further (kCoolixSwingV, addressed as "SwingVStep" in the
# library). This too is relative, not a position.
#
# NOTE: to be sent as a SINGLE frame, see COOLIX_SINGLE_FRAME_WORDS.
COOLIX_SWING_V_STEP = 0xB20FE0

# Horizontal swing. UNCONFIRMED, for three reasons:
#
# 1) In IRremoteESP8266 kCoolixSwingH is a dead constant — it is never sent,
#    never decoded, there is neither a setSwingH() nor a case in the state
#    machine. Untested code, in other words.
# 2) ESPHome's own Coolix implementation knows only the swing toggle and has no
#    horizontal command at all.
# 3) Their own hex comment contradicts their binary literal (see above).
#
# Unlike the other words here this is nothing to rely on before having seen it
# work on one's own unit.
COOLIX_SWING_H = 0xB2F5A2

# Words sent as ONE frame instead of two.
#
# Coolix normally repeats every message once — sendCOOLIX() does
# `for (r = 0; r <= repeat; r++)` with kCoolixDefaultRepeat = 1, i.e. two
# frames, and that is exactly what the learned codes show. The vane step is the
# exception: IRCoolixAC::send() pulls the repeat down to 0 for it
# (ir_Coolix.cpp:113-118, "SwingVStep needs to be sent with `0` repeats").
#
# The reason is obvious once seen: the command is relative. Two frames would be
# two steps. Get sloppy here and you get a vane that jumps two positions per
# button press — and since the protocol has no feedback, it only shows on the
# device.
COOLIX_SINGLE_FRAME_WORDS = frozenset({COOLIX_SWING_V_STEP})

COOLIX_SLEEP = 0xB2E003
COOLIX_TURBO = 0xB5F5A2
COOLIX_LED = 0xB5F5A5
COOLIX_CLEAN = 0xB5F5AA

# Word -> name. The same table serves coolix_describe() and the `special`
# command, hence the names are CLI-friendly tokens without spaces. The values
# are unique, so the inversion below is lossless.
COOLIX_SPECIALS: dict[int, str] = {
    COOLIX_OFF: "off",
    COOLIX_SWING: "swing",
    COOLIX_SWING_V_STEP: "swing-v-step",
    COOLIX_SWING_H: "swing-h",
    COOLIX_SLEEP: "sleep",
    COOLIX_TURBO: "turbo",
    COOLIX_LED: "led",
    COOLIX_CLEAN: "clean",
}

COOLIX_SPECIAL_BY_NAME: dict[str, int] = {name: word for word, name in COOLIX_SPECIALS.items()}

COOLIX_MIN_TEMP = 17
COOLIX_MAX_TEMP = 30

_COOLIX_PREFIX = 0xB2
_COOLIX_FAN = {"auto": 0xB, "low": 0x9, "mid": 0x5, "high": 0x3}
_COOLIX_MODE = {"cool": 0x0, "dry": 0x4, "heat": 0xC, "heat_cool": 0x8, "fan_only": 0x4}
_COOLIX_TEMP = (0x0, 0x1, 0x3, 0x2, 0x6, 0x7, 0x5, 0x4, 0xC, 0xD, 0x9, 0x8, 0xA, 0xB)

# Modes that regulate the fan themselves — see pitfall 2 above.
_COOLIX_FIXED_FAN_MODES = frozenset({"dry", "heat_cool"})
_COOLIX_FIXED_FAN = 0x1

# Placeholder in the temperature field when only the fan is running.
_COOLIX_FAN_ONLY_TEMP = 0xE


def coolix_word(mode: CoolixMode, temp: int = 22, fan: CoolixFan = CoolixFan.AUTO) -> int:
    """Build the 24-bit Coolix word for a state.

    Args:
        mode: Operating mode. For ``fan_only`` the ``temp`` is ignored.
        temp: Target temperature in degrees, 17 to 30.
        fan: Fan speed. Ignored for ``dry`` and ``heat_cool``, because the unit
            regulates it itself there.

    Returns:
        The word, e.g. 0xB2BF20 for cool/auto/20.

    Raises:
        ValueError: Unknown mode, unknown fan speed, or ``temp`` outside 17..30.
    """
    if mode not in _COOLIX_MODE:
        raise ValueError(f"unknown mode {mode!r} — allowed: {sorted(_COOLIX_MODE)}")
    if fan not in _COOLIX_FAN:
        raise ValueError(f"unknown fan speed {fan!r} — allowed: {sorted(_COOLIX_FAN)}")

    if mode == CoolixMode.FAN_ONLY:
        temp_nibble = _COOLIX_FAN_ONLY_TEMP
    else:
        if not COOLIX_MIN_TEMP <= temp <= COOLIX_MAX_TEMP:
            raise ValueError(f"temperature {temp} outside {COOLIX_MIN_TEMP}..{COOLIX_MAX_TEMP}")
        temp_nibble = _COOLIX_TEMP[temp - COOLIX_MIN_TEMP]

    fan_nibble = _COOLIX_FIXED_FAN if mode in _COOLIX_FIXED_FAN_MODES else _COOLIX_FAN[fan]

    return (_COOLIX_PREFIX << 16) | ((fan_nibble << 4 | 0xF) << 8) | (temp_nibble << 4 | _COOLIX_MODE[mode])


def coolix_frame_count(word: int) -> int:
    """Tell how often a word belongs on the wire.

    Args:
        word: 24-bit word.

    Returns:
        1 for relative step commands, 2 otherwise.
    """
    return 1 if word in COOLIX_SINGLE_FRAME_WORDS else 2


def coolix_pulses(word: int, *, frames: int | None = None) -> list[int]:
    """Turn a Coolix word into a pulse/space sequence.

    Args:
        word: 24-bit word, e.g. from ``coolix_word()`` or ``COOLIX_OFF``.
        frames: How many frames. None takes ``coolix_frame_count()`` — which is
            2 for everything but the vane step.

    Returns:
        Durations in microseconds, alternating mark and space — the format
        ``pulses_to_data()`` expects.
    """
    bits: list[int] = []
    for shift in (16, 8, 0):
        byte = (word >> shift) & 0xFF
        # Every byte once plain, once inverted. That is the only error detection
        # the protocol has.
        for value in (byte, ~byte & 0xFF):
            bits.extend((value >> i) & 1 for i in range(7, -1, -1))

    frame = [COOLIX_HDR_MARK, COOLIX_HDR_SPACE]
    for bit in bits:
        frame += [COOLIX_BIT_MARK, COOLIX_ONE_SPACE if bit else COOLIX_ZERO_SPACE]

    # Sending twice is part of the protocol for the state words and not
    # redundancy for safety: units that see only one frame ignore it. For the
    # vane step it is harmful the other way round, see COOLIX_SINGLE_FRAME_WORDS.
    count = coolix_frame_count(word) if frames is None else frames
    pulses: list[int] = []
    for index in range(count):
        last = index == count - 1
        pulses += frame + [COOLIX_BIT_MARK, COOLIX_TRAILING_GAP if last else COOLIX_GAP]
    return pulses


def coolix_code(mode: CoolixMode, temp: int = 22, fan: CoolixFan = CoolixFan.AUTO) -> bytes:
    """Build a ready-to-send Broadlink packet for a climate state.

    The counterpart to :meth:`BroadlinkFleet.learn_ir`: what is learned there in
    front of the device falls out here without any hardware.

    Args:
        mode: Operating mode.
        temp: Target temperature in degrees, 17 to 30.
        fan: Fan speed.

    Returns:
        The code for ``dev.send_data()`` — with ``.hex()`` it can be put into
        ``CODES`` exactly like a learned one.
    """
    return pulses_to_data(coolix_pulses(coolix_word(mode, temp, fan)))


def coolix_raw_code(word: int) -> bytes:
    """Build a Broadlink packet from a ready-made Coolix word.

    For the special commands that are not a state — ``COOLIX_SWING`` and
    relatives. For normal states ``coolix_code()`` is what is meant.

    Args:
        word: 24-bit word, e.g. ``COOLIX_SWING``.

    Returns:
        The code for ``dev.send_data()``.
    """
    return pulses_to_data(coolix_pulses(word))


def coolix_from_data(data: bytes) -> int | None:
    """Read the Coolix word back out of a Broadlink packet.

    The way back, to classify a learned code instead of just filing it as a hex
    sausage. Verifies the complement bytes along the way — what yields a word
    here IS Coolix and not merely similarly timed.

    Args:
        data: Raw packet, e.g. from ``check_data()`` or ``bytes.fromhex()``.

    Returns:
        The 24-bit word, or None when the packet is not Coolix.
    """
    if not data or data[0] != 0x26:
        return None

    pulses = data_to_pulses(data)
    bits: list[int] = []
    index = 0
    while index + 1 < len(pulses):
        mark, space = pulses[index], pulses[index + 1]
        index += 2
        if mark > 2500:  # header — the second frame starts over
            bits = []
            continue
        if space > 2500:  # gap or trailing gap: frame over
            break
        bits.append(1 if space > 1000 else 0)

    if len(bits) != 48:
        return None

    payload = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, 48, 8)]
    if any(payload[i + 1] != (~payload[i] & 0xFF) for i in (0, 2, 4)):
        return None

    return (payload[0] << 16) | (payload[2] << 8) | payload[4]


def coolix_describe(word: int) -> str:
    """Translate a Coolix word back into readable settings.

    Args:
        word: 24-bit word, e.g. from ``coolix_from_data()``.

    Returns:
        Something like ``"cool/auto/20C"``, ``"swing"`` or ``"unknown"``.
    """
    # Special commands first — they look like states but are not.
    # kCoolixCmdFan (0xB2BFE4) is deliberately NOT in the table: that IS a
    # regular state, namely fan_only/auto, and is resolved correctly below.
    if word in COOLIX_SPECIALS:
        return COOLIX_SPECIALS[word]

    mode_nibble, temp_nibble = word & 0x0F, (word >> 4) & 0x0F
    fan_nibble = (word >> 12) & 0x0F

    mode: str
    temp: int | None
    if temp_nibble == _COOLIX_FAN_ONLY_TEMP and mode_nibble == _COOLIX_MODE["fan_only"]:
        mode, temp = "fan_only", None
    else:
        # dry and fan_only share 0x4 — fan_only is already handled above.
        modes = [m for m, n in _COOLIX_MODE.items() if n == mode_nibble and m != "fan_only"]
        if not modes or temp_nibble not in _COOLIX_TEMP:
            return "unknown"
        mode = modes[0]
        temp = COOLIX_MIN_TEMP + _COOLIX_TEMP.index(temp_nibble)

    if mode in _COOLIX_FIXED_FAN_MODES:
        fan = "fixed"
    else:
        fans = [f for f, n in _COOLIX_FAN.items() if n == fan_nibble]
        if not fans:
            return "unknown"
        fan = fans[0]

    return f"{mode}/{fan}" + (f"/{temp}C" if temp is not None else "")


def describe_code(data: bytes) -> str:
    """Interpret a raw packet as far as possible.

    Args:
        data: Raw Broadlink packet.

    Returns:
        The Coolix interpretation, or a hint about the packet format.
    """
    word = coolix_from_data(data)
    if word is not None:
        return f"Coolix 0x{word:06X} = {coolix_describe(word)}"
    kind = {0x26: "IR", 0xB2: "RF 433", 0xD7: "RF 315"}.get(data[0] if data else -1, "?")
    return f"not Coolix ({kind}, {len(data)} bytes)"


# Own code library. This is where learned codes end up — the device itself does
# not have them (see the comment block at BroadlinkFleet).
#
# The three climate codes below are Coolix and therefore do NOT need to be
# learned — coolix_code() computes every combination. They stay as a reference:
# coolix_selftest() checks the generator against them.
CODES: dict[str, str] = {
    # "tv_power": "26005000...",
    "cool_20_noflapmove": "2600ca00938e11361013103710371013101311361113101310371013111310361136111310361136111310361136113610371037103611131037101310131113101311131013101311131037101310131113101311131036113611131036113611361037103710ad8f9310371013103710371013101311361014101310371013101410361136101410361136101410361136103710371036113611131036111310131113101310141013101311131036111310131113101310131136113610131136113610371037103611000d05",
    "cool_23_noflapmove": "2600ca00909013341310143313341310141013341310131014331311131013341334131013341334131013341333143313341334133413101334131013111310131014101310131113351210133413101310141013101334131014331311133314331334133413aa929013341310133413341310131014331311131013341310131113331433131113331433131113331433133413341334133314101334131013101410131013111310131014331311133313111310131113101334131013341310133413341334133314000d05",
    "off": "2600ca008d9311361013113611361013111310371013101311361014101310371036111310371013103710371036113611131036113611361013111310131014103611131013103710371036111310131113101310131113101311131135113611361037103710ad8f9310361113103710361113101311361013111310371013101311361136101311361014103611361037103710131037103710361113101311131013103710131113103611361136101311131013111310131013111310131037103710361136113610000d05",
}

# Add all temperatures as ready-made codes, in the same mode and with the same
# fan speed as the learned reference codes (cool/auto).
#
# Computed rather than pasted in as hex literals: that keeps the codes in
# exactly ONE place, namely the tables above. A block of literals would be a
# second truth that silently drifts apart at the first typo — and nobody proof-
# reads 14 lines of 400 characters each anyway.
#
# Whoever needs the hex form (for HA, Node-RED, wherever) gets it with
# coolix_code("cool", 22).hex() or from CODES — which is the same thing.
#
# Other modes are one more line, e.g. for heating:
#     CODES |= {f"heat_auto_{t}": coolix_code(CoolixMode.HEAT, t).hex()
#               for t in range(COOLIX_MIN_TEMP, COOLIX_MAX_TEMP + 1)}
CODES |= {
    f"cool_auto_{temp}": coolix_code(CoolixMode.COOL, temp).hex()
    for temp in range(COOLIX_MIN_TEMP, COOLIX_MAX_TEMP + 1)
}


def coolix_selftest() -> list[str]:
    """Check the generator against the self-learned codes in ``CODES``.

    The comparison happens at word level and not byte-wise: the RM4 measures
    with its own jitter while learning (measured 4827/4663 instead of
    4692/4416), so the packets cannot possibly be identical — the payload can.

    Returns:
        One line per checked code.
    """
    expected = {
        "cool_20_noflapmove": coolix_word(CoolixMode.COOL, 20, CoolixFan.AUTO),
        "cool_23_noflapmove": coolix_word(CoolixMode.COOL, 23, CoolixFan.AUTO),
        "off": COOLIX_OFF,
    }

    report: list[str] = []
    for label, want in expected.items():
        # Both directions: the learned code must carry the expected word, and
        # the self-built packet must yield the same word again.
        learned = coolix_from_data(bytes.fromhex(CODES[label]))
        built = coolix_from_data(pulses_to_data(coolix_pulses(want)))
        if learned is None or built is None:
            report.append(f"  FAIL {label:20s} not decodable as Coolix")
            continue
        ok = learned == want == built
        report.append(
            f"  {'OK  ' if ok else 'FAIL'} {label:20s} learned=0x{learned:06X} "
            f"built=0x{built:06X} -> {coolix_describe(want)}"
        )
    return report


def resolve_code(spec: str) -> bytes:
    """Resolve a command-line specification into a ready-to-send packet.

    Accepts a name from ``CODES`` or raw hex. The name wins should both apply —
    names here are never hex-suspicious.

    Args:
        spec: Name from ``CODES`` or hex string.

    Returns:
        The raw packet.

    Raises:
        ValueError: Neither a known name nor valid hex.
    """
    if spec in CODES:
        return bytes.fromhex(CODES[spec])
    try:
        return bytes.fromhex(spec.strip())
    except ValueError:
        raise ValueError(
            f"{spec!r} is neither a name from CODES nor valid hex. Known: {', '.join(sorted(CODES))}"
        ) from None


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
#
# What an RM can do and what it can NOT — both are a no, and for two different
# reasons:
#
# 1) SUBDEVICES exist only on broadlink.hub.s3 (`get_subdevices()`, max. 8,
#    addressed via `did`). An RM4 pro and an RM4 mini are both
#    broadlink.remote.*, so they do not even have the method. An RM has no child
#    devices; it blasts IR/RF into the room and does not know who is listening.
#
# 2) CREATED COMMANDS are not stored by the device. There is no command memory
#    and correspondingly nothing to list: `check_data()` reads out ONE volatile
#    buffer, namely the most recently learned code, and raises
#    ReadError/StorageError when it is empty. The named commands ("TV on") live
#    in the Broadlink app respectively its cloud, not on the box. Whoever wants
#    them here learns them once and files them in CODES — the above IS that
#    library.
#
# The complete command repertoire of both devices:
#
#   Both (rmmini inheritance):
#     dev.update()                 pull name + lock status from the device
#     dev.send_data(bytes)         send a code (IR or RF, same method)
#     dev.enter_learning()         IR learning mode on
#     dev.check_data() -> bytes    fetch the most recently learned code
#     dev.check_sensors()          {'temperature': ..., 'humidity': ...}
#     dev.check_temperature() / dev.check_humidity()
#     dev.set_name(str) / dev.set_lock(bool)
#     dev.get_type() / dev.get_fwversion() / dev.ping()
#
#   RM4 pro only (rmpro inheritance, the RF part):
#     dev.sweep_frequency()        search for the carrier frequency
#     dev.check_frequency()        -> (found?, MHz)
#     dev.find_rf_packet(mhz)      RF learning mode at this frequency
#     dev.cancel_sweep_frequency() abort the search
#
#   Module functions, no device access:
#     pulses_to_data([us, ...]) -> bytes    microseconds -> Broadlink packet
#     data_to_pulses(bytes) -> [us, ...]    and back, for inspection


class BroadlinkFleet:
    """The configured devices, their sessions and what one does with them.

    Keeps a cache of logged-in devices: ``auth()`` negotiates a session id and
    an AES key, which should happen once per run and not once per command. That
    is why the instance is what lives across the commands, rather than a module
    dict.
    """

    def __init__(
        self,
        config: dict[str, DeviceConfig] | None = None,
        *,
        probe: bool = True,
        probe_timeout: int = PROBE_TIMEOUT,
        rediscover: bool = True,
        dry_run: bool = False,
    ) -> None:
        """Create the fleet without generating network traffic yet.

        Args:
            config: Device configuration; None takes ``DEVICES``.
            probe: Confirm entries via ``hello()`` instead of building offline.
            probe_timeout: Seconds per unicast attempt.
            rediscover: Fall back to ``discover()`` when unreachable.
            dry_run: Send nothing, only log.
        """
        self.config = DEVICES if config is None else config
        self.probe = probe
        self.probe_timeout = probe_timeout
        self.rediscover = rediscover
        self.dry_run = dry_run
        self._devices: dict[str, Device] | None = None

    # -- session -----------------------------------------------------------

    @staticmethod
    def condauth(dev: Authenticatable) -> bool:
        """Log in to a device, unless that already happened.

        The state is already on the object: ``dev.id`` is 0 while not logged in
        and afterwards carries the session id the firmware assigned
        (``Device.auth`` sets it in device.py:188). A flag NEXT TO it would be
        wrong here, and a global one all the more so — ``auth()`` negotiates
        session id and AES key PER DEVICE. A second device would otherwise stay
        on the factory key from ``Device.__init__``, with which the firmware
        only accepts the login handshake itself: every other command is then
        answered with -7 "Control key is expired".

        Args:
            dev: Device from ``discover()`` or ``gendevice()``.

        Returns:
            True when the device is logged in now.
        """
        if not dev.id:
            dev.auth()
        return bool(dev.id)

    def with_auth[T](self, dev: Authenticatable, action: Callable[[], T]) -> T:
        """Run a device call and log in again on a dead session.

        -7 can also occur perfectly legitimately when the session ages or the
        device restarts in between — the session id is then still set on the
        object, but the firmware no longer knows it. Exactly one retry: if the
        second login does not hold, it is not a session problem and the error
        should propagate instead of disappearing into a loop.

        Args:
            dev: Device from ``discover()`` or ``gendevice()``.
            action: The call, typically a lambda on ``dev``.

        Returns:
            Whatever ``action`` returns.
        """
        self.condauth(dev)
        try:
            return action()
        except e.AuthorizationError:
            logger.debug("session expired, logging in again")
            dev.auth()
            return action()

    # -- construction ------------------------------------------------------

    def build_device(self, name: str, cfg: DeviceConfig) -> Device:
        """Create a device object from a config entry.

        Two ways, both without a broadcast:

        ``probe=False`` builds the object purely locally with ``gendevice()`` —
        zero network traffic, measured 0.00 s. ``name`` is taken from the config
        key, because the device would otherwise stay nameless.

        ``probe=True`` asks the one IP directly via ``hello()`` (unicast,
        measured 0.02 s against 5.02 s broadcast) and gets name, model and above
        all ``is_locked`` from the device itself.

        The latter is not cosmetic: ``set_name()`` sends ``self.is_locked``
        along (device.py:255). A locally built object carries the default False —
        so renaming a locked device would silently unlock it. Whoever writes
        uses ``probe=True`` or calls ``update()`` beforehand.

        Args:
            name: Key from the configuration.
            cfg: The corresponding entry.

        Returns:
            The device, not yet logged in.

        Raises:
            broadlink.exceptions.NetworkTimeoutError: With ``probe=True``, when
                nothing answers at that IP.
        """
        if self.probe:
            return broadlink.hello(cfg["host"][0], port=cfg["host"][1], timeout=self.probe_timeout)

        dev = broadlink.gendevice(cfg["devtype"], cfg["host"], cfg["mac"], name=name)
        dev.timeout = self.probe_timeout
        return dev

    @property
    def devices(self) -> dict[str, Device]:
        """The logged-in devices, built on first access.

        Returns:
            Name -> logged-in device.
        """
        if self._devices is None:
            self._devices = self._connect_all()
        return self._devices

    def _connect_all(self) -> dict[str, Device]:
        """Build all config entries and log them in.

        Every entry is built and logged in once — that is the proof it is
        correct. If one does not answer, ``discover()`` runs ONCE and the hits
        are matched by MAC: that one is stable, whereas the name may have been
        changed and the IP reassigned by DHCP. A stale IP is reported so the
        config can be updated — it is not repaired, that belongs in the file and
        not in process memory.

        Returns:
            Name -> logged-in device.
        """
        if not self.config:
            logger.warning("no devices configured — searching by broadcast")
            found = self.discover()
            return {d.name or d.host[0]: d for d in found if self.condauth(d)}

        devices: dict[str, Device] = {}
        stale: dict[str, DeviceConfig] = {}

        for name, cfg in self.config.items():
            try:
                dev = self.build_device(name, cfg)
                self.condauth(dev)
            except e.NetworkTimeoutError:
                stale[name] = cfg
                continue
            logger.debug(f"{name}: {dev.model} at {dev.host[0]}")
            devices[name] = dev

        if stale and self.rediscover:
            logger.warning(f"unreachable: {', '.join(stale)} — broadcast as a fallback")
            by_mac = {d.mac.hex(): d for d in self.discover()}
            for name, cfg in stale.items():
                dev = by_mac.get(cfg["mac"])
                if dev is None:
                    logger.error(f"{name}: not found by broadcast either (off? wrong MAC?)")
                    continue
                logger.warning(f"{name}: IP {cfg['host'][0]} -> {dev.host[0]} — configured address is stale")
                if self.condauth(dev):
                    devices[name] = dev
        elif stale:
            for name in stale:
                logger.error(f"{name}: unreachable")

        return devices

    def get(self, name: str) -> Device:
        """Fetch a logged-in device by name.

        Args:
            name: Key from the configuration.

        Returns:
            The logged-in device.

        Raises:
            KeyError: No device of that name is reachable.
        """
        try:
            return self.devices[name]
        except KeyError:
            raise KeyError(
                f"{name!r} not reachable. Available: {', '.join(sorted(self.devices)) or '(none)'}"
            ) from None

    def only(self, name: str) -> "BroadlinkFleet":
        """A fleet narrowed down to a single configured device.

        :attr:`devices` builds and logs in EVERY entry, because that is what
        makes a listing trustworthy. For a command that addresses one device
        that is a ``hello()`` plus an ``auth()`` handshake per configured
        device, all of it wasted — and worse, a single unreachable entry drags
        a 5 s broadcast along. Narrowing the configuration keeps the traffic on
        the device actually asked for.

        The flags are carried over, so ``--no-probe`` and friends keep applying.

        Args:
            name: Key from the configuration.

        Returns:
            A second fleet over that one entry, nothing logged in yet.

        Raises:
            KeyError: No such entry in the configuration.
        """
        # Nothing configured means the whole fleet runs off a broadcast search,
        # where the names only come into being with the answers. There is
        # nothing to narrow then; get() reports an unknown name itself.
        if not self.config:
            return self
        if name not in self.config:
            raise KeyError(f"{name!r} is not configured. Configured: {', '.join(sorted(self.config)) or '(none)'}")
        return BroadlinkFleet(
            {name: self.config[name]},
            probe=self.probe,
            probe_timeout=self.probe_timeout,
            rediscover=self.rediscover,
            dry_run=self.dry_run,
        )

    @staticmethod
    def discover(timeout: int = 5) -> list[Device]:
        """Search for devices by broadcast.

        Args:
            timeout: Seconds to wait for answers.

        Returns:
            All devices found, not logged in.
        """
        return broadlink.discover(timeout=timeout)

    @staticmethod
    def config_block(devices: list[Device]) -> str:
        """Build a ``broadlink.devices`` YAML block from discovered devices.

        Args:
            devices: Result of ``discover()``.

        Returns:
            YAML ready to paste into ``broadlink.local.yaml``. The model goes into
            a comment behind each devtype — that value is not readable by itself
            and there is no constant for it (see :class:`DeviceConfig`).
        """
        lines = ["broadlink:", "  devices:"]
        for dev in devices:
            name = dev.name or f"{dev.model}-{dev.host[0]}"
            lines += [
                f"    {name}:",
                f"      devtype: 0x{dev.devtype:04X}   # {dev.model}",
                f'      host: "{dev.host[0]}"',
                f"      port: {dev.host[1]}",
                f'      mac: "{dev.mac.hex()}"',
            ]
        return "\n".join(lines)

    # -- actions -----------------------------------------------------------

    def send(self, name: str, data: bytes, *, repeat: int = 1, delay: float = 0.5) -> None:
        """Send a packet through a device.

        Args:
            name: Device name from the configuration.
            data: Raw packet — IR and RF go through the same method, what is
                sent sits in the first byte (0x26 = IR).
            repeat: How often. Useful for step commands such as
                ``COOLIX_SWING_V_STEP``, where n steps are n transmissions.
            delay: Pause between repetitions in seconds.
        """
        dev = self.get(name)
        meaning = describe_code(data)
        if self.dry_run:
            # Do NOT put the hex into the log here: 412 characters per line are
            # unreadable. Whoever needs it gets it from the CLI on stdout.
            logger.info(f"DRY-RUN: {name} -> {meaning} ({len(data)} bytes, {repeat}x)")
            return

        for index in range(repeat):
            if index:
                time.sleep(delay)
            logger.info(f"{name} -> {index + 1}/{repeat} [{meaning}]")
            self.with_auth(dev, lambda: dev.send_data(data))

    def sensors(self, name: str) -> dict[str, float]:
        """Read the sensors of one device.

        A read, so ``dry_run`` does not apply — there is nothing to suppress.

        Args:
            name: Device name from the configuration.

        Returns:
            What the device measures, e.g. ``{'temperature': 25.5,
            'humidity': 49.5}``.

        Raises:
            TypeError: The device has no sensors.
        """
        dev = self.get(name)
        if not isinstance(dev, SensorReader):
            raise TypeError(f"{name} ({dev.model}) has no sensors")
        return self.with_auth(dev, dev.check_sensors)

    def rename(self, name: str, new_name: str) -> bool:
        """Rename a device — the name it carries itself.

        That is the name ``discover()`` and the Broadlink app show, NOT the key
        from the configuration. That one stays as it is; this program keeps
        addressing devices by the key.

        ``update()`` always runs before writing, and that is the whole point of
        the method: ``set_name()`` sends ``self.is_locked`` along
        (device.py:255, ``packet[0x43] = self.is_locked``). An object whose flag
        does not come from the device — e.g. built locally with ``--no-probe``,
        where the default False applies — would silently unlock a LOCKED device
        while renaming it. ``update()`` fetches name and lock status freshly,
        after which the value is real.

        Args:
            name: Key from the configuration.
            new_name: The new device name, max. 76 bytes UTF-8.

        Returns:
            True when it was renamed; False when the name was already correct.

        Raises:
            TypeError: The device cannot write its name.
            ValueError: Empty name, or longer than the buffer allows.
        """
        # set_name() builds a 0x50-byte buffer from 4 bytes of header + name +
        # padding. Too long a name runs into a bytearray(negative) there and dies
        # with a ValueError bearing no relation whatsoever to the cause.
        encoded = new_name.encode("utf-8")
        if not new_name.strip():
            raise ValueError("empty device name")
        if len(encoded) > 0x50 - 4:
            raise ValueError(f"name is {len(encoded)} bytes long, allowed are {0x50 - 4}")

        dev = self.get(name)
        if not isinstance(dev, Renameable):
            raise TypeError(f"{name} ({dev.model}) cannot write its name")

        self.with_auth(dev, dev.update)
        if dev.name == new_name:
            logger.info(f"{name}: is already called {new_name!r}, nothing to do")
            return False

        if self.dry_run:
            logger.info(
                f"DRY-RUN: {name} would be renamed from {dev.name!r} to {new_name!r} (is_locked={dev.is_locked})"
            )
            return False

        logger.info(f"{name}: {dev.name!r} -> {new_name!r} (is_locked={dev.is_locked} is preserved)")
        self.with_auth(dev, lambda: dev.set_name(new_name))
        return True

    def learn_ir(self, name: str, timeout: float = LEARN_TIMEOUT) -> bytes:
        """Learn an IR code and return it.

        What is asked for is the capability, not the model — see the protocols
        above.

        Args:
            name: Device name from the configuration.
            timeout: Seconds to wait for a button press.

        Returns:
            The raw Broadlink code (storable with ``.hex()``).

        Raises:
            TypeError: The device cannot learn IR.
            TimeoutError: Nothing arrived within ``timeout``.
        """
        dev = self.get(name)
        if not isinstance(dev, IRLearner):
            raise TypeError(f"{name} ({dev.model}) cannot learn IR")

        self.condauth(dev)
        dev.enter_learning()
        logger.info("now press the button on the remote ...")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                return dev.check_data()
            except (e.ReadError, e.StorageError):
                # Nothing in the buffer yet — the normal case while polling.
                continue

        raise TimeoutError("no IR code received")

    def learn_rf(self, name: str, timeout: float = LEARN_TIMEOUT) -> bytes:
        """Learn an RF code (rmpro/rm4pro only).

        Two phases, both needing a button press: first the device searches for
        the carrier frequency, then for the actual code.

        Args:
            name: Device name from the configuration.
            timeout: Seconds per phase.

        Returns:
            The raw Broadlink code.

        Raises:
            TypeError: The device has no RF part.
            TimeoutError: One of the two phases ran empty.
        """
        dev = self.get(name)
        if not isinstance(dev, RFLearner):
            raise TypeError(f"{name} ({dev.model}) has no RF part")

        self.condauth(dev)
        dev.sweep_frequency()
        logger.info("KEEP the button PRESSED until the frequency is found ...")
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(1)
                found, frequency = dev.check_frequency()
                if found:
                    break
            else:
                raise TimeoutError("no frequency found")
        except BaseException:
            dev.cancel_sweep_frequency()
            raise

        logger.info(f"frequency {frequency} MHz, now press the button briefly ...")
        dev.find_rf_packet(frequency)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                return dev.check_data()
            except (e.ReadError, e.StorageError):
                continue

        raise TimeoutError("no RF code received")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Deliberately a short help text instead of __doc__: typer lets rich re-wrap the
# text, which turns the example block in the module docstring into paragraph
# mush. The examples therefore live at the top of the file and per command in
# --help.
app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Broadlink RM4: address devices, learn IR/RF, send codes, compute Coolix.",
)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", envvar="BROADLINK_VERBOSE", help="DEBUG logging."),
    quiet: bool = typer.Option(False, "--quiet", "-q", envvar="BROADLINK_QUIET", help="Warnings and errors only."),
    probe: bool = typer.Option(
        True,
        "--probe/--no-probe",
        envvar="BROADLINK_PROBE",
        help="Confirm devices by unicast (yields is_locked).",
    ),
    timeout: int = typer.Option(
        PROBE_TIMEOUT, "--timeout", envvar="BROADLINK_TIMEOUT", help="Seconds per unicast attempt."
    ),
    no_rediscover: bool = typer.Option(
        False,
        "--no-rediscover",
        envvar="BROADLINK_NO_REDISCOVER",
        help="Do NOT fall back to broadcast when unreachable.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", envvar="BROADLINK_DRY_RUN", help="Send nothing, only show what would be sent."
    ),
) -> None:
    """Shared options; creates the fleet without addressing it yet.

    \f
    Everything below the ``\\f`` is hidden from ``--help`` (click convention).

    Args:
        ctx: The typer context, which carries the fleet to the subcommands.
        verbose: DEBUG instead of INFO.
        quiet: WARNING and worse only.
        probe: Confirm entries via unicast ``hello()``.
        timeout: Seconds per unicast attempt.
        no_rediscover: Suppress the broadcast fallback.
        dry_run: Send nothing.
    """
    # configure_logging() honours LOGURU_LEVEL when verbose is not set — which is
    # how --quiet gets its level without a second logging path.
    if quiet and not verbose:
        os.environ["LOGURU_LEVEL"] = "WARNING"
    configure_logging(verbose=verbose)
    print_banner("broadlinkhelper")
    logger.debug(f"configuration from {', '.join(str(p) for p in CONFIG_SOURCES)}")

    ctx.obj = BroadlinkFleet(
        probe=probe,
        probe_timeout=timeout,
        rediscover=not no_rediscover,
        dry_run=dry_run,
    )


def _fleet(ctx: typer.Context) -> BroadlinkFleet:
    """Fetch the fleet from the context.

    Args:
        ctx: The typer context.

    Returns:
        The fleet created in the callback.
    """
    fleet: BroadlinkFleet = ctx.obj
    return fleet


@app.command()
def devices(ctx: typer.Context) -> None:
    """Log in to the configured devices and list them."""
    fleet = _fleet(ctx)
    found = fleet.devices
    if not found:
        logger.error("no device reachable")
        raise typer.Exit(code=1)

    for name, dev in found.items():
        fw = fleet.with_auth(dev, dev.get_fwversion)
        typer.echo(
            f"{name:12s} {dev.model:10s} {dev.host[0]:15s} fw={fw} "
            f"locked={dev.is_locked} IR={isinstance(dev, IRLearner)} "
            f"RF={isinstance(dev, RFLearner)}"
        )
        if isinstance(dev, SensorReader):
            typer.echo(f"{'':12s} sensors: {fleet.with_auth(dev, dev.check_sensors)}")


@app.command()
def sensors(
    ctx: typer.Context,
    device: str | None = typer.Argument(None, help="Device name from broadlink.devices; all of them when omitted."),
    as_json: bool = typer.Option(False, "--json", help="One JSON object, keyed by device name."),
) -> None:
    """Read temperature and humidity off the devices that have a sensor.

    With a device name only that one is contacted; without, every configured
    device is, and those without sensors are skipped.
    """
    fleet = _fleet(ctx)
    readings: dict[str, dict[str, float]] = {}

    if device is not None:
        try:
            readings[device] = fleet.only(device).sensors(device)
        except (KeyError, TypeError) as exc:
            logger.error(str(exc.args[0] if isinstance(exc, KeyError) else exc))
            raise typer.Exit(code=1) from exc
        except e.BroadlinkException as exc:
            logger.error(f"device error: {exc}")
            raise typer.Exit(code=1) from exc
    else:
        found = fleet.devices
        if not found:
            logger.error("no device reachable")
            raise typer.Exit(code=1)
        for name, dev in found.items():
            if isinstance(dev, SensorReader):
                readings[name] = fleet.with_auth(dev, dev.check_sensors)
        if not readings:
            logger.error(f"none of {', '.join(sorted(found))} has sensors")
            raise typer.Exit(code=1)

    if as_json:
        # Keyed by name in both cases, so a script does not have to know whether
        # it asked for one device or all of them.
        typer.echo(json.dumps(readings))
        return

    for name, values in readings.items():
        typer.echo(f"{name:12s} " + "  ".join(f"{key}={value}" for key, value in values.items()))


@app.command()
def discover(
    ctx: typer.Context,
    timeout: int = typer.Option(5, "--timeout", help="Seconds to wait for answers."),
) -> None:
    """Search by broadcast and print a ready-made broadlink.devices YAML block."""
    found = _fleet(ctx).discover(timeout=timeout)
    if not found:
        logger.error("nothing found")
        raise typer.Exit(code=1)
    logger.info(f"{len(found)} device(s) found")
    typer.echo(BroadlinkFleet.config_block(found))


@app.command()
def rename(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Device name from broadlink.devices."),
    new_name: str = typer.Argument(..., help="The new name the device carries itself."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Rename without asking back."),
) -> None:
    """Rename the name the device carries itself.

    That is the name in the Broadlink app and in `discover` — the key from
    broadlink.devices, through which this program addresses the device, stays
    untouched.
    """
    fleet = _fleet(ctx)
    if not yes and not fleet.dry_run:
        typer.confirm(f"rename {device} to {new_name!r}?", abort=True)
    try:
        changed = fleet.rename(device, new_name)
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(str(exc.args[0] if isinstance(exc, KeyError) else exc))
        raise typer.Exit(code=1) from exc
    except e.BroadlinkException as exc:
        logger.error(f"device error: {exc}")
        raise typer.Exit(code=1) from exc
    if changed:
        typer.echo(new_name)


@app.command()
def send(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Device name from broadlink.devices."),
    code: str = typer.Argument(..., help="Name from CODES or raw hex."),
    repeat: int = typer.Option(1, "--repeat", "-n", min=1, help="How often to send."),
    delay: float = typer.Option(0.5, "--delay", help="Pause between repetitions (s)."),
) -> None:
    """Send a stored or raw code."""
    try:
        data = resolve_code(code)
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=2) from exc
    _dispatch(ctx, device, data, repeat, delay)


@app.command()
def climate(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Device name from broadlink.devices."),
    mode: CoolixMode = typer.Option(CoolixMode.COOL, "--mode", "-m", help="Operating mode."),
    temp: int = typer.Option(22, "--temp", "-t", help="Target temperature in degrees (17..30)."),
    fan: CoolixFan = typer.Option(CoolixFan.AUTO, "--fan", "-f", help="Fan speed."),
) -> None:
    """Compute a Coolix climate state and send it."""
    try:
        data = coolix_code(mode, temp, fan)
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=2) from exc
    _dispatch(ctx, device, data, 1, 0.0)


@app.command()
def special(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Device name from broadlink.devices."),
    name: str = typer.Argument(..., help=f"One of: {', '.join(COOLIX_SPECIAL_BY_NAME)}"),
    repeat: int = typer.Option(
        1, "--repeat", "-n", min=1, help="How often — for swing-v-step n steps are n transmissions."
    ),
    delay: float = typer.Option(0.8, "--delay", help="Pause between repetitions (s)."),
) -> None:
    """Send a Coolix special command (off, swing, sleep, turbo, ...).

    ``swing`` is a toggle and ``swing-v-step`` is one step further — the
    protocol knows no absolute vane position.
    """
    word = COOLIX_SPECIAL_BY_NAME.get(name)
    if word is None:
        logger.error(f"{name!r} unknown. Allowed: {', '.join(sorted(COOLIX_SPECIAL_BY_NAME))}")
        raise typer.Exit(code=2)
    _dispatch(ctx, device, coolix_raw_code(word), repeat, delay)


def _dispatch(ctx: typer.Context, device: str, data: bytes, repeat: int, delay: float) -> None:
    """Send, and translate device errors into clean exit codes.

    Args:
        ctx: The typer context.
        device: Device name.
        data: Raw packet.
        repeat: How often.
        delay: Pause between repetitions.
    """
    fleet = _fleet(ctx)
    try:
        fleet.send(device, data, repeat=repeat, delay=delay)
        # With --dry-run put the hex on stdout: that way the code that WOULD be
        # sent can be picked up into a file or a pipe.
        if fleet.dry_run:
            typer.echo(data.hex())
    except KeyError as exc:
        logger.error(str(exc.args[0]))
        raise typer.Exit(code=1) from exc
    except e.BroadlinkException as exc:
        logger.error(f"device error: {exc}")
        raise typer.Exit(code=1) from exc


@app.command("learn-ir")
def learn_ir_cmd(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Device name from broadlink.devices."),
    timeout: float = typer.Option(LEARN_TIMEOUT, "--timeout", help="How long to wait for the button press."),
) -> None:
    """Learn an IR code; the hex goes to stdout."""
    _learn(ctx, device, timeout, rf=False)


@app.command("learn-rf")
def learn_rf_cmd(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Device name from broadlink.devices (needs an RF part)."),
    timeout: float = typer.Option(LEARN_TIMEOUT, "--timeout", help="How long to wait per phase."),
) -> None:
    """Learn an RF code; the hex goes to stdout."""
    _learn(ctx, device, timeout, rf=True)


def _learn(ctx: typer.Context, device: str, timeout: float, *, rf: bool) -> None:
    """Shared body of learn-ir and learn-rf.

    Args:
        ctx: The typer context.
        device: Device name.
        timeout: How long to wait.
        rf: Learn RF instead of IR.
    """
    fleet = _fleet(ctx)
    learn = fleet.learn_rf if rf else fleet.learn_ir
    try:
        code = learn(device, timeout)
    except (KeyError, TypeError) as exc:
        logger.error(str(exc.args[0] if isinstance(exc, KeyError) else exc))
        raise typer.Exit(code=1) from exc
    except TimeoutError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    logger.info(f"learned: {describe_code(code)}")
    logger.info(f'line for CODES:  "NAME": "{code.hex()}",')
    typer.echo(code.hex())


@app.command()
def codes(
    ctx: typer.Context,
    grep: str | None = typer.Argument(None, help="Only names containing this text."),
    hex_only: bool = typer.Option(False, "--hex", help="Print the hex only, without interpretation."),
) -> None:
    """List the code library, each with its Coolix interpretation."""
    for name in sorted(CODES):
        if grep and grep not in name:
            continue
        if hex_only:
            typer.echo(CODES[name])
        else:
            typer.echo(f"{name:20s} {describe_code(bytes.fromhex(CODES[name]))}")


@app.command()
def decode(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Name from CODES or raw hex."),
) -> None:
    """Analyse a packet — no device, pure maths."""
    try:
        data = resolve_code(code)
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=2) from exc

    pulses = data_to_pulses(data)
    typer.echo(f"type        0x{data[0]:02X}   length {len(data)} bytes, {len(pulses)} pulses")
    typer.echo(f"meaning     {describe_code(data)}")
    word = coolix_from_data(data)
    if word is not None:
        payload = [(word >> s) & 0xFF for s in (16, 8, 0)]
        typer.echo(f"bytes       {' '.join(f'{b:02X}' for b in payload)}   (preamble / fan+0xF / temp+mode)")


@app.command()
def selftest(ctx: typer.Context) -> None:
    """Check the Coolix generator against the learned reference codes."""
    report = coolix_selftest()
    for line in report:
        typer.echo(line)
    if any("FAIL" in line for line in report):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
