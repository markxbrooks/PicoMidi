"""
Standard MIDI Protocol Constants
"""

from picomidi.core.channel_legacy import MidiChannel
from picomidi.core.tempo import MidiTempo
from picomidi.core.value import MidiValue
from picomidi.messages import (Aftertouch, ControlChange, MidiNote, PitchBend,
                               ProgramChange, Song, SysExByte)
from picomidi.misc import MidiMisc


class Midi:
    """Standard MIDI protocol constants."""

    VALUE = MidiValue
    NOTE = MidiNote
    SYSEX = SysExByte
    CC = ControlChange
    PC = ProgramChange
    AFTERTOUCH = Aftertouch
    SONG = Song
    PITCH_BEND = PitchBend
    TEMPO = MidiTempo
    CHANNEL = MidiChannel
    MISC = MidiMisc
