"""
Playback Engine

ScheduledEvent Example Usage:
==============
>>> from picomidi.message.type import MidoMessageType
>>> event = ScheduledEvent(absolute_tick=344, message=mido.Message(MidoMessageType.NOTE_ON.value), track_index=1)
>>> event
ScheduledEvent(absolute_tick=344, message=Message('note_on', channel=0, note=0, velocity=64, time=0), track_index=1)
"""

import bisect
import math
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, List, Optional

import mido

from picomidi import MidiTempo

CC_BANK_SELECT_MSB = 0
CC_MODULATION = 1
CC_BANK_SELECT_LSB = 32
CC_SUSTAIN = 64
CC_ALL_SOUND_OFF = 120
CC_ALL_NOTES_OFF = 123
SUSTAIN_ON_THRESHOLD = 64

# Not restored on seek: RPN/NRPN data entry is order-dependent, 120+ are channel mode messages
UNCHASED_CONTROLS = frozenset({6, 38, 96, 97, 98, 99, 100, 101})
FIRST_CHANNEL_MODE_CONTROL = 120
# Restored to these values on seek when the target position has no earlier value
CHASE_DEFAULTS = {("cc", CC_MODULATION): 0, ("cc", CC_SUSTAIN): 0, ("pitchwheel",): 0}

MIDI_CLOCKS_PER_BEAT = 24
SONG_POSITION_PER_BEAT = 4  # Song Position Pointer counts 16th notes
SONG_POSITION_MAX = 16383

SPEED_MIN = 0.25
SPEED_MAX = 2.0


@dataclass
class ScheduledEvent:
    """A MIDI event at a given file tick, for one track."""

    absolute_tick: int
    message: mido.Message
    track_index: int = 0


class TransportState(Enum):
    """Transport State"""

    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


