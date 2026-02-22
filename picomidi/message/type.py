"""
Types for Mido Messages
"""


class MidoMessageType:
    """Mido Message Types"""
    NOTE_ON: str = "note_on"
    NOTE_OFF: str = "note_off"
    CONTROL_CHANGE: str = "control_change"
    PROGRAM_CHANGE: str = "program_change"
    PITCH_WHEEL: str = "pitchwheel"
    AFTERTOUCH: str = "aftertouch"
    POLYTOUCH: str = "polytouch"
    SET_TEMPO: str = "set_tempo"
