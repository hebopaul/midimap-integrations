"""Versioned overlay client and plain-data constructors. No rendering in workers."""
import base64
from ._wire.overlay_spec import validate_overlay_spec


class UI:
    def __init__(self, runtime):
        self._runtime = runtime

    @property
    def capabilities(self):
        return {"spec_versions": [1], "size_classes": ["compact", "standard", "wide"],
                "reduced_motion": getattr(self._runtime, "_ui_reduced_motion", False)}

    async def _call(self, method, permission_id, **params):
        result = await self._runtime.host_call(method, dict(params, permission_id=permission_id), 5)
        return result["accepted"]

    async def toast(self, spec, *, permission_id):
        return await self._call("host.ui.toast", permission_id, spec=validate_overlay_spec(spec))

    async def update(self, toast_id, patch, *, permission_id):
        return await self._call("host.ui.update", permission_id, toast_id=toast_id, patch=patch)

    async def dismiss(self, toast_id, *, permission_id):
        return await self._call("host.ui.dismiss", permission_id, toast_id=toast_id)

    async def put_asset(self, asset_id, png, *, permission_id):
        return await self._call("host.ui.asset", permission_id, asset_id=asset_id,
                                data=base64.b64encode(png).decode("ascii"))

    async def open_menu(self, menu_id, *, permission_id):
        return await self._call("host.ui.menu.open", permission_id, menu_id=menu_id)


def element(kind, **fields):
    """Construct a v1 element. Full specs are checked before transmission."""
    return {"type": kind, **fields}


def text(value, **fields):
    return element("text", text=value, **fields)


def row(*children, **fields):
    return element("row", children=list(children), **fields)


def column(*children, **fields):
    return element("column", children=list(children), **fields)


def page(id, title, *items, **fields):
    return element("page", id=id, title=title, items=list(items), **fields)


def toast_spec(toast_id, root, size="standard", **fields):
    return validate_overlay_spec({"v": 1, "toast_id": toast_id, "size": size, "root": root, **fields})


def menu_spec(root, *pages, size="standard", **fields):
    return validate_overlay_spec({"v": 1, "surface": "menu", "size": size, "root": root,
                                  "pages": list(pages), **fields})


def _builder(kind):
    def build(**fields):
        return element(kind, **fields)
    build.__name__ = kind
    return build


for _kind in ("spacer", "divider", "stack", "grid", "card", "readout", "badge", "kv", "hint",
              "icon", "image", "swatch", "status", "spinner", "qr", "bar", "dial", "meter",
              "steps", "switch", "range", "progress", "timeline", "sparkline", "countdown", "xy",
              "item", "toggle_item", "slider_item", "choice_item", "action_item", "section"):
    globals()[_kind] = _builder(_kind)
