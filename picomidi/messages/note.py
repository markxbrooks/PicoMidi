"""
MIDI Note message constants.
"""
from mido import Message

from picomidi.message.type import MidoMessageType


class MidiNote:
    """MIDI Note message constants."""
    OFF = 0x80
    ON = 0x90
    __slots__ = ("note", "duration", "velocity", "time")

    def __init__(self, note=None, duration=None, velocity=None, time=0):
        self.note = note
        self.duration = duration
        self.velocity = velocity
        self.time = time

    def __repr__(self):
        return f"MidiNote(note={self.note}, vel={self.velocity}, dur={self.duration})"

    def to_messages(self):
        yield Message(MidoMessageType.NOTE_ON.value, note=self.note, velocity=self.velocity, time=self.time)
        yield Message(MidoMessageType.NOTE_OFF.value, note=self.note, velocity=0, time=self.duration)

    def to_on_off_pair(self):
        """Convert spec to NOTE_ON / NOTE_OFF messages."""

        on = Message(
            MidoMessageType.NOTE_ON.value,
            note=self.note,
            velocity=self.velocity,
            time=self.time,
        )

        off = Message(
            MidoMessageType.NOTE_OFF.value,
            note=self.note,
            velocity=0,
            time=self.duration,
        )

        return on, off