"""
MIDI Note message constants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mido import Message
from picomidi.message.type import MidoMessageType

if TYPE_CHECKING:
    from picomidi.sequencer.event import SequencerEvent


class MidiNote:
    """MIDI Note message constants."""

    OFF = 0x80
    ON = 0x90
    __slots__ = ("note", "duration_ms", "velocity", "time")

    def __init__(self, note=None, duration_ms=None, velocity=None, time=0):
        self.note = note
        self.duration_ms = duration_ms
        self.velocity = velocity
        self.time = time

    def __repr__(self):
        return f"MidiNote(note={self.note}, vel={self.velocity}, dur={self.duration_ms})"

    def to_messages(self):
        yield Message(
            MidoMessageType.NOTE_ON.value, note=self.note, velocity=self.velocity, time=self.time
        )
        yield Message(
            MidoMessageType.NOTE_OFF.value, note=self.note, velocity=0, time=self.duration_ms
        )

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
            time=self.duration_ms,
        )

        return on, off


def note_on(absolute_tick: SequencerEvent) -> Message:
    return Message(
        MidoMessageType.NOTE_ON.value,
        note=absolute_tick.note,
        velocity=absolute_tick.velocity,
        channel=absolute_tick.channel,
        time=0,
    )


def note_off(absolute_tick: SequencerEvent) -> Message:
    return Message(
        MidoMessageType.NOTE_OFF.value,
        note=absolute_tick.note,
        velocity=0,
        channel=absolute_tick.channel,
        time=0,
    )
