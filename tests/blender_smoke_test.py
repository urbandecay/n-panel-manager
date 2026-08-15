"""Run with Blender in background mode; no user preferences are changed."""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

import bpy


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import n_panel_manager as addon  # noqa: E402


fake_preferences = SimpleNamespace(layouts_json="")
addon._preferences = lambda _context=None: fake_preferences

addon.register()
try:
    toolbar_class = addon.VIEW3D_PT_tools_active
    assert (
        toolbar_class.__dict__["tools_from_context"].__func__
        is addon._managed_tools_from_context
    )

    records = addon._base_records(bpy.context, "OBJECT")
    assert len(records) >= 10
    assert any(record["group_size"] > 1 for record in records)

    addon._populate_window_state(bpy.context, "OBJECT")
    state = bpy.context.window_manager.npm_tool_items
    assert len(state) == len(records)
    assert sum(item.icon_value > 0 for item in state) >= len(state) - 1

    original_first = state[0].identifier
    bpy.context.window_manager.npm_tool_index = 0
    result = addon.NPM_OT_move_toolbar_item.execute(
        SimpleNamespace(direction="BOTTOM"), bpy.context
    )
    assert result == {"FINISHED"}
    assert state[-1].identifier == original_first

    state[0].enabled = False
    saved = json.loads(fake_preferences.layouts_json)["modes"]["OBJECT"]
    assert saved[0]["enabled"] is False
    visible = [
        entry
        for entry in addon._managed_tools_from_context(
            addon.VIEW3D_PT_tools_active, bpy.context, "OBJECT"
        )
        if entry is not None
    ]
    assert len(visible) == len(records) - 1

    addon.NPM_OT_restore_mode_defaults.execute(SimpleNamespace(), bpy.context)
    assert "OBJECT" not in json.loads(fake_preferences.layouts_json)["modes"]
finally:
    addon.unregister()

assert (
    addon.VIEW3D_PT_tools_active.__dict__["tools_from_context"].__func__
    is not addon._managed_tools_from_context
)
print("N-Panel Manager Blender smoke test: PASS")
