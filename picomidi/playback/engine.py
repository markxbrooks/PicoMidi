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
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, List, Optional

import mido
from mido import Message

from picomidi import MidiTempo, Channel
from picomidi.message.type import MidoMessageType, MidoMessageAttrs

CC_BANK_SELECT_MSB = 0
CC_MODULATION = 1
CC_DATA_ENTRY_MSB = 6
CC_VOLUME = 7
CC_PAN = 10
CC_EXPRESSION = 11
CC_BANK_SELECT_LSB = 32
CC_DATA_ENTRY_LSB = 38
CC_SUSTAIN = 64
CC_DATA_INCREMENT = 96
CC_DATA_DECREMENT = 97
CC_NRPN_LSB = 98
CC_NRPN_MSB = 99
CC_RPN_LSB = 100
CC_RPN_MSB = 101
CC_ALL_SOUND_OFF = 120
CC_ALL_NOTES_OFF = 123
SUSTAIN_ON_THRESHOLD = 64
# Null RPN/NRPN selection (no parameter)
PARAM_NULL = 127
# GM default pitch-bend sensitivity: 2 semitones (RPN 0,0)
RPN_PITCH_BEND_SENSITIVITY = (0, 0)
RPN_DEFAULTS: dict[tuple[int, int], tuple[int, int]] = {
    RPN_PITCH_BEND_SENSITIVITY: (2, 0),
}

# Handled by ParamEntryState (order-dependent); 120+ are channel mode messages
UNCHASED_CONTROLS = frozenset(
    {
        CC_DATA_ENTRY_MSB,
        CC_DATA_ENTRY_LSB,
        CC_DATA_INCREMENT,
        CC_DATA_DECREMENT,
        CC_NRPN_LSB,
        CC_NRPN_MSB,
        CC_RPN_LSB,
        CC_RPN_MSB,
    }
)
FIRST_CHANNEL_MODE_CONTROL = 120
# Baseline per channel when the file has no earlier value (GM-ish defaults)
CHASE_DEFAULTS = {
    ("cc", CC_BANK_SELECT_MSB): 0,
    ("cc", CC_BANK_SELECT_LSB): 0,
    ("program",): 0,
    ("cc", CC_VOLUME): 100,
    ("cc", CC_PAN): 64,
    ("cc", CC_EXPRESSION): 127,
    ("cc", CC_MODULATION): 0,
    ("cc", CC_SUSTAIN): 0,
    ("pitchwheel",): 0,
}

MIDI_CLOCKS_PER_BEAT = 24
SONG_POSITION_PER_BEAT = 4  # Song Position Pointer counts 16th notes
SONG_POSITION_MAX = 16383
CONTROLLER_SNAPSHOT_INTERVAL = 512

SPEED_MIN = 0.25
SPEED_MAX = 2.0


@dataclass
class ScheduledEvent:
    """A MIDI event at a given file tick, for one track."""

    absolute_tick: int
    message: mido.Message
    track_index: int = 0


@dataclass
class ParamEntryState:
    """
    Per-channel RPN/NRPN selection and registered parameter values.

    Data Entry / Inc / Dec apply to the currently selected RPN or NRPN.
    Chase restores each known parameter as an ordered CC sequence
    (select MSB/LSB, then Data Entry MSB/LSB).
    """

    rpn_msb: int = PARAM_NULL
    rpn_lsb: int = PARAM_NULL
    nrpn_msb: int = PARAM_NULL
    nrpn_lsb: int = PARAM_NULL
    active: Optional[str] = None  # "rpn", "nrpn", or None
    # (msb, lsb) -> (data_msb, data_lsb)
    rpn_values: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    nrpn_values: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    # track that last touched this channel's param-entry state (for mute filter)
    last_track: int = 0
    # track that last set each parameter value
    rpn_tracks: dict[tuple[int, int], int] = field(default_factory=dict)
    nrpn_tracks: dict[tuple[int, int], int] = field(default_factory=dict)

    def copy(self) -> "ParamEntryState":
        return ParamEntryState(
            rpn_msb=self.rpn_msb,
            rpn_lsb=self.rpn_lsb,
            nrpn_msb=self.nrpn_msb,
            nrpn_lsb=self.nrpn_lsb,
            active=self.active,
            rpn_values=dict(self.rpn_values),
            nrpn_values=dict(self.nrpn_values),
            last_track=self.last_track,
            rpn_tracks=dict(self.rpn_tracks),
            nrpn_tracks=dict(self.nrpn_tracks),
        )

    def apply(self, control: int, value: int, track_index: int) -> None:
        """Update selection or data from one CC."""
        self.last_track = track_index
        if control == CC_RPN_MSB:
            self.rpn_msb = value
            self.active = "rpn"
        elif control == CC_RPN_LSB:
            self.rpn_lsb = value
            self.active = "rpn"
        elif control == CC_NRPN_MSB:
            self.nrpn_msb = value
            self.active = "nrpn"
        elif control == CC_NRPN_LSB:
            self.nrpn_lsb = value
            self.active = "nrpn"
        elif control == CC_DATA_ENTRY_MSB:
            self._write_data(data_msb=value, track_index=track_index)
        elif control == CC_DATA_ENTRY_LSB:
            self._write_data(data_lsb=value, track_index=track_index)
        elif control == CC_DATA_INCREMENT:
            self._nudge(1, track_index)
        elif control == CC_DATA_DECREMENT:
            self._nudge(-1, track_index)

    def _selected_key(self) -> Optional[tuple[str, tuple[int, int]]]:
        if self.active == "rpn":
            key = (self.rpn_msb, self.rpn_lsb)
            if key == (PARAM_NULL, PARAM_NULL):
                return None
            return "rpn", key
        if self.active == "nrpn":
            key = (self.nrpn_msb, self.nrpn_lsb)
            if key == (PARAM_NULL, PARAM_NULL):
                return None
            return "nrpn", key
        return None

    def _write_data(
        self,
        track_index: int,
        data_msb: Optional[int] = None,
        data_lsb: Optional[int] = None,
    ) -> None:
        selected = self._selected_key()
        if selected is None:
            return
        kind, key = selected
        store = self.rpn_values if kind == "rpn" else self.nrpn_values
        tracks = self.rpn_tracks if kind == "rpn" else self.nrpn_tracks
        cur_msb, cur_lsb = store.get(key, (0, 0))
        if data_msb is not None:
            cur_msb = data_msb
        if data_lsb is not None:
            cur_lsb = data_lsb
        store[key] = (cur_msb, cur_lsb)
        tracks[key] = track_index

    def _nudge(self, delta: int, track_index: int) -> None:
        selected = self._selected_key()
        if selected is None:
            return
        kind, key = selected
        store = self.rpn_values if kind == "rpn" else self.nrpn_values
        tracks = self.rpn_tracks if kind == "rpn" else self.nrpn_tracks
        msb, lsb = store.get(key, (0, 0))
        value = max(0, min(16383, (msb << 7) | lsb) + delta)
        store[key] = (value >> 7, value & 0x7F)
        tracks[key] = track_index

    def freeze(self) -> tuple:
        """Immutable snapshot payload for ControllerSnapshot."""
        return (
            self.rpn_msb,
            self.rpn_lsb,
            self.nrpn_msb,
            self.nrpn_lsb,
            self.active,
            self.last_track,
            tuple(
                (k[0], k[1], v[0], v[1], self.rpn_tracks.get(k, self.last_track))
                for k, v in sorted(self.rpn_values.items())
            ),
            tuple(
                (k[0], k[1], v[0], v[1], self.nrpn_tracks.get(k, self.last_track))
                for k, v in sorted(self.nrpn_values.items())
            ),
        )

    @classmethod
    def thaw(cls, payload: tuple) -> "ParamEntryState":
        (
            rpn_msb,
            rpn_lsb,
            nrpn_msb,
            nrpn_lsb,
            active,
            last_track,
            rpn_rows,
            nrpn_rows,
        ) = payload
        state = cls(
            rpn_msb=rpn_msb,
            rpn_lsb=rpn_lsb,
            nrpn_msb=nrpn_msb,
            nrpn_lsb=nrpn_lsb,
            active=active,
            last_track=last_track,
        )
        for msb, lsb, d_msb, d_lsb, track in rpn_rows:
            key = (msb, lsb)
            state.rpn_values[key] = (d_msb, d_lsb)
            state.rpn_tracks[key] = track
        for msb, lsb, d_msb, d_lsb, track in nrpn_rows:
            key = (msb, lsb)
            state.nrpn_values[key] = (d_msb, d_lsb)
            state.nrpn_tracks[key] = track
        return state


