"""
MIDI tempo and timing constants.
"""


class MidiTempo:
    """Tempo and timing constants."""

    BPM_120 = 120
    BPM_100_USEC = 600_000
    BPM_120_USEC = 500_000
    BPM_150_USEC = 400_000
    BPM_162_USEC = 370_370
    MICROSECONDS_PER_SECOND = 1_000_000
    MICROSECONDS_PER_MINUTE = 60_000_000
    MILLISECONDS_PER_SECOND = 1_000
    MILLISECONDS_PER_MINUTE = 60_000
    SECONDS_PER_MINUTE = 60


def milliseconds_per_note(bpm: int | float, division: int = 4) -> float:
    """calculate step duration
    1 = quarter note
    2 = eighth note
    4 = 16th note
    """
    if bpm is None:
        raise ValueError("BPM must not be None")
    if bpm <= 0:
        raise ValueError("BPM must be positive")
    if division <= 0:
        raise ValueError("division must be positive")
    ms_per_step = (MidiTempo.MILLISECONDS_PER_MINUTE / bpm) / division  # 4 = ms per 16th note
    return ms_per_step
