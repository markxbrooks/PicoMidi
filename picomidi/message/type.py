"""
Types for Mido Messages
"""
from enum import Enum


class MidoMessageType(Enum):
    """Mido Message Types"""
    NOTE_ON = "note_on"
    NOTE_OFF = "note_off"
    CONTROL_CHANGE = "control_change"
    PROGRAM_CHANGE = "program_change"
    PITCH_WHEEL = "pitchwheel"
    AFTERTOUCH = "aftertouch"
    POLYTOUCH = "polytouch"
    SET_TEMPO = "set_tempo"
