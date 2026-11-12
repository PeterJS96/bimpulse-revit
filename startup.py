# -*- coding: utf-8 -*-
"""Runs when Revit (or pyRevit) starts: registers the BIMPulse Dashboard's docked panel, so
"Dock beside the model" in the Dashboard's menu can show it. Revit only accepts panels while it
starts; nothing loads until the panel is first shown."""
import traceback

try:
    import bimpulse_dashboard as dashboard
    try:
        dashboard.register_panel()
    except Exception:
        # Already registered (pyRevit reloaded), or an older pyRevit without docked panels.
        dashboard.debug("docked panel not registered: " + traceback.format_exc())
except Exception:
    pass  # the Dashboard button still works as a window
