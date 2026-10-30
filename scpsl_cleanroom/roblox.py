"""Unity -> Roblox conversion (coordinates, units, lights) and Luau emission."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .unity_scene import LightRecord, MaterialRecord, RenderSettingsRecord, quat_rotate

ROBLOX_MAX_LIGHT_RANGE = 60.0  # studs


@dataclass
class ConvertOptions:
    meters_per_stud: float = 0.28
    # Roblox Brightness has no physical unit; this factor is tuned by eye per project.
    brightness_scale: float = 1.0
    max_brightness: float = 40.0


def to_roblox_position(p, opt: ConvertOptions):
    """Unity (left-handed, +Z forward, meters) -> Roblox (right-handed, -Z forward, studs)."""
    s = 1.0 / opt.meters_per_stud
    return (p[0] * s, p[1] * s, -p[2] * s)


def to_roblox_rotation(q):
    """Mirror across Z: R' = M R M with M = diag(1, 1, -1)."""
    x, y, z, w = q
    return (-x, -y, z, w)


def quat_to_matrix(q):
    x, y, z, w = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def cframe_components(pos, rot) -> list[float]:
    """CFrame.new(x, y, z, R00, R01, R02, R10, R11, R12, R20, R21, R22)."""
    m = quat_to_matrix(rot)
    return [round(v, 5) for v in (*pos, *m[0], *m[1], *m[2])]


def to_color3(c) -> tuple[float, float, float]:
    return tuple(round(min(max(v, 0.0), 1.0), 4) for v in c)


def convert_light(light: LightRecord, opt: ConvertOptions) -> dict:
    range_studs = light.range / opt.meters_per_stud
    rec = {
        "name": light.name,
        "path": light.path,
        "source": f"asset:{light.source}",
        "enabled": light.enabled,
        "color": to_color3(light.color),
        "brightness": round(min(light.intensity * opt.brightness_scale, opt.max_brightness), 4),
        "shadows": light.shadows != "none",
        "range": round(min(range_studs, ROBLOX_MAX_LIGHT_RANGE), 3),
        "range_clamped": range_studs > ROBLOX_MAX_LIGHT_RANGE,
        "cframe": cframe_components(to_roblox_position(light.position, opt), to_roblox_rotation(light.rotation)),
    }
    if light.type == "spot":
        rec["class"] = "SpotLight"
        rec["angle"] = round(min(light.spot_angle, 180.0), 3)
    elif light.type == "directional":
        rec["class"] = "Directional"
        fwd = quat_rotate(light.rotation, (0.0, 0.0, 1.0))
        # Lighting direction in Roblox points *towards* the sun.
        rec["sun_direction"] = [round(-fwd[0], 5), round(-fwd[1], 5), round(fwd[2], 5)]
    elif light.type in ("area", "disc"):
        rec["class"] = "SurfaceLight"
        rec["angle"] = 90.0
    else:
        rec["class"] = "PointLight"
    if light.extra:
        rec["unity_extra"] = light.extra
    return rec


def convert_render_settings(rs: RenderSettingsRecord, opt: ConvertOptions) -> dict:
    out = {
        "source": f"asset:{rs.source}",
        "Ambient": to_color3(rs.ambient_equator if rs.ambient_mode == 1 else rs.ambient_sky),
        "OutdoorAmbient": to_color3(rs.ambient_sky),
        "ColorShift_Bottom": to_color3(rs.ambient_ground),
    }
    if rs.fog:
        out["FogColor"] = to_color3(rs.fog_color)
        if rs.fog_mode == "linear":
            out["FogStart"] = round(rs.fog_start / opt.meters_per_stud, 2)
            out["FogEnd"] = round(rs.fog_end / opt.meters_per_stud, 2)
        else:
            # Roblox fog is linear: pick the distance where exp fog reaches ~95% density.
            d = rs.fog_density or 0.01
            k = 3.0 if rs.fog_mode == "exponential" else math.sqrt(3.0)
            out["FogStart"] = 0
            out["FogEnd"] = round((k / d) / opt.meters_per_stud, 2)
            out["fog_note"] = f"approximated {rs.fog_mode} fog (density {d}) as linear"
    return out


def convert_material(m: MaterialRecord) -> dict:
    out = {"name": m.name, "source": f"asset:{m.source}"}
    if m.color:
        out["Color"] = to_color3(m.color)
    if m.emission:
        out["Material"] = "Neon"
        out["EmissionColor"] = to_color3(m.emission)
        out["emission_strength"] = round(max(m.emission), 4)
    return out


# --- Luau emission -----------------------------------------------------------

def _lua_key(k: str) -> str:
    return k if k.isidentifier() else f'["{k}"]'


def to_luau(value, indent: int = 0) -> str:
    pad = "\t" * indent
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "nil"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
            return "0"
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, (list, tuple)):
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return "{" + ", ".join(to_luau(v) for v in value) + "}"
        items = [f"{pad}\t{to_luau(v, indent + 1)}," for v in value]
        return "{\n" + "\n".join(items) + f"\n{pad}}}"
    if isinstance(value, dict):
        items = [f"{pad}\t{_lua_key(str(k))} = {to_luau(v, indent + 1)}," for k, v in value.items()]
        return "{\n" + "\n".join(items) + f"\n{pad}}}" if items else "{}"
    raise TypeError(f"cannot convert {type(value).__name__} to Luau")


def lighting_module(lights: list[dict], settings: list[dict], materials: list[dict]) -> str:
    data = {"Lights": lights, "Lighting": settings[0] if settings else {}, "Materials": materials}
    return (
        "--!strict\n"
        "-- Generated by scpsl_cleanroom from licensed asset data. Do not edit by hand.\n"
        f"return {to_luau(data)}\n"
    )
