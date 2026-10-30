"""UI layout from licensed prefabs/scenes: RectTransforms, text (font, size, colour,
alignment) and images, converted to Roblox GuiObject properties (UDim2, AnchorPoint).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .unity_scene import CLASS_MONOBEHAVIOUR, CLASS_RECTTRANSFORM, Scene, _color

_GUID = re.compile(r"^guid:\s*([0-9a-f]{32})", re.MULTILINE)

# TextAnchor (legacy UI Text): 0..8 = Upper/Middle/Lower x Left/Center/Right
_LEGACY_ALIGN = {i: (("Left", "Center", "Right")[i % 3], ("Top", "Center", "Bottom")[i // 3]) for i in range(9)}
_TMP_H = {1: "Left", 2: "Center", 4: "Right", 8: "Left", 16: "Center", 32: "Center"}
_TMP_V = {256: "Top", 512: "Center", 1024: "Bottom", 2048: "Center", 4096: "Center", 8192: "Center"}


def guid_index(roots: list[str | Path]) -> dict[str, str]:
    """guid -> asset file name, from the ``.meta`` files next to exported assets."""
    out = {}
    for root in map(Path, roots):
        metas = root.rglob("*.meta") if root.is_dir() else []
        for meta in metas:
            m = _GUID.search(meta.read_text(encoding="utf-8", errors="replace")[:2000])
            if m:
                out[m.group(1)] = meta.name[:-5]
    return out


@dataclass
class UiNode:
    name: str
    path: str
    anchor_min: tuple[float, float]
    anchor_max: tuple[float, float]
    anchored_position: tuple[float, float]
    size_delta: tuple[float, float]
    pivot: tuple[float, float]
    active: bool
    abs_size: tuple[float, float] = (0.0, 0.0)
    text: dict | None = None
    image: dict | None = None
    children: list["UiNode"] = field(default_factory=list)

    def roblox(self, parent_size: tuple[float, float] | None) -> dict:
        """GuiObject properties. With ``parent_size`` offsets become scale (resolution independent)."""
        (ax0, ay0), (ax1, ay1) = self.anchor_min, self.anchor_max
        px, py = self.pivot
        pos_sx = ax0 + (ax1 - ax0) * px
        pos_sy = 1 - (ay0 + (ay1 - ay0) * py)
        pos_ox, pos_oy = self.anchored_position[0], -self.anchored_position[1]
        size_sx, size_sy = ax1 - ax0, ay1 - ay0
        size_ox, size_oy = self.size_delta
        if parent_size and parent_size[0] > 0 and parent_size[1] > 0:
            pw, ph = parent_size
            pos_sx, pos_sy = pos_sx + pos_ox / pw, pos_sy + pos_oy / ph
            size_sx, size_sy = size_sx + size_ox / pw, size_sy + size_oy / ph
            pos_ox = pos_oy = size_ox = size_oy = 0.0
        r = lambda v: round(v, 5)
        out = {
            "Name": self.name,
            "Visible": self.active,
            "AnchorPoint": [r(px), r(1 - py)],
            "Position": [r(pos_sx), r(pos_ox), r(pos_sy), r(pos_oy)],
            "Size": [r(size_sx), r(size_ox), r(size_sy), r(size_oy)],
        }
        if self.text:
            out["ClassName"] = "TextLabel"
            out.update(self.text)
        elif self.image:
            out["ClassName"] = "ImageLabel"
            out.update(self.image)
        else:
            out["ClassName"] = "Frame"
        return out


def _v2(d, default=(0.0, 0.0)):
    if not d:
        return default
    return (float(d.get("x", 0)), float(d.get("y", 0)))


def _ref_name(ref: dict | None, guids: dict[str, str]) -> str | None:
    if not ref or not ref.get("guid"):
        return None
    return guids.get(ref["guid"], f"guid:{ref['guid']}")


def _text_props(mb: dict, guids, ref_height: float) -> dict | None:
    if "m_text" in mb and ("m_fontSize" in mb or "m_fontAsset" in mb):  # TextMeshPro
        h = _TMP_H.get(int(mb.get("m_HorizontalAlignment", 0)), None)
        v = _TMP_V.get(int(mb.get("m_VerticalAlignment", 0)), None)
        if h is None or v is None:  # older TMP: single combined enum
            code = int(mb.get("m_textAlignment", 257))
            h = _TMP_H.get(code & 0xFF, "Left")
            v = _TMP_V.get(code & ~0xFF, "Top")
        size = float(mb.get("m_fontSize", 36))
        color = mb.get("m_fontColor") or {}
        return {
            "Text": str(mb.get("m_text", "")),
            "Font": _ref_name(mb.get("m_fontAsset"), guids),
            "TextSize": size,
            "TextSizeScale": round(size / ref_height, 5) if ref_height else None,
            "TextScaled": bool(mb.get("m_enableAutoSizing", 0)),
            "TextSizeMin": mb.get("m_fontSizeMin"),
            "TextSizeMax": mb.get("m_fontSizeMax"),
            "TextColor3": list(_color(color)),
            "TextTransparency": round(1 - float(color.get("a", 1)), 4),
            "TextXAlignment": h,
            "TextYAlignment": v,
            "FontStyle": int(mb.get("m_fontStyle", 0)),
            "CharacterSpacing": mb.get("m_characterSpacing"),
            "LineSpacing": mb.get("m_lineSpacing"),
            "BackgroundTransparency": 1,
            "source_component": "TextMeshPro",
        }
    if "m_Text" in mb and "m_FontData" in mb:  # legacy UI Text
        fd = mb["m_FontData"] or {}
        h, v = _LEGACY_ALIGN.get(int(fd.get("m_Alignment", 0)), ("Left", "Top"))
        size = float(fd.get("m_FontSize", 14))
        color = mb.get("m_Color") or {}
        return {
            "Text": str(mb.get("m_Text", "")),
            "Font": _ref_name(fd.get("m_Font"), guids),
            "TextSize": size,
            "TextSizeScale": round(size / ref_height, 5) if ref_height else None,
            "TextScaled": bool(fd.get("m_BestFit", 0)),
            "TextSizeMin": fd.get("m_MinSize"),
            "TextSizeMax": fd.get("m_MaxSize"),
            "TextColor3": list(_color(color)),
            "TextTransparency": round(1 - float(color.get("a", 1)), 4),
            "TextXAlignment": h,
            "TextYAlignment": v,
            "FontStyle": int(fd.get("m_FontStyle", 0)),
            "LineSpacing": fd.get("m_LineSpacing"),
            "BackgroundTransparency": 1,
            "source_component": "UI.Text",
        }
    return None


def _image_props(mb: dict, guids) -> dict | None:
    if "m_Sprite" in mb and "m_Color" in mb:
        color = mb.get("m_Color") or {}
        return {
            "Image": _ref_name(mb.get("m_Sprite"), guids),
            "ImageColor3": list(_color(color)),
            "ImageTransparency": round(1 - float(color.get("a", 1)), 4),
            "BackgroundTransparency": 1,
            "source_component": "UI.Image",
        }
    return None


def extract(scene: Scene, guids: dict[str, str] | None = None) -> list[dict]:
    """One tree per Canvas root (RectTransform without RectTransform parent)."""
    guids = guids or {}
    rects = {o.file_id: o for o in scene.objects.values() if o.class_id == CLASS_RECTTRANSFORM and not o.stripped}
    mbs_by_go: dict[int, list[dict]] = {}
    for o in scene.objects.values():
        if o.class_id == CLASS_MONOBEHAVIOUR:
            go = int((o.data.get("m_GameObject") or {}).get("fileID", 0))
            mbs_by_go.setdefault(go, []).append(o.data)

    ref_res = None
    for datas in mbs_by_go.values():
        for d in datas:
            if "m_ReferenceResolution" in d:
                ref_res = _v2(d["m_ReferenceResolution"])
    canvas = ref_res or (1920.0, 1080.0)

    def build(rt, parent_size, parent_path) -> UiNode:
        d = rt.data
        go = scene.objects.get(int((d.get("m_GameObject") or {}).get("fileID", 0)))
        name = str(go.data.get("m_Name", "?")) if go else "?"
        node = UiNode(
            name=name,
            path=f"{parent_path}/{name}" if parent_path else name,
            anchor_min=_v2(d.get("m_AnchorMin")),
            anchor_max=_v2(d.get("m_AnchorMax")),
            anchored_position=_v2(d.get("m_AnchoredPosition")),
            size_delta=_v2(d.get("m_SizeDelta")),
            pivot=_v2(d.get("m_Pivot"), (0.5, 0.5)),
            active=bool(go.data.get("m_IsActive", 1)) if go else True,
        )
        if parent_size is None:  # canvas root fills the screen
            node.abs_size = canvas
        else:
            node.abs_size = (
                (node.anchor_max[0] - node.anchor_min[0]) * parent_size[0] + node.size_delta[0],
                (node.anchor_max[1] - node.anchor_min[1]) * parent_size[1] + node.size_delta[1],
            )
        for mb in mbs_by_go.get(go.file_id if go else 0, []):
            node.text = node.text or _text_props(mb, guids, canvas[1])
            node.image = node.image or _image_props(mb, guids)
        for c in d.get("m_Children") or []:
            child = rects.get(int(c.get("fileID", 0)))
            if child:
                node.children.append(build(child, node.abs_size, node.path))
        return node

    roots = [rt for rt in rects.values() if int((rt.data.get("m_Father") or {}).get("fileID", 0)) not in rects]
    out = []
    for rt in roots:
        tree = build(rt, None, "")
        out.append({"reference_resolution": list(canvas), "source": scene.source, "root": _to_roblox(tree, None, True)})
    return out


def _to_roblox(node: UiNode, parent_size, scale_only: bool) -> dict:
    if parent_size is None:
        props = {"Name": node.name, "ClassName": "ScreenGui", "IgnoreGuiInset": True}
    else:
        props = node.roblox(parent_size if scale_only else None)
    props = {k: v for k, v in props.items() if v is not None}
    props["unity_path"] = node.path
    props["Children"] = [_to_roblox(c, node.abs_size, scale_only) for c in node.children]
    return props


def texts(trees: list[dict]) -> list[dict]:
    """Flat list of text elements (for the spec's font/typography table)."""
    out = []

    def walk(n):
        if n.get("ClassName") == "TextLabel":
            out.append({k: n.get(k) for k in ("unity_path", "Text", "Font", "TextSize", "TextSizeScale",
                                               "TextScaled", "TextColor3", "TextXAlignment", "TextYAlignment",
                                               "Position", "Size", "AnchorPoint")})
        for c in n.get("Children", []):
            walk(c)

    for t in trees:
        walk(t["root"])
    return out