class PlaybackEngine:
    """
    Pure MIDI playback engine.

    No Qt. No UI. No threads. Drives playback using absolute tick scheduling
    with a single merged event list and segment-wise tempo.

    How the engine is driven:
    - The owner (e.g. a worker or controller) loads a file with load_file(),
      then calls start(start_tick) to begin playback.
    - On a timer or tick (e.g. every 5–20 ms), the owner calls
      process_until_now(). The engine advances over events whose scheduled
      time has passed and invokes the on_event callback for each that passes
      _should_send (mute/suppress). It returns True exactly once, on the call
      where playback reaches the end of the file.
    - The owner sets engine.on_event to a callable (e.g. lambda msg: send(msg))
      so that messages are actually sent (e.g. to a MIDI port).
    - Transport: pause()/resume() keep the position; stop() keeps the position
      (call seek_tick(0) to rewind); seek_tick()/seek_seconds() work in any
      state and leave the state unchanged.
    - The engine tracks the notes (and sustain pedals) it has sounded, and
      releases them on pause, stop, seek and mute so nothing hangs.
    - Unmuting a track or channel restores its controller state (as a seek
      does), so it plays with the program, volume and bend it would have had.
    - set_track_velocity_scale() scales a track's note-on velocities as they
      are sent (the file is not changed).
    - Starting or seeking mid-file restores ("chases") program/bank, controller
      and pitch-bend values from earlier in the file, sending only values that
      differ from what the engine last sent.
    - Optional: loop (whole file or A–B), playback speed, MIDI clock out.

    The engine is not thread-safe; an owner driving it from another thread
    must serialise calls (e.g. with a lock).
    """

    def __init__(self):
        self.midi_file: Optional[mido.MidiFile] = None
        self.ticks_per_beat: int = 480

        self.tempo_us: int = MidiTempo.BPM_120_USEC  # default 120 BPM
        self._tempo_map: dict[int, int] = {}
        self._tempo_ticks: List[int] = []  # sorted keys of _tempo_map
        self._tick_to_time: List[tuple[int, float]] = (
            []
        )  # (tick, cumulative seconds from 0)
        self._time_map_ticks: List[int] = []
        self._time_map_seconds: List[float] = []
        # (tick, numerator, denominator), sorted by tick
        self._time_signatures: List[tuple[int, int, int]] = [(0, 4, 4)]

        self.events: List[ScheduledEvent] = []
        self._event_ticks: List[int] = (
            []
        )  # Cached ticks for binary search; set in _build_event_list
        self._event_seconds: List[float] = []  # Absolute time of each event
        self.event_index: int = 0
        self.length_ticks: int = 0

        self.start_tick: int = 0
        self._start_time: float = 0.0  # clock time when playback (re)started
        self._start_seconds: float = 0.0  # file position at _start_time
        self._position_seconds: float = 0.0  # file position while not playing
        self.speed: float = 1.0

        self.loop_enabled: bool = False
        # A–B loop range in ticks; None loops the whole file
        self.loop_range: Optional[tuple[int, int]] = None

        self.send_clock: bool = False
        self._next_clock_tick: float = 0.0
        self._clock_position_dirty = False  # Song Position Pointer needed on next run

        self._muted_tracks: set[int] = set()
        self._muted_channels: set[int] = set()
        # track_index -> note-on velocity factor (absent = 1.0)
        self._velocity_scales: dict[int, float] = {}

        # (channel, note) -> tracks that sounded it and have not released it
        self._held_notes: dict[tuple[int, int], set[int]] = {}
        # channel -> track_index for sustain pedals left down
        self._sustained_channels: dict[int, int] = {}
        # Last value sent for each chased key, e.g. ("program", ch) -> 5
        self._sent_controllers: dict[tuple, int] = {}
        self._chase_pending = False

        self.suppress_program_changes = False
        self.suppress_control_changes = False

        self._state = TransportState.STOPPED
        self._is_playing = False  # True only when _state == PLAYING

        # callback hook (UI/worker attaches to this)
        self.on_event: Optional[Callable[[mido.Message], Any]] = None

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    @property
    def state(self) -> TransportState:
        return self._state

    def _set_state(self, new_state: TransportState) -> None:
        if self._state == new_state:
            return

        self._state = new_state
        if new_state == TransportState.PLAYING:
            self._enter_playing()
        elif new_state == TransportState.STOPPED:
            self._enter_stopped()
        elif new_state == TransportState.PAUSED:
            self._enter_paused()

    def _enter_playing(self) -> None:
        self._is_playing = True

    def _enter_stopped(self) -> None:
        self._is_playing = False

    def _enter_paused(self) -> None:
        self._is_playing = False

    def load_file(self, midi_file: mido.MidiFile) -> None:
        self.midi_file = midi_file
        self.ticks_per_beat = midi_file.ticks_per_beat

        self._build_tempo_map()
        self._build_event_list()
        self._build_time_map()
        self._event_seconds = [self._tick_to_seconds(t) for t in self._event_ticks]
        self.loop_range = None
        self.reset()

    def reset(self) -> None:
        """Reset playback state (position, play flag). Mute/suppress settings are unchanged."""
        self.event_index = 0
        self.start_tick = 0
        self._start_time = 0.0
        self._start_seconds = 0.0
        self._position_seconds = 0.0
        self._held_notes.clear()
        self._sustained_channels.clear()
        self._sent_controllers.clear()
        self._chase_pending = False
        self._clock_position_dirty = False
        self._set_state(TransportState.STOPPED)

    def _build_tempo_map(self) -> None:
        """Build global tempo and time-signature maps from all tracks (Type 0/1: merge by tick)."""
        self._tempo_map.clear()
        tempo_events: List[tuple[int, int]] = []
        signatures: dict[int, tuple[int, int]] = {}

        for track in self.midi_file.tracks:
            absolute_tick = 0
            for msg in track:
                absolute_tick += msg.time
                if msg.type == "set_tempo":
                    tempo_events.append((absolute_tick, msg.tempo))
                elif msg.type == "time_signature":
                    signatures[absolute_tick] = (msg.numerator, msg.denominator)

        tempo_events.sort(key=lambda x: x[0])
        for tick, tempo in tempo_events:
            self._tempo_map[tick] = tempo

        if 0 not in self._tempo_map:
            self._tempo_map[0] = MidiTempo.BPM_120_USEC  # default

        self._tempo_ticks = sorted(self._tempo_map)

        signatures.setdefault(0, (4, 4))
        self._time_signatures = [
            (tick, num, den) for tick, (num, den) in sorted(signatures.items())
        ]

    def _build_event_list(self) -> None:
        self.events.clear()
        self.length_ticks = 0

        for track_index, track in enumerate(self.midi_file.tracks):
            absolute_tick = 0

            for msg in track:
                absolute_tick += msg.time

                if not msg.is_meta:
                    self.events.append(
                        ScheduledEvent(
                            absolute_tick=absolute_tick,
                            message=msg.copy(),
                            track_index=track_index,
                        )
                    )

            # Track length includes trailing meta events such as end_of_track
            self.length_ticks = max(self.length_ticks, absolute_tick)

        self.events.sort(key=lambda e: e.absolute_tick)

        # cache ticks for binary search
        self._event_ticks = [e.absolute_tick for e in self.events]

    # --- Position -------------------------------------------------------------

    @property
    def duration_seconds(self) -> float:
        """Length of the loaded file in seconds (0.0 when nothing is loaded)."""
        return self._tick_to_seconds(self.length_ticks)

    @property
    def position_seconds(self) -> float:
        """Current file position in seconds, valid in every transport state."""
        if self._is_playing:
            return min(self._running_position(), self.duration_seconds)
        return self._position_seconds

    def _running_position(self) -> float:
        return self._start_seconds + (self._now() - self._start_time) * self.speed

    @property
    def position_tick(self) -> int:
        """Current file position in ticks, valid in every transport state."""
        return self.seconds_to_tick(self.position_seconds)

    def tick_to_seconds(self, tick: int) -> float:
        """Seconds from the start of the file to *tick* (tempo-map aware)."""
        return self._tick_to_seconds(tick)

    def seconds_to_tick(self, seconds: float) -> int:
        """File tick at *seconds* from the start of the file (tempo-map aware)."""
        if seconds <= 0 or not self._time_map_seconds:
            return 0
        idx = bisect.bisect_right(self._time_map_seconds, seconds) - 1
        seg_tick = self._time_map_ticks[idx]
        seg_time = self._time_map_seconds[idx]
        tempo = self._get_tempo_at_tick(seg_tick)
        return seg_tick + int(
            mido.second2tick(seconds - seg_time, self.ticks_per_beat, tempo)
        )

    def tempo_at_tick(self, tick: int) -> int:
        """Tempo (µs per beat) in effect at *tick*."""
        return self._get_tempo_at_tick(tick)

    # --- Bars and beats -------------------------------------------------------

    def _signature_segments(self):
        """Yield (start_tick, end_tick | None, bar_ticks, beat_ticks, first_bar)."""
        first_bar = 1
        for i, (tick, num, den) in enumerate(self._time_signatures):
            end = (
                self._time_signatures[i + 1][0]
                if i + 1 < len(self._time_signatures)
                else None
            )
            beat_ticks = self.ticks_per_beat * 4 / den
            bar_ticks = beat_ticks * num
            yield tick, end, bar_ticks, beat_ticks, first_bar
            if end is not None:
                first_bar += math.ceil((end - tick) / bar_ticks)

    def bar_beat_at_tick(self, tick: int) -> tuple[int, int]:
        """1-based (bar, beat) at *tick*, following the file's time signatures."""
        for start, end, bar_ticks, beat_ticks, first_bar in self._signature_segments():
            if end is None or tick < end:
                rel = max(0, tick - start)
                bars = int(rel // bar_ticks)
                beat = int((rel - bars * bar_ticks) // beat_ticks) + 1
                return first_bar + bars, beat
        return 1, 1  # unreachable: the last segment is open-ended

    def time_signature_at_tick(self, tick: int) -> tuple[int, int]:
        """(numerator, denominator) in effect at *tick*."""
        idx = bisect.bisect_right([t for t, _, _ in self._time_signatures], tick) - 1
        _, num, den = self._time_signatures[max(0, idx)]
        return num, den

    def nearest_bar_tick(self, tick: int) -> int:
        """Tick of the bar line nearest to *tick*."""
        for start, end, bar_ticks, _, _ in self._signature_segments():
            if end is None or tick < end:
                bars = (max(0, tick - start)) // bar_ticks
                before = start + bars * bar_ticks
                after = before + bar_ticks
                if end is not None:
                    after = min(after, end)
                return int(round(before if tick - before <= after - tick else after))
        return tick

    # --- Transport ------------------------------------------------------------

    def start(self, start_tick: int = 0) -> None:
        self._position_at_tick(start_tick)
        self._start_time = self._now()
        self._set_state(TransportState.PLAYING)
        if start_tick > 0:
            self._chase()
        self._chase_pending = False
        if self.send_clock:
            if start_tick > 0:
                self._send_song_position()
                self._send(mido.Message("continue"))
            else:
                self._next_clock_tick = 0.0
                self._send(mido.Message("start"))
        self._clock_position_dirty = False

    def _find_start_index(self, start_tick: int) -> int:
        return bisect.bisect_left(self._event_ticks, start_tick)

    def _position_at_tick(self, tick: int) -> None:
        """Move the play position to *tick* without changing the transport state."""
        self.start_tick = tick
        self.event_index = self._find_start_index(tick)
        self._start_seconds = self._tick_to_seconds(tick)
        self._position_seconds = self._start_seconds

    def stop(self) -> None:
        """Stop playback; the position is kept (seek_tick(0) rewinds)."""
        was_running = self._is_playing
        if was_running:
            self._hold_position()
        self._set_state(TransportState.STOPPED)
        self.release_held_notes()
        # A later play re-establishes controller state from scratch
        self._sent_controllers.clear()
        if was_running and self.send_clock:
            self._send(mido.Message("stop"))

    def pause(self) -> None:
        """Pause playback; position is preserved and sounding notes are released."""
        if not self._is_playing:
            return
        self._hold_position()
        self._set_state(TransportState.PAUSED)
        self.release_held_notes()
        if self.send_clock:
            self._send(mido.Message("stop"))

    def _hold_position(self) -> None:
        """Freeze the running position; start_tick then names the stopped position."""
        self._position_seconds = self.position_seconds
        self.start_tick = self.seconds_to_tick(self._position_seconds)

    def resume(self) -> None:
        """Resume from the paused position without re-sending past events."""
        if self._state != TransportState.PAUSED:
            return
        self._start_seconds = self._position_seconds
        self._start_time = self._now()
        self._set_state(TransportState.PLAYING)
        if self._chase_pending:
            self._chase()
            self._chase_pending = False
        if self.send_clock:
            if self._clock_position_dirty:
                self._send_song_position()
            self._send(mido.Message("continue"))
        self._clock_position_dirty = False

    def seek_tick(self, tick: int) -> None:
        """Move the position to *tick* in any state; the state is unchanged."""
        tick = max(0, min(int(tick), self.length_ticks))
        self.release_held_notes()
        playing = self._is_playing
        if playing and self.send_clock:
            self._send(mido.Message("stop"))
        self._position_at_tick(tick)
        self._start_time = self._now()
        if playing:
            self._chase()
            if self.send_clock:
                self._send_song_position()
                self._send(mido.Message("continue"))
        else:
            self._chase_pending = True
            self._clock_position_dirty = True

    def seek_seconds(self, seconds: float) -> None:
        """Move to *seconds* in any state; the state is unchanged."""
        self.seek_tick(self.seconds_to_tick(seconds))

    def scrub_to_tick(self, tick: int) -> None:
        """Alias for seek_tick (kept for existing callers)."""
        self.seek_tick(tick)

    def set_speed(self, speed: float) -> None:
        """Set playback speed (1.0 = file tempo), keeping the current position."""
        speed = max(SPEED_MIN, min(SPEED_MAX, float(speed)))
        if self._is_playing:
            self._start_seconds = self._running_position()
            self._start_time = self._now()
        self.speed = speed

    def set_loop(
        self, enabled: bool, loop_range: Optional[tuple[int, int]] = None
    ) -> None:
        """
        Enable or disable looping.

        :param enabled: Loop when the end of the range is reached
        :param loop_range: (start_tick, end_tick) for an A–B loop; None loops the whole file
        """
        if loop_range is not None:
            start, end = sorted(int(t) for t in loop_range)
            loop_range = (max(0, start), min(end, self.length_ticks))
            if loop_range[1] <= loop_range[0]:
                loop_range = None
        self.loop_enabled = enabled
        self.loop_range = loop_range

    def set_send_clock(self, enabled: bool) -> None:
        """Turn MIDI clock out on or off; takes effect immediately while playing."""
        if enabled == self.send_clock:
            return
        if self._is_playing:
            if enabled:
                self.send_clock = True
                self.start_tick = self.seconds_to_tick(self.position_seconds)
                self._send_song_position()
                self._send(mido.Message("continue"))
                return
            self._send(mido.Message("stop"))
        self.send_clock = enabled
        self._clock_position_dirty = True

    # --- Timing ---------------------------------------------------------------

    def _build_time_map(self) -> None:
        """Build cumulative time (seconds) at each tempo change for segment-wise tick→time."""
        if not self._tempo_map:
            self._tick_to_time = [(0, 0.0)]
        else:
            ticks_sorted = sorted(self._tempo_map.keys())
            out: List[tuple[int, float]] = []
            time_sec = 0.0
            prev_tick = 0
            prev_tempo = (
                self._tempo_map[0] if 0 in self._tempo_map else MidiTempo.BPM_120_USEC
            )
            for t in ticks_sorted:
                if t == 0:
                    out.append((0, 0.0))
                    prev_tempo = self._tempo_map[0]
                    continue
                segment_ticks = t - prev_tick
                time_sec += mido.tick2second(
                    segment_ticks, self.ticks_per_beat, prev_tempo
                )
                out.append((t, time_sec))
                prev_tick = t
                prev_tempo = self._tempo_map[t]
            self._tick_to_time = out if out else [(0, 0.0)]
        self._time_map_ticks = [t for t, _ in self._tick_to_time]
        self._time_map_seconds = [s for _, s in self._tick_to_time]

    def _tick_to_seconds(self, tick: float) -> float:
        """Seconds from 0 to tick using segment-wise tempo (variable tempo safe)."""
        if tick <= 0:
            return 0.0
        if not self._time_map_ticks:
            return mido.tick2second(tick, self.ticks_per_beat, self._get_tempo_at_tick(0))
        # Find segment: last (t, time) where t <= tick
        idx = bisect.bisect_right(self._time_map_ticks, tick) - 1
        if idx < 0:
            return 0.0
        seg_tick, seg_time = self._tick_to_time[idx]
        tempo = self._get_tempo_at_tick(seg_tick)
        delta_ticks = tick - seg_tick
        return seg_time + mido.tick2second(delta_ticks, self.ticks_per_beat, tempo)

    def _get_tempo_at_tick(self, tick: int) -> int:
        idx = bisect.bisect_right(self._tempo_ticks, tick) - 1
        if idx < 0:
            return MidiTempo.BPM_120_USEC
        return self._tempo_map[self._tempo_ticks[idx]]

    def _loop_end_seconds(self) -> Optional[float]:
        """End of the active loop in seconds, or None when not looping."""
        if not self.loop_enabled or self.length_ticks <= 0:
            return None
        end_tick = self.loop_range[1] if self.loop_range else self.length_ticks
        end = self._tick_to_seconds(end_tick)
        # Playing from beyond the loop end plays on to the end of the file
        if self._start_seconds >= end:
            return None
        return end

    def process_until_now(self) -> bool:
        """
        Send every event (and MIDI clock) that is due.

        :return: True on the call where playback reaches the end of the file
            (the engine is then STOPPED with its position at the end).
        """
        if not self._is_playing:
            return False

        position = self._running_position()
        loop_end = self._loop_end_seconds()
        self._send_due(position, loop_end)
        if loop_end is not None and position >= loop_end:
            self._wrap_loop(position - loop_end)
            self._send_due(self._running_position(), self._loop_end_seconds())
            return False

        if self.event_index >= len(self.events) and position >= self.duration_seconds:
            self._position_seconds = self.duration_seconds
            self.start_tick = self.length_ticks
            self._set_state(TransportState.STOPPED)
            self.release_held_notes()
            self._sent_controllers.clear()
            if self.send_clock:
                self._send(mido.Message("stop"))
            return True
        return False

    def _send_due(self, position: float, limit: Optional[float]) -> None:
        """Send events and clock due at *position*, in time order, before *limit*."""
        events_count = len(self.events)
        clock_time = self._next_clock_seconds()
        while True:
            event_time = (
                self._event_seconds[self.event_index]
                if self.event_index < events_count
                else math.inf
            )
            due = min(event_time, clock_time)
            if due == math.inf or due > position or (limit is not None and due >= limit):
                return
            if clock_time <= event_time:
                self._send(mido.Message("clock"))
                self._next_clock_tick += self.ticks_per_beat / MIDI_CLOCKS_PER_BEAT
                clock_time = self._next_clock_seconds()
            else:
                self._dispatch(self.events[self.event_index])
                self.event_index += 1

    def _next_clock_seconds(self) -> float:
        if not self.send_clock:
            return math.inf
        return self._tick_to_seconds(self._next_clock_tick)

    def _wrap_loop(self, overshoot: float) -> None:
        """Jump from the loop end back to the loop start."""
        end = self._loop_end_seconds()
        start_tick = self.loop_range[0] if self.loop_range else 0
        self.release_held_notes()
        if self.send_clock:
            self._send(mido.Message("stop"))
        self._position_at_tick(start_tick)
        if end is None or overshoot > end - self._start_seconds:
            overshoot = 0.0
        self._start_time = self._now() - overshoot / self.speed
        self._chase()
        if self.send_clock:
            self._send_song_position()
            self._send(mido.Message("continue"))

    def _send_song_position(self) -> None:
        """Send Song Position Pointer for start_tick and align the next clock to it."""
        sixteenth = self.ticks_per_beat / SONG_POSITION_PER_BEAT
        position = min(int(self.start_tick // sixteenth), SONG_POSITION_MAX)
        self._send(mido.Message("songpos", pos=position))
        clock_ticks = self.ticks_per_beat / MIDI_CLOCKS_PER_BEAT
        self._next_clock_tick = position * sixteenth
        while self._next_clock_tick < self.start_tick:
            self._next_clock_tick += clock_ticks

    # --- Chase ----------------------------------------------------------------

    @staticmethod
    def _chase_key(msg: mido.Message) -> Optional[tuple]:
        if msg.type == "program_change":
            return ("program", msg.channel)
        if msg.type == "pitchwheel":
            return ("pitchwheel", msg.channel)
        if (
            msg.type == "control_change"
            and msg.control not in UNCHASED_CONTROLS
            and msg.control < FIRST_CHANNEL_MODE_CONTROL
        ):
            return ("cc", msg.channel, msg.control)
        return None

    @staticmethod
    def _chase_value(msg: mido.Message) -> int:
        if msg.type == "program_change":
            return msg.program
        if msg.type == "pitchwheel":
            return msg.pitch
        return msg.value

    def _chase(self) -> None:
        """Send the controller state in effect at the position (changes only)."""
        target: dict[tuple, ScheduledEvent] = {}
        for event in self.events[: self.event_index]:
            key = self._chase_key(event.message)
            if key is not None and self._should_send(event):
                target[key] = event

        def differs(key: tuple) -> bool:
            event = target.get(key)
            return event is not None and self._sent_controllers.get(
                key
            ) != self._chase_value(event.message)

        channels = {key[1] for key in target} | {key[1] for key in self._sent_controllers}
        for ch in sorted(channels):
            bank_keys = [("cc", ch, CC_BANK_SELECT_MSB), ("cc", ch, CC_BANK_SELECT_LSB)]
            program_key = ("program", ch)
            if program_key in target and any(
                differs(k) for k in bank_keys + [program_key]
            ):
                for key in bank_keys + [program_key]:
                    if key in target:
                        self._dispatch(target[key])
            for key in sorted(k for k in target if k[0] == "cc" and k[1] == ch):
                if key not in bank_keys and differs(key):
                    self._dispatch(target[key])
            if differs(("pitchwheel", ch)):
                self._dispatch(target[("pitchwheel", ch)])
            self._restore_defaults(ch, target)

    def _restore_defaults(self, channel: int, target: dict) -> None:
        """Reset bend/modulation/sustain left non-default where the file has no value yet."""
        for kind, default in CHASE_DEFAULTS.items():
            key = (kind[0], channel, *kind[1:])
            if key in target or self._sent_controllers.get(key, default) == default:
                continue
            if kind[0] == "pitchwheel":
                msg = mido.Message("pitchwheel", channel=channel, pitch=default)
            else:
                msg = mido.Message(
                    "control_change", channel=channel, control=kind[1], value=default
                )
            self._send(msg)
            self._sent_controllers[key] = default
            if kind == ("cc", CC_SUSTAIN):
                self._sustained_channels.pop(channel, None)

    # --- Sending --------------------------------------------------------------

    def _dispatch(self, event: ScheduledEvent) -> None:
        if not self._should_send(event):
            return
        self._track_sounding(event)
        key = self._chase_key(event.message)
        if key is not None:
            self._sent_controllers[key] = self._chase_value(event.message)
        if self.on_event:
            self.on_event(self._scaled(event))

    def _scaled(self, event: ScheduledEvent) -> mido.Message:
        """The event's message with the track's velocity scale applied."""
        msg = event.message
        scale = self._velocity_scales.get(event.track_index)
        if scale is None or msg.type != "note_on" or msg.velocity == 0:
            return msg
        # At least 1: velocity 0 would be a note-off
        return msg.copy(velocity=max(1, min(127, round(msg.velocity * scale))))

    def _track_sounding(self, event: ScheduledEvent) -> None:
        """Remember which notes and sustain pedals are down so they can be released."""
        msg = event.message
        key = (getattr(msg, "channel", None), getattr(msg, "note", None))
        if msg.type == "note_on" and msg.velocity > 0:
            self._held_notes.setdefault(key, set()).add(event.track_index)
        elif msg.type in ("note_off", "note_on"):
            tracks = self._held_notes.get(key)
            if tracks is not None:
                tracks.discard(event.track_index)
                if not tracks:
                    del self._held_notes[key]
        elif msg.type == "control_change" and msg.control == CC_SUSTAIN:
            if msg.value >= SUSTAIN_ON_THRESHOLD:
                self._sustained_channels[msg.channel] = event.track_index
            else:
                self._sustained_channels.pop(msg.channel, None)

    def _send(self, msg: mido.Message) -> None:
        if self.on_event:
            self.on_event(msg)

    def release_held_notes(
        self, channel: Optional[int] = None, track_index: Optional[int] = None
    ) -> None:
        """
        Send note-off (and sustain-off) for what the engine has sounded.

        :param channel: Only release notes on this channel (0-15)
        :param track_index: Only release notes sounded by this track
        """

        def matches(ch: int, track: int) -> bool:
            return (channel is None or ch == channel) and (
                track_index is None or track == track_index
            )

        for (ch, note), tracks in list(self._held_notes.items()):
            released = {track for track in tracks if matches(ch, track)}
            if not released:
                continue
            self._send(mido.Message("note_off", channel=ch, note=note, velocity=0))
            tracks -= released
            if not tracks:
                del self._held_notes[(ch, note)]
        for ch, track in list(self._sustained_channels.items()):
            if matches(ch, track):
                self._send(
                    mido.Message(
                        "control_change", channel=ch, control=CC_SUSTAIN, value=0
                    )
                )
                del self._sustained_channels[ch]
                self._sent_controllers[("cc", ch, CC_SUSTAIN)] = 0

    def all_notes_off(self) -> None:
        """Panic: release tracked notes, then All Sound Off / All Notes Off on all channels."""
        self.release_held_notes()
        for ch in range(16):
            self._send(
                mido.Message(
                    "control_change", channel=ch, control=CC_ALL_SOUND_OFF, value=0
                )
            )
            self._send(
                mido.Message(
                    "control_change", channel=ch, control=CC_ALL_NOTES_OFF, value=0
                )
            )

    def _should_send(self, event: ScheduledEvent) -> bool:
        msg = event.message

        if event.track_index in self._muted_tracks:
            return False

        channel = getattr(msg, "channel", None)
        if channel is not None and channel in self._muted_channels:
            return False

        if self.suppress_program_changes and msg.type == "program_change":
            return False

        if self.suppress_control_changes and msg.type == "control_change":
            return False

        return True

    def mute_channel(self, channel: int, muted: bool) -> None:
        """Mute or unmute a channel (0-15); calls that change nothing do nothing."""
        if muted == (channel in self._muted_channels):
            return
        if muted:
            self._muted_channels.add(channel)
            self.release_held_notes(channel=channel)
        else:
            self._muted_channels.discard(channel)
            self._restore_unmuted()

    def mute_track(self, track_index: int, muted: bool) -> None:
        """Mute or unmute a track; calls that change nothing do nothing."""
        if muted == (track_index in self._muted_tracks):
            return
        if muted:
            self._muted_tracks.add(track_index)
            self.release_held_notes(track_index=track_index)
        else:
            self._muted_tracks.discard(track_index)
            self._restore_unmuted()

    def set_track_velocity_scale(self, track_index: int, scale: float) -> None:
        """
        Scale a track's note-on velocities by *scale* (1.0 = as in the file).

        Applies to notes sent from now on; sounding notes keep their velocity.
        """
        if scale == 1.0:
            self._velocity_scales.pop(track_index, None)
        else:
            self._velocity_scales[track_index] = max(0.0, float(scale))

    def _restore_unmuted(self) -> None:
        """
        Send the controller state the unmuted events left (program, CCs, bend).

        Muted events were not sent, so the synth still has what the other
        tracks (or nothing) set; chasing sends only what differs.
        """
        if self._is_playing:
            self._chase()
        elif self.midi_file is not None:
            self._chase_pending = True
