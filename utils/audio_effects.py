from __future__ import annotations

from dataclasses import dataclass
from math import isclose

from utils.models import AudioFilterPreset, GuildMusicState

SAMPLE_RATE = 48_000


@dataclass(frozen=True, slots=True)
class AudioEffectProfile:
    label: str
    speed_multiplier: float = 1.0
    pitch_semitones: int = 0
    filters: tuple[str, ...] = ()


_PRESET_PROFILES: dict[AudioFilterPreset, AudioEffectProfile] = {
    AudioFilterPreset.OFF: AudioEffectProfile(label="normal"),
    AudioFilterPreset.BASSBOOST: AudioEffectProfile(
        label="bassboost",
        filters=("bass=g=8:f=110:w=0.6", "alimiter=limit=0.94"),
    ),
    AudioFilterPreset.CLEAR: AudioEffectProfile(
        label="clear",
        filters=(
            "highpass=f=40",
            "lowpass=f=16500",
            "acompressor=threshold=-18dB:ratio=2.0:attack=20:release=250",
        ),
    ),
    AudioFilterPreset.RADIO: AudioEffectProfile(
        label="radio",
        filters=(
            "highpass=f=220",
            "lowpass=f=4200",
            "acompressor=threshold=-20dB:ratio=2.5:attack=15:release=220",
        ),
    ),
    AudioFilterPreset.NIGHTCORE: AudioEffectProfile(
        label="nightcore",
        speed_multiplier=1.08,
        pitch_semitones=2,
        filters=(
            "treble=g=3:f=9000:w=0.7",
            "bass=g=1.5:f=120:w=0.3",
        ),
    ),
    AudioFilterPreset.VAPORWAVE: AudioEffectProfile(
        label="vaporwave",
        speed_multiplier=0.92,
        pitch_semitones=-2,
        filters=(
            "bass=g=4:f=95:w=0.8",
            "lowpass=f=12500",
        ),
    ),
}


def get_preset_profile(preset: AudioFilterPreset) -> AudioEffectProfile:
    return _PRESET_PROFILES.get(preset, _PRESET_PROFILES[AudioFilterPreset.OFF])


def effective_speed(state: GuildMusicState) -> float:
    profile = get_preset_profile(state.filter_preset)
    return max(0.25, state.playback_speed * profile.speed_multiplier)


def effective_pitch_semitones(state: GuildMusicState) -> int:
    profile = get_preset_profile(state.filter_preset)
    return state.pitch_semitones + profile.pitch_semitones


def describe_audio_effects(state: GuildMusicState) -> str:
    profile = get_preset_profile(state.filter_preset)
    speed = effective_speed(state)
    pitch = effective_pitch_semitones(state)

    if profile.label == "normal" and isclose(speed, 1.0, abs_tol=0.01) and pitch == 0:
        return "Normal"

    return f"{profile.label} | {speed:.2f}x | {pitch:+d} st"


def build_ffmpeg_filter_chain(state: GuildMusicState) -> str | None:
    profile = get_preset_profile(state.filter_preset)
    target_speed = effective_speed(state)
    target_pitch = effective_pitch_semitones(state)
    pitch_ratio = 2 ** (target_pitch / 12)

    filters: list[str] = []
    if not isclose(pitch_ratio, 1.0, abs_tol=0.001):
        filters.append(f"asetrate={SAMPLE_RATE}*{pitch_ratio:.8f}")
        filters.append(f"aresample={SAMPLE_RATE}")

    target_tempo = max(0.25, target_speed / pitch_ratio)
    if not isclose(target_tempo, 1.0, abs_tol=0.001):
        filters.extend(_build_atempo_filters(target_tempo))

    filters.extend(profile.filters)
    return ",".join(filters) if filters else None


def _build_atempo_filters(target_tempo: float) -> list[str]:
    remaining = target_tempo
    filters: list[str] = []

    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5

    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0

    if not isclose(remaining, 1.0, abs_tol=0.001):
        filters.append(f"atempo={remaining:.5f}")
    return filters