@dataclass(frozen=True)
class ControllerSnapshot:
    """Controller state at a point in the event list (for fast chase)."""

    event_index: int
    # Per-track history: (track_index, chase_key, value, event_index)
    # chase_key is e.g. ("cc", channel, control) or ("program", channel).
    state: tuple[tuple[int, tuple, int, int], ...]
    # channel -> frozen ParamEntryState payload
    param_entries: tuple[tuple[int, tuple], ...] = ()


class TransportState(Enum):
    """Transport State"""

    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


def mido_send_channel_cc(ch: int, control: int, value: int) -> Message:
    return mido.Message(
        MidoMessageType.CONTROL_CHANGE, channel=ch, control=control, value=value
    )


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
      are sent (the file is not changed). A scale of 0.0 suppresses note-ons.
    - Starting (including tick 0) and seeking restore ("chase") program/bank,
      controller and pitch-bend values from earlier in the file, sending only
      values that differ from what was successfully delivered. Missing values
      use a per-channel baseline (defaults). Stop does not clear the
      last-delivered cache: the synthesizer still holds that state.
    - Controller history is kept per track so mute/remap can resolve the
      effective output-channel state from the latest eligible track (file order:
      absolute tick, then track index). Same-tick precedence follows that order.
    - Optional: loop (whole file or A–B), playback speed, MIDI clock out.

    Shared-channel limitation: MIDI note-off and sustain are channel-wide.
    When two tracks share a channel and the same pitch (or both hold sustain),
    the engine keeps logical per-track ownership and only sends a device
    note-off / sustain-off when no track still owns that state. Independent
    track mute cannot fully isolate parts that share channel and pitch.
    Remapping multiple tracks onto one output channel inherits the same limit.

    RPN/NRPN: known RPN defaults (e.g. pitch-bend sensitivity) are restored on
    seek-back; arbitrary NRPN values cannot be reversed without device-specific
    knowledge and are left as last delivered when the file has no earlier value.

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
        self._time_signature_ticks: List[int] = [0]

        self.events: List[ScheduledEvent] = []
        self._event_ticks: List[int] = (
            []
        )  # Cached ticks for binary search; set in _build_event_list
        self._event_seconds: List[float] = []  # Absolute time of each event
        self._controller_snapshots: List[ControllerSnapshot] = []
        self._snapshot_indexes: List[int] = []
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
        self._next_clock_seconds: float = 0.0
        self._clock_segment_end_tick: float = math.inf
        self._clock_seconds_per_pulse: float = 0.0
        self._clock_position_dirty = False  # Song Position Pointer needed on next run

        self._muted_tracks: set[int] = set()
        self._muted_channels: set[int] = set()
        # track_index -> note-on velocity factor (absent = 1.0)
        self._velocity_scales: dict[int, float] = {}
        # track_index -> overall note-on level 0.0–1.0 (absent = 1.0)
        self._track_volumes: dict[int, float] = {}
        # track_index -> semitone offset (absent = 0)
        self._track_transposes: dict[int, int] = {}
        # track_index -> output MIDI channel 0–15 (absent = file channel)
        self._track_channel_maps: dict[int, int] = {}

        # (channel, note) -> {track_index: outstanding note-on count}
        self._held_notes: dict[tuple[int, int], dict[int, int]] = {}
        # channel -> tracks that currently have sustain >= 64
        self._sustained_channels: dict[int, set[int]] = {}
        # Last value successfully delivered for each chased key
        self._sent_controllers: dict[tuple, int] = {}
        # Last RPN/NRPN parameter state delivered per output channel
        self._sent_param_entries: dict[int, ParamEntryState] = {}
        # False after load until a chase/emit establishes device agreement
        self._output_known = False
        # Channels that must be baseline-established after a file replace
        self._channels_needing_establish: set[int] = set()
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
        if getattr(midi_file, "type", 1) == 2:
            raise ValueError(
                "MIDI Type 2 (independent sequences) is not supported; "
                "use Type 0 or Type 1"
            )
        self.release_held_notes()
        # New file: delivery relative to this intent is unknown until chase.
        # Remember channels we had delivered so baselines can be re-established.
        self._channels_needing_establish = (
            {key[1] for key in self._sent_controllers}
            | set(self._sent_param_entries)
        )
        self._sent_controllers.clear()
        self._sent_param_entries.clear()
        self._output_known = False
        self.midi_file = midi_file
        self.ticks_per_beat = midi_file.ticks_per_beat

        self._build_tempo_map()
        self._build_event_list()
        self._build_controller_snapshots()
        self._build_time_map()
        self._event_seconds = [self._tick_to_seconds(t) for t in self._event_ticks]
        self.loop_range = None
        self.reset()
        if self.on_event is not None:
            self._chase()
            self._output_known = True

    def reset(self) -> None:
        """Reset playback state (position, play flag). Mute/suppress settings are unchanged.

        Last-delivered controller caches are kept; :meth:`load_file` clears them
        when replacing the file so the next chase can re-establish honestly.
        """
        self.release_held_notes()
        self.event_index = 0
        self.start_tick = 0
        self._start_time = 0.0
        self._start_seconds = 0.0
        self._position_seconds = 0.0
        self._held_notes.clear()
        self._sustained_channels.clear()
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
        self._time_signature_ticks = [t for t, _, _ in self._time_signatures]

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

        # Same-tick order: track index (stable within a track preserves sequence).
        self.events.sort(key=lambda e: (e.absolute_tick, e.track_index))

        # cache ticks for binary search
        self._event_ticks = [e.absolute_tick for e in self.events]

    def _build_controller_snapshots(self) -> None:
        """Periodic chase-state snapshots for O(log N + K) seeking."""
        self._controller_snapshots = []
        self._snapshot_indexes = []
        # (track_index, chase_key) -> (value, event_index)
        state: dict[tuple[int, tuple], tuple[int, int]] = {}
        param_by_ch: dict[int, ParamEntryState] = {}
        for i, event in enumerate(self.events):
            key = self._chase_key(event.message)
            if key is not None:
                state[(event.track_index, key)] = (
                    self._chase_value(event.message),
                    i,
                )
            self._apply_param_event(param_by_ch, event)
            if (i + 1) % CONTROLLER_SNAPSHOT_INTERVAL == 0:
                snap = ControllerSnapshot(
                    event_index=i + 1,
                    state=tuple(
                        (track, key, value, event_index)
                        for (track, key), (value, event_index) in state.items()
                    ),
                    param_entries=tuple(
                        (ch, param_by_ch[ch].freeze())
                        for ch in sorted(param_by_ch)
                    ),
                )
                self._controller_snapshots.append(snap)
                self._snapshot_indexes.append(snap.event_index)

    @staticmethod
    def _apply_param_event(
        param_by_ch: dict[int, ParamEntryState], event: ScheduledEvent
    ) -> None:
        """Update per-channel RPN/NRPN state from one scheduled event."""
        msg = event.message
        if msg.type != MidoMessageType.CONTROL_CHANGE:
            return
        if msg.control not in UNCHASED_CONTROLS:
            return
        state = param_by_ch.setdefault(msg.channel, ParamEntryState())
        state.apply(msg.control, msg.value, event.track_index)

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
        idx = bisect.bisect_right(self._time_signature_ticks, tick) - 1
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
        """Begin or resume playback from *start_tick* (clamped to length).

        Always chases desired controller state (baseline ∪ file), including
        when *start_tick* is 0.
        """
        if self._is_playing:
            self.release_held_notes()
        start_tick = max(0, min(int(start_tick), self.length_ticks))
        self._position_at_tick(start_tick)
        self._start_time = self._now()
        self._set_state(TransportState.PLAYING)
        self._chase()
        self._chase_pending = False
        self._output_known = True
        if self.send_clock:
            if start_tick > 0:
                self._send_song_position()
                self._emit(mido.Message("continue"))
            else:
                self._next_clock_tick = 0.0
                self._recompute_clock_deadline()
                self._emit(mido.Message("start"))
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
        """Stop playback; the position is kept (seek_tick(0) rewinds).

        Last-delivered controller caches are kept so the next chase can
        diff honestly against synthesizer state.
        """
        was_running = self._is_playing
        if was_running:
            self._hold_position()
        self._set_state(TransportState.STOPPED)
        self.release_held_notes()
        if was_running and self.send_clock:
            self._emit(mido.Message("stop"))

    def pause(self) -> None:
        """Pause playback; position is preserved and sounding notes are released.

        Events due at the pause instant are dispatched before the position
        is frozen so they are not deferred until resume.
        """
        if not self._is_playing:
            return
        self.process_until_now()
        if not self._is_playing:
            return
        self._hold_position()
        self._set_state(TransportState.PAUSED)
        self.release_held_notes()
        # Controllers (including sustain) must be restored on resume without
        # re-striking notes that were sounding at the pause point.
        self._chase_pending = True
        if self.send_clock:
            self._emit(mido.Message("stop"))

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
            if self.send_clock:
                self._emit(mido.Message("stop"))
            return True
        return False

    def wall_seconds_until_next_due(self) -> float:
        """
        Wall-clock seconds until the next event, MIDI clock, or loop boundary.

        Returns ``0.0`` if something is already due or overdue. Returns
        ``math.inf`` when not playing so a scheduler can block on a wake event
        instead of polling.
        """
        if not self._is_playing:
            return math.inf
        position = self._running_position()
        due_times: List[float] = []
        if self.event_index < len(self._event_seconds):
            due_times.append(self._event_seconds[self.event_index])
        else:
            due_times.append(self.duration_seconds)
        clock = self._next_clock_deadline()
        if clock != math.inf:
            due_times.append(clock)
        loop_end = self._loop_end_seconds()
        if loop_end is not None:
            due_times.append(loop_end)
        delay_file = min(due_times) - position
        if delay_file <= 0:
            return 0.0
        return delay_file / max(self.speed, 1e-9)

    def _send_due(self, position: float, limit: Optional[float]) -> None:
        """Send events and clock due at *position*, in time order, before *limit*."""
        events_count = len(self.events)
        clock_time = self._next_clock_deadline()
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
                self._advance_clock_pulse()
                clock_time = self._next_clock_deadline()
            else:
                self._dispatch(self.events[self.event_index])
                self.event_index += 1

    def _next_clock_deadline(self) -> float:
        if not self.send_clock:
            return math.inf
        return self._next_clock_seconds

    def _recompute_clock_deadline(self) -> None:
        """Set the next clock deadline and tempo-segment end from `_next_clock_tick`."""
        self._next_clock_seconds = self._tick_to_seconds(self._next_clock_tick)
        idx = bisect.bisect_right(self._tempo_ticks, self._next_clock_tick)
        if idx < len(self._tempo_ticks):
            self._clock_segment_end_tick = float(self._tempo_ticks[idx])
        else:
            self._clock_segment_end_tick = math.inf
        tempo = self._get_tempo_at_tick(int(self._next_clock_tick))
        pulse = self.ticks_per_beat / MIDI_CLOCKS_PER_BEAT
        self._clock_seconds_per_pulse = mido.tick2second(
            pulse, self.ticks_per_beat, tempo
        )

    def _advance_clock_pulse(self) -> None:
        """Advance one MIDI clock pulse, using incremental time inside a tempo segment."""
        pulse = self.ticks_per_beat / MIDI_CLOCKS_PER_BEAT
        next_tick = self._next_clock_tick + pulse
        if next_tick <= self._clock_segment_end_tick + 1e-9:
            self._next_clock_tick = next_tick
            self._next_clock_seconds += self._clock_seconds_per_pulse
        else:
            self._next_clock_tick = next_tick
            self._recompute_clock_deadline()

    def _wrap_loop(self, overshoot: float) -> None:
        """Jump from the loop end back to the loop start.

        A long scheduler stall may cross several loop boundaries; fold whole
        loop lengths out of *overshoot* so the remaining offset is within one
        loop and elapsed time is not discarded.
        """
        start_tick = self.loop_range[0] if self.loop_range else 0
        end_tick = self.loop_range[1] if self.loop_range else self.length_ticks
        loop_start_sec = self._tick_to_seconds(start_tick)
        loop_end_sec = self._tick_to_seconds(end_tick)
        loop_dur = loop_end_sec - loop_start_sec
        if loop_dur > 0:
            while overshoot >= loop_dur:
                overshoot -= loop_dur
        else:
            overshoot = 0.0
        self.release_held_notes()
        if self.send_clock:
            self._send(mido.Message("stop"))
        self._position_at_tick(start_tick)
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
        self._recompute_clock_deadline()

    # --- Chase ----------------------------------------------------------------

    @staticmethod
    def _chase_key(msg: mido.Message) -> Optional[tuple]:
        if msg.type == MidoMessageType.PROGRAM_CHANGE:
            return "program", msg.channel
        if msg.type == MidoMessageType.PITCH_WHEEL:
            return "pitchwheel", msg.channel
        if (
            msg.type == MidoMessageType.CONTROL_CHANGE
            and msg.control not in UNCHASED_CONTROLS
            and msg.control < FIRST_CHANNEL_MODE_CONTROL
        ):
            return "cc", msg.channel, msg.control
        return None

    @staticmethod
    def _chase_value(msg: mido.Message) -> int:
        if msg.type == MidoMessageType.PROGRAM_CHANGE:
            return msg.program
        if msg.type == MidoMessageType.PITCH_WHEEL:
            return msg.pitch
        return msg.value

    @staticmethod
    def _default_key(kind: tuple, channel: int) -> tuple:
        return (kind[0], channel, *kind[1:])

    def _remap_chase_key(self, key: tuple, track_index: int) -> tuple:
        """Rewrite a chase key's channel when the track is remapped."""
        mapped = self._track_channel_maps.get(track_index)
        if mapped is None:
            return key
        return (key[0], mapped, *key[2:])

    def _channel_baseline(self, channel: int) -> dict[tuple, int]:
        return {
            self._default_key(kind, channel): default
            for kind, default in CHASE_DEFAULTS.items()
        }

    def _controller_state_before(
        self, event_index: int
    ) -> tuple[dict[tuple[int, tuple], tuple[int, int]], dict[int, ParamEntryState]]:
        """
        Per-track controller history and RPN/NRPN state before *event_index*.

        History maps ``(track_index, chase_key) -> (value, event_index)``.
        Uses periodic snapshots then replays the short tail.
        """
        idx = bisect.bisect_right(self._snapshot_indexes, event_index) - 1
        if idx >= 0:
            snap = self._controller_snapshots[idx]
            start = snap.event_index
            state = {
                (track, key): (value, ev_i)
                for track, key, value, ev_i in snap.state
            }
            param_by_ch = {
                ch: ParamEntryState.thaw(payload)
                for ch, payload in snap.param_entries
            }
        else:
            start = 0
            state = {}
            param_by_ch = {}
        for i, event in enumerate(self.events[start:event_index], start=start):
            key = self._chase_key(event.message)
            if key is not None:
                state[(event.track_index, key)] = (
                    self._chase_value(event.message),
                    i,
                )
            self._apply_param_event(param_by_ch, event)
        return state, param_by_ch

    def _resolve_from_file(
        self, history: dict[tuple[int, tuple], tuple[int, int]]
    ) -> dict[tuple, int]:
        """
        Resolve per-track history to effective output-channel controller values.

        Among unmuted tracks, apply remapping, then take the last event in file
        order (merged list: absolute tick, then track index) per output key.
        """
        # out_key -> (event_index, value)
        best: dict[tuple, tuple[int, int]] = {}
        for (track_index, key), (value, event_index) in history.items():
            if track_index in self._muted_tracks:
                continue
            if self._suppressed_kind(key[0], key[2] if key[0] == "cc" else None):
                continue
            out_key = self._remap_chase_key(key, track_index)
            if out_key[1] in self._muted_channels:
                continue
            prev = best.get(out_key)
            if prev is None or event_index >= prev[0]:
                best[out_key] = (event_index, value)
        return {key: value for key, (_, value) in best.items()}

    def _chase(self) -> None:
        """Send the controller state in effect at the position (changes only)."""
        history, param_by_ch = self._controller_state_before(self.event_index)
        from_file = self._resolve_from_file(history)

        desired_params = self._remap_param_entries(param_by_ch)
        channels = (
            {key[1] for key in from_file}
            | {key[1] for key in self._sent_controllers}
            | set(desired_params)
            | set(self._sent_param_entries)
            | set(self._channels_needing_establish)
        )
        from_file_keys = set(from_file)
        for ch in sorted(channels):
            desired = self._channel_baseline(ch)
            for key, value in from_file.items():
                if key[1] == ch:
                    desired[key] = value
            self._send_desired_channel_state(ch, desired, from_file_keys)
            if not self.suppress_control_changes and ch not in self._muted_channels:
                self._chase_param_entry(ch, desired_params.get(ch))
        self._channels_needing_establish.clear()
        self._output_known = True

    def _remap_param_entries(
        self, param_by_ch: dict[int, ParamEntryState]
    ) -> dict[int, ParamEntryState]:
        """Apply track mute and channel remap to file RPN/NRPN state."""
        out: dict[int, ParamEntryState] = {}
        for file_ch, state in param_by_ch.items():
            for key, value in state.rpn_values.items():
                track = state.rpn_tracks.get(key, state.last_track)
                if track in self._muted_tracks:
                    continue
                out_ch = self._track_channel_maps.get(track, file_ch)
                slot = out.setdefault(out_ch, ParamEntryState())
                slot.rpn_values[key] = value
                slot.rpn_tracks[key] = track
                slot.last_track = track
            for key, value in state.nrpn_values.items():
                track = state.nrpn_tracks.get(key, state.last_track)
                if track in self._muted_tracks:
                    continue
                out_ch = self._track_channel_maps.get(track, file_ch)
                slot = out.setdefault(out_ch, ParamEntryState())
                slot.nrpn_values[key] = value
                slot.nrpn_tracks[key] = track
                slot.last_track = track
            if state.last_track not in self._muted_tracks and state.active:
                out_ch = self._track_channel_maps.get(state.last_track, file_ch)
                slot = out.setdefault(out_ch, ParamEntryState())
                slot.rpn_msb = state.rpn_msb
                slot.rpn_lsb = state.rpn_lsb
                slot.nrpn_msb = state.nrpn_msb
                slot.nrpn_lsb = state.nrpn_lsb
                slot.active = state.active
                slot.last_track = state.last_track
        return out

    def _chase_param_entry(
        self, channel: int, desired: Optional[ParamEntryState]
    ) -> None:
        """Emit ordered RPN/NRPN sequences so the device matches *desired*."""
        desired = (desired or ParamEntryState()).copy()
        sent = self._sent_param_entries.get(channel) or ParamEntryState()
        # Include GM defaults for known RPNs when the file never set them but
        # we previously sent a non-default (backwards seek).
        for key, default in RPN_DEFAULTS.items():
            if key not in desired.rpn_values:
                if sent.rpn_values.get(key) not in (None, default):
                    desired.rpn_values[key] = default

        # Restore each RPN that differs
        for key in sorted(set(desired.rpn_values) | set(sent.rpn_values)):
            want = desired.rpn_values.get(key)
            have = sent.rpn_values.get(key)
            if want is None:
                want = RPN_DEFAULTS.get(key)
            if want is None or want == have:
                continue
            if not self._send_param_sequence(channel, "rpn", key, want):
                return
        for key in sorted(set(desired.nrpn_values) | set(sent.nrpn_values)):
            want = desired.nrpn_values.get(key)
            have = sent.nrpn_values.get(key)
            if want is None or want == have:
                continue
            if not self._send_param_sequence(channel, "nrpn", key, want):
                return

        # Leave selection null after chase so later live Data Entry is safe
        if (
            desired.rpn_values
            or desired.nrpn_values
            or sent.rpn_values
            or sent.nrpn_values
        ):
            self._send_param_null(channel)

    def _send_param_sequence(
        self,
        channel: int,
        kind: str,
        key: tuple[int, int],
        data: tuple[int, int],
    ) -> bool:
        """
        Send select + data-entry for one RPN/NRPN.

        :return: False if a message was blocked by mute/suppress
        """
        msb_cc = CC_RPN_MSB if kind == "rpn" else CC_NRPN_MSB
        lsb_cc = CC_RPN_LSB if kind == "rpn" else CC_NRPN_LSB
        param_msb, param_lsb = key
        data_msb, data_lsb = data
        sequence = [
            (msb_cc, param_msb),
            (lsb_cc, param_lsb),
            (CC_DATA_ENTRY_MSB, data_msb),
            (CC_DATA_ENTRY_LSB, data_lsb),
        ]
        for control, value in sequence:
            msg = mido_send_channel_cc(channel, control, value)
            if not self._controller_allowed(msg):
                return False
            if not self._emit(msg):
                return False
        slot = self._sent_param_entries.setdefault(channel, ParamEntryState())
        if kind == "rpn":
            slot.rpn_values[key] = data
            slot.active = "rpn"
            slot.rpn_msb, slot.rpn_lsb = key
        else:
            slot.nrpn_values[key] = data
            slot.active = "nrpn"
            slot.nrpn_msb, slot.nrpn_lsb = key
        return True

    def _send_param_null(self, channel: int) -> None:
        """Select the null RPN so subsequent Data Entry is ignored by the device."""
        for control, value in (
            (CC_RPN_MSB, PARAM_NULL),
            (CC_RPN_LSB, PARAM_NULL),
        ):
            msg = mido_send_channel_cc(channel, control, value)
            if not self._controller_allowed(msg):
                return
            if not self._emit(msg):
                return
        slot = self._sent_param_entries.setdefault(channel, ParamEntryState())
        slot.rpn_msb = PARAM_NULL
        slot.rpn_lsb = PARAM_NULL
        slot.active = None

    def _send_desired_channel_state(
        self,
        channel: int,
        desired: dict[tuple, int],
        from_file_keys: set[tuple],
    ) -> None:
        """Diff *desired* against last-sent and emit filtered controller messages."""
        bank_keys = [
            ("cc", channel, CC_BANK_SELECT_MSB),
            ("cc", channel, CC_BANK_SELECT_LSB),
        ]
        program_key = ("program", channel)

        def needs_send(key: tuple) -> bool:
            if key not in desired:
                return False
            sent = self._sent_controllers.get(key)
            if sent is None:
                # Cold start: only file values (assume hardware at baseline).
                # After load_file replaced a previously delivered channel,
                # re-establish the full desired baseline ∪ file for that channel.
                return (
                    key in from_file_keys
                    or key[1] in self._channels_needing_establish
                )
            return sent != desired[key]

        if any(needs_send(k) for k in bank_keys + [program_key]):
            for key in bank_keys + [program_key]:
                if key not in desired:
                    continue
                # Banks accompany a program establish even when only baseline
                if key in bank_keys and (
                    needs_send(program_key) or needs_send(key)
                ):
                    if self._sent_controllers.get(key) != desired[key]:
                        self._send_chased_value(key, desired[key])
                elif needs_send(key):
                    self._send_chased_value(key, desired[key])

        for key in sorted(k for k in desired if k[0] == "cc" and k[1] == channel):
            if key not in bank_keys and needs_send(key):
                self._send_chased_value(key, desired[key])

        pitch_key = ("pitchwheel", channel)
        if needs_send(pitch_key):
            self._send_chased_value(pitch_key, desired[pitch_key])

    def _message_for_chase_key(self, key: tuple, value: int) -> mido.Message:
        kind = key[0]
        channel = key[1]
        if kind == "program":
            return mido.Message(
                MidoMessageType.PROGRAM_CHANGE, channel=channel, program=value
            )
        if kind == "pitchwheel":
            return mido.Message(
                MidoMessageType.PITCH_WHEEL, channel=channel, pitch=value
            )
        return mido.Message(
            MidoMessageType.CONTROL_CHANGE,
            channel=channel,
            control=key[2],
            value=value,
        )

    def _send_chased_value(self, key: tuple, value: int) -> None:
        """Send one chased controller through the mute/suppress filter."""
        msg = self._message_for_chase_key(key, value)
        if not self._controller_allowed(msg):
            return
        if not self._emit(msg):
            return
        self._sent_controllers[key] = value
        if key[0] == "cc" and key[2] == CC_SUSTAIN:
            if value >= SUSTAIN_ON_THRESHOLD:
                # Chase does not attribute sustain to a track; mark channel as
                # held by a synthetic owner so release_held_notes can clear it.
                self._sustained_channels.setdefault(key[1], set()).add(-1)
            else:
                self._sustained_channels.pop(key[1], None)

    def _controller_allowed(self, msg: mido.Message) -> bool:
        channel = getattr(msg, "channel", None)
        if channel is not None and channel in self._muted_channels:
            return False
        return not self._suppressed(msg)

    def _suppressed_kind(self, kind: str, control: Optional[int] = None) -> bool:
        """
        Whether program/CC suppression drops a message of *kind*.

        Bank Select (CC 0 / CC 32) only qualifies the program change that
        follows it, so it follows ``suppress_program_changes``, not
        ``suppress_control_changes``: otherwise suppressing CCs would send
        program changes without their bank, selecting the right program
        number in the wrong bank.

        :param kind: "program", "cc" or a mido message type
        :param control: CC number for control changes
        :return: bool True if the message must not be sent
        """
        if kind in ("program", "program_change"):
            return self.suppress_program_changes
        if kind in ("cc", "control_change"):
            if control in (CC_BANK_SELECT_MSB, CC_BANK_SELECT_LSB):
                return self.suppress_program_changes
            return self.suppress_control_changes
        return False

    def _suppressed(self, msg: mido.Message) -> bool:
        """
        Whether program/CC suppression drops *msg* (see :meth:`_suppressed_kind`).

        :param msg: mido.Message
        :return: bool
        """
        return self._suppressed_kind(msg.type, getattr(msg, "control", None))

    # --- Sending --------------------------------------------------------------

    def _emit(self, msg: mido.Message) -> bool:
        """
        Deliver *msg* via ``on_event``.

        Always copies before the callback. Returns True only on successful
        delivery; callers must not commit sent/held delivered state otherwise.
        """
        if self.on_event is None:
            return False
        try:
            self.on_event(msg.copy())
        except Exception:
            return False
        return True

    def _dispatch(self, event: ScheduledEvent) -> None:
        if not self._should_send_track_and_suppress(event):
            return
        prepared = self._prepare_output(event)
        if prepared is None:
            return
        msg, send_msg = prepared
        out_channel = getattr(msg, "channel", None)
        if out_channel is not None and out_channel in self._muted_channels:
            return

        # Silenced note-ons (scale/volume 0) are not tracked or sent.
        if (
            send_msg is None
            and msg.type == MidoMessageType.NOTE_ON
            and msg.velocity > 0
        ):
            return

        is_note_release = msg.type == MidoMessageType.NOTE_OFF or (
            msg.type == MidoMessageType.NOTE_ON and msg.velocity == 0
        )
        is_sustain_off = (
            msg.type == MidoMessageType.CONTROL_CHANGE
            and msg.control == CC_SUSTAIN
            and msg.value < SUSTAIN_ON_THRESHOLD
        )
        sounding = ScheduledEvent(event.absolute_tick, msg, event.track_index)

        # Shared-channel: suppress device note-off / sustain-off while another
        # track would still own that sounding state after this event.
        if is_note_release:
            note_key = (msg.channel, msg.note)
            counts = self._held_notes.get(note_key)
            if counts and event.track_index in counts:
                would_remain = dict(counts)
                would_remain[event.track_index] -= 1
                if would_remain[event.track_index] <= 0:
                    del would_remain[event.track_index]
                if would_remain:
                    self._track_sounding(sounding)
                    return
                if send_msg is not None and not self._emit(send_msg):
                    return
                self._track_sounding(sounding)
                return
            # Never owned (e.g. silenced note-on, mid-file start, pause release):
            # forward the file note-off for device/file fidelity, unless another
            # track is sounding this channel/pitch: note-off is channel-wide and
            # would cut that track's note while it is still counted as held.
            if send_msg is not None and not counts:
                self._emit(send_msg)
            return

        if is_sustain_off:
            owners = self._sustained_channels.get(msg.channel)
            if owners and event.track_index in owners:
                would_remain = owners - {event.track_index}
                if would_remain:
                    self._track_sounding(sounding)
                    return
                if send_msg is not None and not self._emit(send_msg):
                    return
                self._track_sounding(sounding)
                key = self._chase_key(msg)
                if key is not None:
                    self._sent_controllers[key] = self._chase_value(msg)
                return
            # Not an owner: a sustain-off would release another track's pedal
            if owners:
                return
            if send_msg is not None and self._emit(send_msg):
                key = self._chase_key(msg)
                if key is not None:
                    self._sent_controllers[key] = self._chase_value(msg)
            return

        if send_msg is not None and not self._emit(send_msg):
            return

        self._track_sounding(sounding)
        key = self._chase_key(msg)
        if key is not None:
            self._sent_controllers[key] = self._chase_value(msg)
        if (
            msg.type == MidoMessageType.CONTROL_CHANGE
            and msg.control in UNCHASED_CONTROLS
        ):
            slot = self._sent_param_entries.setdefault(
                msg.channel, ParamEntryState()
            )
            slot.apply(msg.control, msg.value, event.track_index)

    def _prepare_output(
        self, event: ScheduledEvent
    ) -> Optional[tuple[mido.Message, Optional[mido.Message]]]:
        """
        Apply channel remap, transpose, velocity scale and track volume.

        :return: (message used for state/chase, message to send) or None when
            a transposed note is out of range. The send message is None when a
            note-on is silenced by scale/volume 0 (state is still tracked).
        """
        msg = self._apply_arrange(event)
        if msg is None:
            return None
        send_msg = self._apply_velocity_and_volume(event.track_index, msg)
        return msg, send_msg

    def _apply_arrange(self, event: ScheduledEvent) -> Optional[mido.Message]:
        """Channel remap and transpose; None if the sounding note is out of 0–127."""
        msg = event.message
        track = event.track_index
        changes: dict[str, int] = {}
        mapped = self._track_channel_maps.get(track)
        if mapped is not None and hasattr(msg, "channel"):
            changes["channel"] = mapped
        transpose = self._track_transposes.get(track, 0)
        if transpose and msg.type in (
            MidoMessageType.NOTE_ON,
            MidoMessageType.NOTE_OFF,
            "polytouch",
        ):
            new_note = msg.note + transpose
            if new_note < 0 or new_note > 127:
                return None
            changes["note"] = new_note
        if not changes:
            return msg
        return msg.copy(**changes)

    def _apply_velocity_and_volume(
        self, track_index: int, msg: mido.Message
    ) -> Optional[mido.Message]:
        """Scale note-on velocity; return None to suppress a silenced note-on."""
        if msg.type != "note_on" or msg.velocity == 0:
            return msg
        scale = self._velocity_scales.get(track_index, 1.0)
        volume = self._track_volumes.get(track_index, 1.0)
        if scale == 0.0 or volume == 0.0:
            return None
        factor = scale * volume
        if factor == 1.0:
            return msg.copy()
        return msg.copy(velocity=max(1, min(127, round(msg.velocity * factor))))

    def _track_sounding(self, event: ScheduledEvent) -> None:
        """Remember which notes and sustain pedals are down so they can be released."""
        msg = event.message
        key = (
            getattr(msg, MidoMessageAttrs.CHANNEL, None),
            getattr(msg, MidoMessageAttrs.NOTE, None),
        )
        track = event.track_index
        if msg.type == MidoMessageType.NOTE_ON and msg.velocity > 0:
            counts = self._held_notes.setdefault(key, {})
            counts[track] = counts.get(track, 0) + 1
        elif msg.type in (MidoMessageType.NOTE_OFF, MidoMessageType.NOTE_ON):
            counts = self._held_notes.get(key)
            if counts is not None and track in counts:
                counts[track] -= 1
                if counts[track] <= 0:
                    del counts[track]
                if not counts:
                    del self._held_notes[key]
        elif msg.type == MidoMessageType.CONTROL_CHANGE and msg.control == CC_SUSTAIN:
            if msg.value >= SUSTAIN_ON_THRESHOLD:
                self._sustained_channels.setdefault(msg.channel, set()).add(track)
            else:
                owners = self._sustained_channels.get(msg.channel)
                if owners is not None:
                    owners.discard(track)
                    if not owners:
                        del self._sustained_channels[msg.channel]

    def _send(self, msg: mido.Message) -> None:
        """Legacy alias for clock/seek paths; prefer :meth:`_emit`."""
        self._emit(msg)

    def release_held_notes(
        self, channel: Optional[int] = None, track_index: Optional[int] = None
    ) -> None:
        """
        Send note-off (and sustain-off) for what the engine has sounded.

        Device messages are only sent when effective channel state becomes
        silent (no remaining track still owns the note or sustain). Delivered
        state is updated only after a successful emit.

        :param channel: Only release notes on this channel (0-15)
        :param track_index: Only release notes sounded by this track
        """

        def matches(ch: int, track: int) -> bool:
            return (channel is None or ch == channel) and (
                track_index is None or track == track_index
            )

        for (ch, note), tracks in list(self._held_notes.items()):
            released = [track for track in list(tracks) if matches(ch, track)]
            if not released:
                continue
            remaining = {t: c for t, c in tracks.items() if t not in released}
            if remaining:
                self._held_notes[(ch, note)] = remaining
                continue
            if not self._emit(
                mido.Message(
                    MidoMessageType.NOTE_OFF, channel=ch, note=note, velocity=0
                )
            ):
                continue
            del self._held_notes[(ch, note)]
        for ch, owners in list(self._sustained_channels.items()):
            released = {track for track in owners if matches(ch, track)}
            if not released:
                continue
            remaining = owners - released
            if remaining:
                self._sustained_channels[ch] = remaining
                continue
            if not self._emit(mido_send_channel_cc(ch, CC_SUSTAIN, 0)):
                continue
            del self._sustained_channels[ch]
            self._sent_controllers[("cc", ch, CC_SUSTAIN)] = 0

    def all_notes_off(self) -> None:
        """Panic: release tracked notes, then All Sound Off / All Notes Off on all channels."""
        self.release_held_notes()
        for ch in range(Channel.TOTAL_CHANNELS):
            control = CC_ALL_SOUND_OFF
            midi_value = 0
            self._emit(mido_send_channel_cc(ch, control, midi_value))
            control = CC_ALL_NOTES_OFF
            self._emit(mido_send_channel_cc(ch, control, midi_value))

    def _should_send_track_and_suppress(self, event: ScheduledEvent) -> bool:
        """Track mute and program/CC suppression (channel mute uses output channel)."""
        msg = event.message

        if event.track_index in self._muted_tracks:
            return False

        return not self._suppressed(msg)

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
        """Mute or unmute a track; calls that change nothing do nothing.

        Both mute and unmute re-resolve effective controllers so a muted
        track's last CC cannot strand another track on a shared channel.
        """
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

        Applies to notes sent from now on. A scale of 0.0 suppresses note-ons
        and releases sounding notes on the track first.
        """
        new_scale = 1.0 if scale == 1.0 else max(0.0, float(scale))
        old = self._velocity_scales.get(track_index, 1.0)
        if new_scale == 0.0 or old == 0.0:
            self.release_held_notes(track_index=track_index)
        if new_scale == 1.0:
            self._velocity_scales.pop(track_index, None)
        else:
            self._velocity_scales[track_index] = new_scale

    def set_track_volume(self, track_index: int, volume: float) -> None:
        """
        Set a track's overall note-on level (0.0–1.0; 1.0 = as scaled).

        Stacks with :meth:`set_track_velocity_scale`. Does not inject CC7.
        A volume of 0.0 suppresses note-ons and releases sounding notes first.
        """
        volume = max(0.0, min(1.0, float(volume)))
        old = self._track_volumes.get(track_index, 1.0)
        if volume == 0.0 or old == 0.0:
            self.release_held_notes(track_index=track_index)
        if volume == 1.0:
            self._track_volumes.pop(track_index, None)
        else:
            self._track_volumes[track_index] = volume

    def set_track_transpose(self, track_index: int, semitones: int) -> None:
        """
        Transpose a track's notes by *semitones* (0 clears).

        Releases sounding notes on the track before applying the change.
        Out-of-range notes after transpose are suppressed. Held-note tracking
        uses the sounding pitch.
        """
        self.release_held_notes(track_index=track_index)
        semitones = int(semitones)
        if semitones == 0:
            self._track_transposes.pop(track_index, None)
        else:
            self._track_transposes[track_index] = semitones
        if self._is_playing:
            self._chase()

    def set_track_channel_map(
        self, track_index: int, channel: Optional[int]
    ) -> None:
        """
        Remap a track to output MIDI *channel* (0–15), or None for the file channel.

        Releases sounding notes on the track first; re-chases if playing.
        Channel mute follows the output channel. Remapping tracks onto one
        channel shares that channel's note/sustain ownership.
        """
        self.release_held_notes(track_index=track_index)
        if channel is None:
            self._track_channel_maps.pop(track_index, None)
        else:
            self._track_channel_maps[track_index] = max(0, min(15, int(channel)))
        if self._is_playing:
            self._chase()

    def _restore_unmuted(self) -> None:
        """
        Re-resolve and chase effective controller state after mute changes.

        Mute and unmute both call this so a muted track's last CC cannot
        leave a shared output channel stranded.
        """
        if self._is_playing:
            self._chase()
        elif self.midi_file is not None:
            self._chase_pending = True
