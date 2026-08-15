# SPDX-License-Identifier: GPL-3.0-or-later

"""Choose, hide, and reorder buttons in Blender's 3D View toolbar."""

from __future__ import annotations

import json
from typing import Any, Iterable

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList
from bl_ui.space_toolsystem_common import ToolDef, ToolSelectPanelHelper
from bl_ui.space_toolsystem_toolbar import VIEW3D_PT_tools_active


bl_info = {
    "name": "N-Panel Manager: Toolbar Organizer",
    "author": "N-Panel Manager contributors",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Toolbar",
    "description": "Choose and reorder the buttons in the 3D Viewport toolbar",
    "category": "3D View",
}


ADDON_ID = __package__ or __name__
CONFIG_VERSION = 1

_syncing_ui = False
_original_tools_descriptor = None
_original_tools_function = None
_original_draw_function = None
_original_draw_was_local = False


def _preferences(context=None):
    """Return this add-on's preferences, including inside extension packages."""
    context = context or bpy.context
    preferences = getattr(context, "preferences", None)
    if preferences is None:
        return None

    addon = preferences.addons.get(ADDON_ID)
    if addon is not None:
        return addon.preferences

    # This fallback helps development installs whose extension repository prefix
    # changes after a reinstall.
    package_tail = ADDON_ID.rsplit(".", 1)[-1]
    for module_name, candidate in preferences.addons.items():
        if module_name.rsplit(".", 1)[-1] == package_tail:
            return candidate.preferences
    return None


def _read_config(context=None) -> dict[str, Any]:
    prefs = _preferences(context)
    if prefs is None or not prefs.layouts_json:
        return {"version": CONFIG_VERSION, "modes": {}}
    try:
        data = json.loads(prefs.layouts_json)
    except (TypeError, ValueError):
        return {"version": CONFIG_VERSION, "modes": {}}
    if not isinstance(data, dict) or not isinstance(data.get("modes"), dict):
        return {"version": CONFIG_VERSION, "modes": {}}
    return data


def _write_config(data: dict[str, Any], context=None) -> None:
    prefs = _preferences(context)
    if prefs is None:
        return
    data["version"] = CONFIG_VERSION
    prefs.layouts_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _mode_key(context, mode=None) -> str:
    return str(mode if mode is not None else getattr(context, "mode", "OBJECT"))


def _tool_token(tool) -> str:
    idname = str(getattr(tool, "idname", "unknown"))
    data_block = getattr(tool, "data_block", None)
    if data_block:
        return f"{idname}@{data_block}"
    return idname


def _entry_identifier(entry) -> str:
    if type(entry) is tuple:
        tokens = [_tool_token(tool) for tool in entry if tool is not None]
        return "GROUP|" + "|".join(tokens)
    return "TOOL|" + _tool_token(entry)


def _entry_primary_tool(entry):
    if type(entry) is tuple:
        return next((tool for tool in entry if tool is not None), None)
    return entry


