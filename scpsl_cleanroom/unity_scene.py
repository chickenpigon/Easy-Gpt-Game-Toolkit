"""Read Unity text-serialized scenes/prefabs/materials (licensed assets) without Unity.

Input is what an asset exporter writes (``.unity``, ``.prefab``, ``.mat`` in
"Force Text" YAML). Extracts lights with world transforms, render settings
(ambient, fog) and material colours/emission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CLASS_GAMEOBJECT = 1
CLASS_TRANSFORM = 4
CLASS_MONOBEHAVIOUR = 114
CLASS_RENDERSETTINGS = 104
CLASS_LIGHT = 108
CLASS_RECTTRANSFORM = 224

LIGHT_TYPES = {0: "spot", 1: "directional", 2: "point", 3: "area", 4: "disc"}
SHADOW_TYPES = {0: "none", 1: "hard", 2: "soft"}
FOG_MODES = {1: "linear", 2: "exponential", 3: "exponential_squared"}

_DOC_HEADER = re.compile(r"^--- !u!(\d+) &(-?\d+)(.*)$", re.MULTILINE)
_HD_LIGHT_KEYS = re.compile(r"(?i)(intensity|lightunit|range|temperature|volumetric|shape|lux|lumen)")


@dataclass
class UnityObject:
    class_id: int
    file_id: int
    type_name: str
    data: dict
    stripped: bool = False


def parse_unity_yaml(text: str) -> dict[int, UnityObject]:
    objects: dict[int, UnityObject] = {}
    headers = list(_DOC_HEADER.finditer(text))
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[m.end():end]
        try:
            parsed = yaml.safe_load(body) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict) or not parsed:
            continue
        type_name, data = next(iter(parsed.items()))
        objects[int(m.group(2))] = UnityObject(
            class_id=int(m.group(1)),
            file_id=int(m.group(2)),
            type_name=type_name,
            data=data or {},
            stripped="stripped" in m.group(3),
        )
    return objects


# --- vector / quaternion helpers (Unity convention: x, y, z, w) -------------

def _vec(d: dict | None, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    if not d:
        return default
    return (float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("z", 0)))


def _quat(d: dict | None) -> tuple[float, float, float, float]:
    if not d:
        return (0.0, 0.0, 0.0, 1.0)
    return (float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("z", 0)), float(d.get("w", 1)))


def quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_rotate(q, v):
    x, y, z, w = q
    vq = (v[0], v[1], v[2], 0.0)
    r = quat_mul(quat_mul(q, vq), (-x, -y, -z, w))
    return (r[0], r[1], r[2])


def _color(d: dict | None) -> tuple[float, float, float]:
    if not d:
        return (1.0, 1.0, 1.0)
    return (float(d.get("r", 1)), float(d.get("g", 1)), float(d.get("b", 1)))


# --- extracted records -------------------------------------------------------

@dataclass
class LightRecord:
    name: str
    path: str
    type: str
    color: tuple[float, float, float]
    intensity: float
    range: float
    spot_angle: float
    shadows: str
    enabled: bool
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    source: str
    extra: dict = field(default_factory=dict)


@dataclass
class RenderSettingsRecord:
    ambient_mode: int
    ambient_sky: tuple[float, float, float]
    ambient_equator: tuple[float, float, float]
    ambient_ground: tuple[float, float, float]
    ambient_intensity: float
    fog: bool
    fog_color: tuple[float, float, float]
    fog_mode: str
    fog_density: float
    fog_start: float
    fog_end: float
    source: str


@dataclass
class MaterialRecord:
    name: str
    color: tuple[float, float, float] | None
    emission: tuple[float, float, float] | None
    floats: dict
    source: str


class Scene:
    def __init__(self, objects: dict[int, UnityObject], source: str):
        self.objects = objects
        self.source = source
        self._world_cache: dict[int, tuple] = {}
        self._transform_by_go: dict[int, UnityObject] = {}
        for o in objects.values():
            if o.class_id in (CLASS_TRANSFORM, CLASS_RECTTRANSFORM):
                go_id = int((o.data.get("m_GameObject") or {}).get("fileID", 0))
                self._transform_by_go[go_id] = o

    @classmethod
    def load(cls, path: str | Path) -> "Scene":
        p = Path(path)
        return cls(parse_unity_yaml(p.read_text(encoding="utf-8", errors="replace")), p.name)

    def _ref(self, d: dict | None) -> UnityObject | None:
        if not d:
            return None
        return self.objects.get(int(d.get("fileID", 0)))

    def transform_of(self, go_id: int) -> UnityObject | None:
        return self._transform_by_go.get(go_id)

    def world(self, tr: UnityObject):
        """(position, rotation, scale) in world space."""
        if tr.file_id in self._world_cache:
            return self._world_cache[tr.file_id]
        lp = _vec(tr.data.get("m_LocalPosition"))
        lr = _quat(tr.data.get("m_LocalRotation"))
        ls = _vec(tr.data.get("m_LocalScale"), (1.0, 1.0, 1.0))
        parent = self._ref(tr.data.get("m_Father"))
        if parent is None:
            result = (lp, lr, ls)
        else:
            pp, pr, ps = self.world(parent)
            scaled = (lp[0] * ps[0], lp[1] * ps[1], lp[2] * ps[2])
            off = quat_rotate(pr, scaled)
            result = (
                (pp[0] + off[0], pp[1] + off[1], pp[2] + off[2]),
                quat_mul(pr, lr),
                (ps[0] * ls[0], ps[1] * ls[1], ps[2] * ls[2]),
            )
        self._world_cache[tr.file_id] = result
        return result

    def path_of(self, tr: UnityObject | None) -> str:
        names = []
        while tr is not None:
            go = self._ref(tr.data.get("m_GameObject"))
            names.append(str(go.data.get("m_Name", "?")) if go else "?")
            tr = self._ref(tr.data.get("m_Father"))
        return "/".join(reversed(names))

    def lights(self) -> list[LightRecord]:
        out = []
        for o in self.objects.values():
            if o.class_id != CLASS_LIGHT or o.stripped:
                continue
            d = o.data
            go = self._ref(d.get("m_GameObject"))
            go_id = go.file_id if go else 0
            tr = self.transform_of(go_id) if go else None
            pos, rot, _ = self.world(tr) if tr else ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), None)
            shadows = d.get("m_Shadows") or {}
            out.append(LightRecord(
                name=str(go.data.get("m_Name", "Light")) if go else "Light",
                path=self.path_of(tr),
                type=LIGHT_TYPES.get(int(d.get("m_Type", 2)), "point"),
                color=_color(d.get("m_Color")),
                intensity=float(d.get("m_Intensity", 1)),
                range=float(d.get("m_Range", 10)),
                spot_angle=float(d.get("m_SpotAngle", 30)),
                shadows=SHADOW_TYPES.get(int(shadows.get("m_Type", 0)), "none"),
                enabled=bool(d.get("m_Enabled", 1)) and bool(go.data.get("m_IsActive", 1) if go else 1),
                position=pos,
                rotation=rot,
                source=f"{self.source}#{o.file_id}",
                extra=self._hd_light_fields(go_id),
            ))
        return out

    def _hd_light_fields(self, go_id: int) -> dict:
        """Scalar fields of MonoBehaviours next to the light (e.g. render-pipeline light data)."""
        extra = {}
        for o in self.objects.values():
            if o.class_id != CLASS_MONOBEHAVIOUR:
                continue
            if int((o.data.get("m_GameObject") or {}).get("fileID", 0)) != go_id:
                continue
            for k, v in o.data.items():
                if _HD_LIGHT_KEYS.search(k) and isinstance(v, (int, float, str)):
                    extra[k] = v
        return extra

    def render_settings(self) -> RenderSettingsRecord | None:
        for o in self.objects.values():
            if o.class_id != CLASS_RENDERSETTINGS:
                continue
            d = o.data
            return RenderSettingsRecord(
                ambient_mode=int(d.get("m_AmbientMode", 0)),
                ambient_sky=_color(d.get("m_AmbientSkyColor")),
                ambient_equator=_color(d.get("m_AmbientEquatorColor")),
                ambient_ground=_color(d.get("m_AmbientGroundColor")),
                ambient_intensity=float(d.get("m_AmbientIntensity", 1)),
                fog=bool(d.get("m_Fog", 0)),
                fog_color=_color(d.get("m_FogColor")),
                fog_mode=FOG_MODES.get(int(d.get("m_FogMode", 3)), "exponential_squared"),
                fog_density=float(d.get("m_FogDensity", 0.01)),
                fog_start=float(d.get("m_LinearFogStart", 0)),
                fog_end=float(d.get("m_LinearFogEnd", 300)),
                source=f"{self.source}#{o.file_id}",
            )
        return None


def _named_list(entries) -> dict:
    """Unity serializes property maps either as [{name: value}] lists or plain dicts."""
    out = {}
    if isinstance(entries, dict):
        return dict(entries)
    for e in entries or []:
        if isinstance(e, dict):
            if "first" in e and "second" in e:  # very old format
                out[str(e["first"].get("name", e["first"]) if isinstance(e["first"], dict) else e["first"])] = e["second"]
            else:
                out.update(e)
    return out


def read_material(path: str | Path) -> list[MaterialRecord]:
    p = Path(path)
    objs = parse_unity_yaml(p.read_text(encoding="utf-8", errors="replace"))
    out = []
    for o in objs.values():
        if o.type_name != "Material":
            continue
        props = o.data.get("m_SavedProperties") or {}
        colors = _named_list(props.get("m_Colors"))
        floats = {k: v for k, v in _named_list(props.get("m_Floats")).items() if isinstance(v, (int, float))}
        base = colors.get("_BaseColor") or colors.get("_Color")
        emission = colors.get("_EmissionColor") or colors.get("_EmissiveColor")
        emissive_on = emission is not None and any(c > 0 for c in _color(emission))
        out.append(MaterialRecord(
            name=str(o.data.get("m_Name", p.stem)),
            color=_color(base) if base else None,
            emission=_color(emission) if emission and emissive_on else None,
            floats=floats,
            source=p.name,
        ))
    return out


def scan(paths: list[str | Path]) -> dict:
    """Walk files/directories and collect lights, render settings and materials."""
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.suffix in (".unity", ".prefab", ".mat")]
        else:
            files.append(p)
    lights, settings, materials, errors = [], [], [], []
    for f in sorted(files):
        try:
            if f.suffix == ".mat":
                materials += read_material(f)
                continue
            scene = Scene.load(f)
            lights += scene.lights()
            rs = scene.render_settings()
            if rs:
                settings.append(rs)
        except (OSError, ValueError) as e:
            errors.append(f"{f}: {e}")
    return {"lights": lights, "render_settings": settings, "materials": materials, "errors": errors}
