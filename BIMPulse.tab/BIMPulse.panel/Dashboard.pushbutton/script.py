# -*- coding: utf-8 -*-
"""Opens the BIMPulse Dashboard: your BIMPulse audit issues in Revit, live with your team.
(The dashboard itself is lib/bimpulse_dashboard.py; it can also dock beside the model.)"""
__title__ = "BIMPulse Dashboard"
__author__ = "Whitecode Consulting"
__persistentengine__ = True  # the window is modeless and drives Revit through an ExternalEvent

import traceback

from pyrevit import forms

try:
    import bimpulse_dashboard as dashboard
    dashboard.open_window()
except Exception:
    detail = traceback.format_exc()
    try:
        dashboard.debug("could not open: " + detail)
    except Exception:
        pass
    forms.alert("The BIMPulse Dashboard could not open.\n\n" + detail, title="BIMPulse Dashboard")