def _entry_records(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Turn a toolbar sequence into stable, reorderable button records."""
    records: list[dict[str, Any]] = []
    occurrences: dict[str, int] = {}
    separator_before = False

    for entry in entries:
        if entry is None:
            if records:
                separator_before = True
            continue
        if type(entry) is not ToolDef and type(entry) is not tuple:
            continue

        base_identifier = _entry_identifier(entry)
        occurrence = occurrences.get(base_identifier, 0)
        occurrences[base_identifier] = occurrence + 1
        identifier = (
            base_identifier if occurrence == 0 else f"{base_identifier}#{occurrence + 1}"
        )
        primary = _entry_primary_tool(entry)
        if primary is None:
            continue
        group_size = (
            sum(tool is not None for tool in entry) if type(entry) is tuple else 1
        )
        records.append(
            {
                "identifier": identifier,
                "entry": entry,
                "label": str(getattr(primary, "label", identifier)),
                "tool_idname": str(getattr(primary, "idname", "")),
                "icon": getattr(primary, "icon", None),
                "group_size": group_size,
                "separator_before": separator_before,
            }
        )
        separator_before = False
    return records


def _base_entries(context, mode=None) -> list[Any]:
    if _original_tools_function is None:
        return []
    return list(_original_tools_function(VIEW3D_PT_tools_active, context, mode))


def _base_records(context, mode=None) -> list[dict[str, Any]]:
    return _entry_records(_base_entries(context, mode))


def _ordered_records(context, mode=None, *, include_disabled=False):
    """Return the current mode's toolbar records in the user's saved order."""
    mode_name = _mode_key(context, mode)
    records = _base_records(context, mode)
    config = _read_config(context)
    saved = config["modes"].get(mode_name)
    if not isinstance(saved, list):
        return [(record, True) for record in records]

    by_identifier = {record["identifier"]: record for record in records}
    result = []
    consumed = set()
    for setting in saved:
        if not isinstance(setting, dict):
            continue
        identifier = setting.get("id")
        record = by_identifier.get(identifier)
        if record is None or identifier in consumed:
            continue
        consumed.add(identifier)
        enabled = bool(setting.get("enabled", True))
        if include_disabled or enabled:
            result.append((record, enabled))

    # Newly installed tools should appear automatically instead of vanishing just
    # because the mode already has a saved layout.
    for record in records:
        if record["identifier"] not in consumed:
            result.append((record, True))
    return result


def _managed_tools_from_context(cls, context, mode=None):
    ordered = _ordered_records(context, mode)
    first = True
    for record, _enabled in ordered:
        if record["separator_before"] and not first:
            yield None
        yield record["entry"]
        first = False


def _tag_view3d_redraw(context=None) -> None:
    context = context or bpy.context
    window_manager = getattr(context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _toolbar_manager_draw(self, context):
    """Add a permanent manager button above the normal tool buttons."""
    layout = self.layout
    ui_gen, show_text = self._layout_generator_detect_from_region(
        layout, context.region, 1.75
    )
    ui_gen.send(None)
    sub = ui_gen.send(False)
    sub.operator(
        NPM_OT_open_toolbar_manager.bl_idname,
        text="Toolbar Manager" if show_text else "",
        icon="PREFERENCES",
    )
    ui_gen.send(None)
    layout.separator()
    _original_draw_function(self, context)


def _save_window_state(context) -> None:
    window_manager = context.window_manager
    mode_name = window_manager.npm_edit_mode
    if not mode_name:
        return
    config = _read_config(context)
    config["modes"][mode_name] = [
        {"id": item.identifier, "enabled": bool(item.enabled)}
        for item in window_manager.npm_tool_items
    ]
    _write_config(config, context)
    _tag_view3d_redraw(context)


def _enabled_updated(_item, context) -> None:
    if _syncing_ui or context is None:
        return
    _save_window_state(context)


class NPM_ToolListItem(PropertyGroup):
    identifier: StringProperty(options={"HIDDEN"})
    label: StringProperty()
    tool_idname: StringProperty(options={"HIDDEN"})
    enabled: BoolProperty(name="Visible", default=True, update=_enabled_updated)
    icon_value: IntProperty(default=0, options={"HIDDEN"})
    group_size: IntProperty(default=1, options={"HIDDEN"})


class NPM_UL_toolbar_tools(UIList):
    """Checkbox list shown in the manager dialog."""

    def draw_item(
        self,
        _context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_property,
        _index=0,
        _flt_flag=0,
    ):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="")
            label_row = row.row(align=True)
            label_row.active = item.enabled
            if item.icon_value:
                label_row.label(text=item.label, icon_value=item.icon_value)
            else:
                label_row.label(text=item.label, icon="TOOL_SETTINGS")
            if item.group_size > 1:
                label_row.label(text=f"{item.group_size} tools", icon="TRIA_DOWN")
        else:
            layout.label(text="", icon_value=item.icon_value)


def _populate_window_state(context, mode_name: str) -> None:
    global _syncing_ui
    window_manager = context.window_manager
    _syncing_ui = True
    try:
        window_manager.npm_tool_items.clear()
        for record, enabled in _ordered_records(
            context, mode_name, include_disabled=True
        ):
            item = window_manager.npm_tool_items.add()
            item.identifier = record["identifier"]
            item.label = record["label"]
            item.tool_idname = record["tool_idname"]
            item.enabled = enabled
            item.group_size = record["group_size"]
            try:
                item.icon_value = ToolSelectPanelHelper._icon_value_from_icon_handle(
                    record["icon"]
                )
            except (AttributeError, RuntimeError, TypeError):
                item.icon_value = 0
        window_manager.npm_tool_index = min(
            window_manager.npm_tool_index,
            max(0, len(window_manager.npm_tool_items) - 1),
        )
        window_manager.npm_edit_mode = mode_name
    finally:
        _syncing_ui = False


class NPM_OT_open_toolbar_manager(Operator):
    bl_idname = "view3d.npm_toolbar_manager"
    bl_label = "Toolbar Manager"
    bl_description = "Choose and reorder the buttons in this toolbar"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"

    def execute(self, context):
        _populate_window_state(context, _mode_key(context))
        return bpy.ops.wm.call_panel(
            "INVOKE_DEFAULT",
            name=NPM_PT_toolbar_manager.bl_idname,
            keep_open=True,
        )


class NPM_PT_toolbar_manager(Panel):
    """Persistent popover opened by the toolbar's manager button."""

    bl_idname = "NPM_PT_toolbar_manager"
    bl_label = "3D View Toolbar Manager"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_ui_units_x = 28

    def draw(self, context):
        layout = self.layout
        window_manager = context.window_manager
        pretty_mode = window_manager.npm_edit_mode.replace("_", " ").title()

        header = layout.row(align=True)
        header.label(text=f"{pretty_mode} Mode", icon="WORKSPACE")
        header.label(text="Changes update the toolbar immediately")

        row = layout.row()
        row.template_list(
            NPM_UL_toolbar_tools.__name__,
            "",
            window_manager,
            "npm_tool_items",
            window_manager,
            "npm_tool_index",
            rows=min(16, max(8, len(window_manager.npm_tool_items))),
        )

        controls = row.column(align=True)
        op = controls.operator(NPM_OT_move_toolbar_item.bl_idname, text="", icon="TRIA_UP_BAR")
        op.direction = "TOP"
        op = controls.operator(NPM_OT_move_toolbar_item.bl_idname, text="", icon="TRIA_UP")
        op.direction = "UP"
        op = controls.operator(NPM_OT_move_toolbar_item.bl_idname, text="", icon="TRIA_DOWN")
        op.direction = "DOWN"
        op = controls.operator(
            NPM_OT_move_toolbar_item.bl_idname, text="", icon="TRIA_DOWN_BAR"
        )
        op.direction = "BOTTOM"

        layout.separator()
        footer = layout.row(align=True)
        op = footer.operator(NPM_OT_set_toolbar_visibility.bl_idname, icon="CHECKBOX_HLT")
        op.visible = True
        op = footer.operator(NPM_OT_set_toolbar_visibility.bl_idname, icon="CHECKBOX_DEHLT")
        op.visible = False
        footer.operator(NPM_OT_restore_mode_defaults.bl_idname, icon="LOOP_BACK")

        box = layout.box()
        box.label(
            text="Rows marked with a triangle are Blender flyout groups and stay grouped.",
            icon="INFO",
        )


class NPM_OT_move_toolbar_item(Operator):
    bl_idname = "view3d.npm_move_toolbar_item"
    bl_label = "Move Toolbar Button"
    bl_description = "Move the selected toolbar button"
    bl_options = {"INTERNAL"}

    direction: EnumProperty(
        items=(
            ("TOP", "Top", "Move to the top"),
            ("UP", "Up", "Move up one place"),
            ("DOWN", "Down", "Move down one place"),
            ("BOTTOM", "Bottom", "Move to the bottom"),
        )
    )

    def execute(self, context):
        window_manager = context.window_manager
        items = window_manager.npm_tool_items
        index = window_manager.npm_tool_index
        if not items or index < 0 or index >= len(items):
            return {"CANCELLED"}

        if self.direction == "TOP":
            destination = 0
        elif self.direction == "UP":
            destination = max(0, index - 1)
        elif self.direction == "DOWN":
            destination = min(len(items) - 1, index + 1)
        else:
            destination = len(items) - 1

        if destination != index:
            items.move(index, destination)
            window_manager.npm_tool_index = destination
            _save_window_state(context)
        return {"FINISHED"}


class NPM_OT_set_toolbar_visibility(Operator):
    bl_idname = "view3d.npm_set_toolbar_visibility"
    bl_label = "Show All"
    bl_description = "Show or hide every toolbar button in this mode"
    bl_options = {"INTERNAL"}

    visible: BoolProperty(default=True, options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        return "Show every button" if properties.visible else "Hide every button"

    def execute(self, context):
        global _syncing_ui
        _syncing_ui = True
        try:
            for item in context.window_manager.npm_tool_items:
                item.enabled = self.visible
        finally:
            _syncing_ui = False
        _save_window_state(context)
        return {"FINISHED"}


class NPM_OT_restore_mode_defaults(Operator):
    bl_idname = "view3d.npm_restore_toolbar_defaults"
    bl_label = "Restore Blender Order"
    bl_description = "Show every button and restore Blender's default order for this mode"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        mode_name = context.window_manager.npm_edit_mode
        config = _read_config(context)
        config["modes"].pop(mode_name, None)
        _write_config(config, context)
        _populate_window_state(context, mode_name)
        _tag_view3d_redraw(context)
        return {"FINISHED"}


class NPM_OT_reset_all_modes(Operator):
    bl_idname = "preferences.npm_reset_all_toolbar_modes"
    bl_label = "Reset All Toolbar Modes"
    bl_description = "Remove every saved toolbar layout"

    def execute(self, context):
        prefs = _preferences(context)
        if prefs is not None:
            prefs.layouts_json = ""
        _tag_view3d_redraw(context)
        self.report({"INFO"}, "All toolbar layouts restored")
        return {"FINISHED"}


class NPM_AddonPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    layouts_json: StringProperty(default="", options={"HIDDEN"})

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Toolbar layouts are stored separately for each 3D View mode.")
        layout.operator(NPM_OT_reset_all_modes.bl_idname, icon="LOOP_BACK")


classes = (
    NPM_ToolListItem,
    NPM_UL_toolbar_tools,
    NPM_OT_open_toolbar_manager,
    NPM_PT_toolbar_manager,
    NPM_OT_move_toolbar_item,
    NPM_OT_set_toolbar_visibility,
    NPM_OT_restore_mode_defaults,
    NPM_OT_reset_all_modes,
    NPM_AddonPreferences,
)


def _patch_toolbar() -> None:
    global _original_tools_descriptor
    global _original_tools_function
    global _original_draw_function
    global _original_draw_was_local

    toolbar_class = VIEW3D_PT_tools_active
    _original_tools_descriptor = toolbar_class.__dict__["tools_from_context"]
    _original_tools_function = _original_tools_descriptor.__func__
    _original_draw_was_local = "draw" in toolbar_class.__dict__
    _original_draw_function = toolbar_class.draw

    toolbar_class.tools_from_context = classmethod(_managed_tools_from_context)
    toolbar_class.draw = _toolbar_manager_draw


def _restore_toolbar() -> None:
    toolbar_class = VIEW3D_PT_tools_active
    if _original_tools_descriptor is not None:
        toolbar_class.tools_from_context = _original_tools_descriptor
    if _original_draw_function is not None:
        if _original_draw_was_local:
            toolbar_class.draw = _original_draw_function
        elif "draw" in toolbar_class.__dict__:
            del toolbar_class.draw
    _tag_view3d_redraw()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.npm_tool_items = CollectionProperty(
        type=NPM_ToolListItem, options={"SKIP_SAVE"}
    )
    bpy.types.WindowManager.npm_tool_index = IntProperty(
        default=0, options={"SKIP_SAVE"}
    )
    bpy.types.WindowManager.npm_edit_mode = StringProperty(
        options={"HIDDEN", "SKIP_SAVE"}
    )
    _patch_toolbar()
    _tag_view3d_redraw()


def unregister():
    _restore_toolbar()

    del bpy.types.WindowManager.npm_edit_mode
    del bpy.types.WindowManager.npm_tool_index
    del bpy.types.WindowManager.npm_tool_items

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
