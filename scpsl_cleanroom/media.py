"""Animation, animator, audio-source data from licensed assets, and sound identification
in gameplay recordings.

``match_sounds`` finds where each clip of the licensed sound library plays in a
recording (log-band spectrogram, normalized cross-correlation), so a video of
your own play turns into timed ``sound:<clip>`` events for timeline mining.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .unity_scene import Scene, parse_unity_yaml

CLASS_ANIMATOR_CONTROLLER = 91
CLASS_ANIMATION_CLIP = 74
CLASS_AUDIO_SOURCE = 82
CLASS_STATE_TRANSITION = 1101
CLASS_STATE = 1102
CLASS_STATE_MACHINE = 1107

PARAM_TYPES = {1: "float", 3: "int", 4: "bool", 9: "trigger"}
CONDITION_MODES = {1: "true", 2: "false", 3: ">", 4: "<", 6: "==", 7: "!="}
ROLLOFF = {0: "InverseTapered", 1: "Linear", 2: "Custom"}
AUDIO_SUFFIXES = {".wav", ".ogg", ".mp3", ".flac", ".aif", ".aiff"}


# --- animation clips ------------------------------------------------------------

def read_animation(path: str | Path, max_keys=64) -> list[dict]:
    p = Path(path)
    out = []
    for o in parse_unity_yaml(p.read_text(encoding="utf-8", errors="replace")).values():
        if o.class_id != CLASS_ANIMATION_CLIP:
            continue
        d = o.data
        st = d.get("m_AnimationClipSettings") or {}
        curves = []
        for c in d.get("m_FloatCurves") or []:
            keys = ((c.get("curve") or {}).get("m_Curve")) or []
            curves.append({
                "path": c.get("path", ""),
                "attribute": c.get("attribute", ""),
                "keys": [[round(float(k.get("time", 0)), 4), round(float(k.get("value", 0)), 5)] for k in keys[:max_keys]],
                "truncated": len(keys) > max_keys,
            })
        transform_paths = sorted({c.get("path", "") for kind in ("m_PositionCurves", "m_RotationCurves",
                                                                 "m_EulerCurves", "m_ScaleCurves")
                                  for c in d.get(kind) or []})
        out.append({
            "name": str(d.get("m_Name", p.stem)),
            "source": p.name,
            "sample_rate": float(d.get("m_SampleRate", 60)),
            "start": float(st.get("m_StartTime", 0)),
            "stop": float(st.get("m_StopTime", 0)),
            "length": round(float(st.get("m_StopTime", 0)) - float(st.get("m_StartTime", 0)), 4),
            "loop": bool(st.get("m_LoopTime", 0)),
            "events": [{
                "time": round(float(e.get("time", 0)), 4),
                "function": e.get("functionName"),
                "string": e.get("data") or None,
                "float": e.get("floatParameter"),
                "int": e.get("intParameter"),
            } for e in d.get("m_Events") or []],
            "float_curves": curves,
            "animated_transforms": transform_paths,
        })
    return out


# --- animator controllers ------------------------------------------------------------

def read_animator(path: str | Path, guids: dict[str, str] | None = None) -> list[dict]:
    """States, transitions (conditions, durations, exit times) and parameters."""
    guids = guids or {}
    p = Path(path)
    objs = parse_unity_yaml(p.read_text(encoding="utf-8", errors="replace"))

    def name_of(fid):
        o = objs.get(int(fid or 0))
        return str(o.data.get("m_Name", "?")) if o else None

    def transition(tid):
        t = objs.get(int(tid))
        if not t:
            return None
        d = t.data
        return {
            "to": name_of((d.get("m_DstState") or {}).get("fileID")) or ("Exit" if d.get("m_IsExit") else None),
            "conditions": [f"{c.get('m_ConditionEvent')} {CONDITION_MODES.get(int(c.get('m_ConditionMode', 1)), '?')}"
                           + (f" {c.get('m_EventTreshold')}" if int(c.get("m_ConditionMode", 1)) > 2 else "")
                           for c in d.get("m_Conditions") or []],
            "duration": float(d.get("m_TransitionDuration", 0)),
            "fixed_duration": bool(d.get("m_HasFixedDuration", 1)),
            "exit_time": float(d.get("m_ExitTime", 0)) if d.get("m_HasExitTime") else None,
        }

    out = []
    for ctrl in (o for o in objs.values() if o.class_id == CLASS_ANIMATOR_CONTROLLER):
        layers = []
        for layer in ctrl.data.get("m_AnimatorLayers") or []:
            sm = objs.get(int((layer.get("m_StateMachine") or {}).get("fileID", 0)))
            if not sm:
                continue
            states = []
            for cs in sm.data.get("m_ChildStates") or []:
                st = objs.get(int((cs.get("m_State") or {}).get("fileID", 0)))
                if not st:
                    continue
                motion = st.data.get("m_Motion") or {}
                states.append({
                    "name": str(st.data.get("m_Name")),
                    "speed": float(st.data.get("m_Speed", 1)),
                    "tag": st.data.get("m_Tag") or None,
                    "motion": guids.get(motion.get("guid", ""), name_of(motion.get("fileID")) or motion.get("guid")),
                    "transitions": [t for t in (transition(x.get("fileID", 0)) for x in st.data.get("m_Transitions") or []) if t],
                })
            layers.append({
                "name": layer.get("m_Name"),
                "default_state": name_of((sm.data.get("m_DefaultState") or {}).get("fileID")),
                "states": states,
                "any_state": [t for t in (transition(x.get("fileID", 0)) for x in sm.data.get("m_AnyStateTransitions") or []) if t],
            })
        out.append({
            "name": str(ctrl.data.get("m_Name", p.stem)),
            "source": p.name,
            "parameters": [{"name": x.get("m_Name"), "type": PARAM_TYPES.get(int(x.get("m_Type", 1)), "?"),
                            "default": x.get("m_DefaultFloat") if int(x.get("m_Type", 1)) == 1 else
                            x.get("m_DefaultInt") if int(x.get("m_Type", 1)) == 3 else x.get("m_DefaultBool")}
                           for x in ctrl.data.get("m_AnimatorParameters") or []],
            "layers": layers,
        })
    return out


def mermaid_animator(ctrl: dict) -> str:
    lines = ["stateDiagram-v2"]
    mid = lambda s: "".join(c if c.isalnum() else "_" for c in str(s))
    for layer in ctrl["layers"]:
        if layer["default_state"]:
            lines.append(f"    [*] --> {mid(layer['default_state'])}")
        for st in layer["states"]:
            for t in st["transitions"]:
                cond = " & ".join(t["conditions"]) or (f"exit {t['exit_time']}" if t["exit_time"] is not None else "")
                lines.append(f"    {mid(st['name'])} --> {mid(t['to']) if t['to'] != 'Exit' else '[*]'}: {cond}")
        for t in layer["any_state"]:
            lines.append(f"    AnyState --> {mid(t['to'])}: {' & '.join(t['conditions'])}")
    return "\n".join(lines)


# --- audio sources & files -------------------------------------------------------------

def audio_sources(scene: Scene, guids: dict[str, str] | None = None, meters_per_stud=0.28) -> list[dict]:
    guids = guids or {}
    out = []
    for o in scene.objects.values():
        if o.class_id != CLASS_AUDIO_SOURCE or o.stripped:
            continue
        d = o.data
        go = scene.objects.get(int((d.get("m_GameObject") or {}).get("fileID", 0)))
        tr = scene.transform_of(go.file_id) if go else None
        clip = d.get("m_audioClip") or {}
        blend_keys = ((d.get("panLevelCustomCurve") or {}).get("m_Curve")) or []
        mn, mx = float(d.get("MinDistance", 1)), float(d.get("MaxDistance", 500))
        out.append({
            "path": scene.path_of(tr),
            "source": f"{scene.source}#{o.file_id}",
            "clip": guids.get(clip.get("guid", ""), clip.get("guid")),
            "play_on_awake": bool(d.get("m_PlayOnAwake", 1)),
            "loop": bool(d.get("Loop", 0)),
            "volume": float(d.get("m_Volume", 1)),
            "pitch": float(d.get("m_Pitch", 1)),
            "spatial_blend": float(blend_keys[0].get("value", 0)) if blend_keys else 0.0,
            "min_distance_m": mn,
            "max_distance_m": mx,
            "rolloff": ROLLOFF.get(int(d.get("rolloffMode", 0)), "?"),
            "roblox": {
                "Volume": float(d.get("m_Volume", 1)),
                "PlaybackSpeed": float(d.get("m_Pitch", 1)),
                "Looped": bool(d.get("Loop", 0)),
                "RollOffMinDistance": round(mn / meters_per_stud, 3),
                "RollOffMaxDistance": round(mx / meters_per_stud, 3),
                "RollOffMode": {0: "InverseTapered", 1: "Linear"}.get(int(d.get("rolloffMode", 0)), "InverseTapered"),
            },
        })
    return out


def probe_audio(path: str | Path) -> dict:
    p = Path(path)
    if p.suffix.lower() == ".wav":
        with wave.open(str(p)) as w:
            return {"file": p.name, "duration": round(w.getnframes() / w.getframerate(), 4),
                    "sample_rate": w.getframerate(), "channels": w.getnchannels()}
    if not shutil.which("ffprobe"):
        return {"file": p.name, "error": "ffprobe not installed"}
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                       capture_output=True, text=True)
    try:
        info = json.loads(r.stdout)
        st = next(s for s in info["streams"] if s.get("codec_type") == "audio")
        return {"file": p.name, "duration": round(float(info["format"]["duration"]), 4),
                "sample_rate": int(st.get("sample_rate", 0)), "channels": int(st.get("channels", 0))}
    except (ValueError, KeyError, StopIteration):
        return {"file": p.name, "error": r.stderr.strip()[:200] or "unreadable"}


# --- sound identification ----------------------------------------------------------------

SR = 8000
FRAME = 256
HOP = 128
BANDS = 32


def decode(path: str | Path, sr=SR) -> np.ndarray:
    """Mono float32 at ``sr`` Hz. WAV is read directly, everything else through ffmpeg."""
    p = Path(path)
    if p.suffix.lower() == ".wav":
        with wave.open(str(p)) as w:
            if w.getsampwidth() == 2:
                raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
                raw = raw.reshape(-1, w.getnchannels()).mean(axis=1)
                if w.getframerate() == sr:
                    return raw
                n = int(len(raw) * sr / w.getframerate())
                return np.interp(np.linspace(0, len(raw) - 1, n), np.arange(len(raw)), raw).astype(np.float32)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to decode " + p.suffix)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[:300])
    return np.frombuffer(r.stdout, dtype="<f4")


_BAND_EDGES = np.unique(np.geomspace(2, FRAME // 2, BANDS + 1).astype(int))


def spectrogram(sig: np.ndarray) -> np.ndarray:
    """(frames, bands) level in dB, log-spaced bands, 16 ms hop."""
    if len(sig) < FRAME:
        sig = np.pad(sig, (0, FRAME - len(sig)))
    n = 1 + (len(sig) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    mag = np.abs(np.fft.rfft(sig[idx] * np.hanning(FRAME), axis=1))
    bands = np.add.reduceat(mag ** 2, _BAND_EDGES[:-1], axis=1)
    return 10 * np.log10(bands + 1e-10)


def _xcorr(rec_fft, kernel, nfft, valid):
    return np.fft.irfft(rec_fft * np.conj(np.fft.rfft(kernel, nfft, axis=0)), nfft, axis=0)[:valid].sum(axis=1)


def _ncc(rec: np.ndarray, tpl: np.ndarray, rec_fft=None, rec2_fft=None, nfft=None, dynamic_db=30.0) -> np.ndarray:
    """Normalized cross-correlation over the template's loud cells only.

    Quiet cells of a clean library clip would otherwise be compared against the
    recording's noise floor; restricting to the clip's top ``dynamic_db`` makes the
    score robust to background noise, and the mean removal makes it gain-independent.
    """
    m, _ = tpl.shape
    n = rec.shape[0]
    if n < m:
        return np.zeros(0)
    mask = (tpl > tpl.max() - dynamic_db).astype(float)
    k = mask.sum()
    if k < 4:
        return np.zeros(n - m + 1)
    t = (tpl - (tpl * mask).sum() / k) * mask
    sd = np.sqrt((t ** 2).sum() / k)
    if sd < 1e-9:
        return np.zeros(n - m + 1)
    t /= sd
    nfft = nfft or int(2 ** np.ceil(np.log2(n + m)))
    if rec_fft is None:
        rec_fft = np.fft.rfft(rec, nfft, axis=0)
        rec2_fft = np.fft.rfft(rec ** 2, nfft, axis=0)
    valid = n - m + 1
    num = _xcorr(rec_fft, t, nfft, valid)
    wsum = _xcorr(rec_fft, mask, nfft, valid)
    wsq = _xcorr(rec2_fft, mask, nfft, valid)
    var = wsq / k - (wsum / k) ** 2
    return num / (k * np.sqrt(np.clip(var, 1e-9, None)))


def match_sounds(recording: str | Path, library: list[str | Path], threshold=0.6, offset=0.0) -> list[dict]:
    """Events ``sound:<clip>`` wherever a library clip plays in the recording."""
    rec = spectrogram(decode(recording))
    clips = []
    for root in map(Path, library):
        files = [f for f in root.rglob("*") if f.suffix.lower() in AUDIO_SUFFIXES] if root.is_dir() else [root]
        for f in files:
            try:
                spec = spectrogram(decode(f))
            except RuntimeError:
                continue
            if 3 <= spec.shape[0] <= rec.shape[0]:
                clips.append((f.stem, spec))
    if not clips:
        return []
    nfft = int(2 ** np.ceil(np.log2(rec.shape[0] + max(c.shape[0] for _, c in clips))))
    rec_fft = np.fft.rfft(rec, nfft, axis=0)
    rec2_fft = np.fft.rfft(rec ** 2, nfft, axis=0)
    found = []
    for name, tpl in clips:
        score = _ncc(rec, tpl, rec_fft, rec2_fft, nfft)
        m = tpl.shape[0]
        order = np.argsort(-score)
        taken = np.zeros(len(score), dtype=bool)
        for i in order:
            if score[i] < threshold:
                break
            if taken[i]:
                continue
            taken[max(0, i - m + 1): i + m] = True
            found.append({"t": round(offset + float(i) * HOP / SR, 3), "id": "audio", "event": f"sound:{name}",
                          "score": round(float(score[i]), 3), "duration": round(m * HOP / SR, 3),
                          "source": "sound_match"})
    found.sort(key=lambda e: e["t"])
    return found


def onsets(recording: str | Path, sensitivity=6.0, offset=0.0) -> list[dict]:
    """Unidentified sound onsets (spectral flux peaks) - candidates for unknown sounds."""
    spec = spectrogram(decode(recording)) / 20  # dB -> log10 amplitude
    flux = np.clip(np.diff(spec, axis=0), 0, None).sum(axis=1)
    if len(flux) < 3:
        return []
    med = np.median(flux)
    mad = np.median(np.abs(flux - med)) + 1e-9
    out, last = [], -1e9
    for i in range(1, len(flux) - 1):
        if flux[i] > med + sensitivity * mad * 1.4826 and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1]:
            t = (i + 1) * HOP / SR
            if t - last >= 0.08:
                out.append({"t": round(offset + t, 3), "id": "audio", "event": "sound:onset",
                            "strength": round(float(flux[i] / (med + 1e-9)), 2), "source": "onset"})
                last = t
    return out
