# Changelog

All notable changes to picomidi are documented in this file.

# [0.3.0] — 2026-11-19

Playback release. `PlaybackEngine` grows the per-track controls and the
deadline scheduler that JD-Xi Editor's MIDI player needs. Nothing public was
removed, so 0.2.0 code keeps working; the engine's playback tests live in the
JD-Xi Editor repository (`tests/test_playback_engine.py`,
`tests/test_playback_scheduler.py`) and gate this release.

### Added

- **`picomidi.playback.scheduler.PlaybackScheduler`**: Drives a
  `PlaybackEngine` from a background thread, waiting until the next event,
  clock pulse, or loop boundary is due instead of polling on a fixed interval.
  `wake()` interrupts the wait after a seek, pause, resume, or speed change.
  A single sleep is capped (`DEFAULT_MAX_SLEEP_S`) so a missed wake cannot
  stall playback. The engine itself stays deterministic and Qt-free.
- **Per-track playback controls on `PlaybackEngine`**: `set_track_volume`,
  `set_track_transpose`, and `set_track_channel_map`.
- **Engine state snapshots**: `copy`, `apply`, `freeze`, and `thaw`, plus the
  `ControllerSnapshot` and `ParamEntryState` types, so controller state can be
  captured and restored around a seek.
- **`wall_seconds_until_next_due`**: What the scheduler waits on — the wall
  time until the engine's next deadline, or `math.inf` when nothing is due.
- **RPN/NRPN chasing**: Controller chasing (CC, bank/program, pitch wheel)
  already reconstructed state before the seek target; it now also replays
  RPN/NRPN parameter-entry sequences, with the null-parameter terminator, so a
  seek into the middle of a file no longer leaves parameter entry half-set.
- **`picomidi.message.type.MidoMessageAttrs`**: The `channel` and `note`
  attribute names, for code that reads them off a mido message.

### Changed

- **`MidoMessageType` and `MidoMetaMessageType` are now `str` enums**. Their
  members compare equal to mido's `msg.type` and can be passed straight to
  `mido.Message(...)`, which removes the `.value` dance at each call site.
  Existing comparisons against members are unaffected.
- **`testpaths`** points at `picomidi/tests`, where the tests actually live.
  It previously named a `tests/` directory that does not exist, so a strict
  `pytest` run collected nothing.

# [0.2.0] — 2026-11-06

First release that ships the subpackages (`core`, `playback`, `sysex`, ...);
0.1.2 contained the top-level package only.
