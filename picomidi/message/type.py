"""
Types for Mido Messages
"""

from enum import Enum


class MidoMessageAttrs:
    """Mido Message Attributes"""
    CHANNEL = "channel"
    NOTE = "note"


class MidoMessageType(str, Enum):
    """
    Mido message type strings.

    ``str`` mixin so values compare equal to mido's ``msg.type`` and can be
    passed directly to ``mido.Message(...)``.
    """

    CLOCK = "clock"
    SYSEX = "sysex"
    NOTE_ON = "note_on"
    NOTE_OFF = "note_off"
    CONTROL_CHANGE = "control_change"
    PROGRAM_CHANGE = "program_change"
    PITCH_WHEEL = "pitchwheel"
    AFTERTOUCH = "aftertouch"
    POLYTOUCH = "polytouch"
    SET_TEMPO = "set_tempo"


class MidoMetaMessageType(str, Enum):
    """Mido meta message type strings."""

    TRACK_NAME = "track_name"
    END_OF_TRACK = "end_of_track"
    SET_TEMPO = "set_tempo"
    TIME_SIGNATURE = "time_signature"
    SEQUENCE_NAME = "sequence_name"
