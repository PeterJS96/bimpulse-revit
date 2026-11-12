# BIMPulse for Revit

A pyRevit extension by [Whitecode Consulting](https://www.whitecode.co.uk) for
[BIMPulse](https://bimpulse.whitecode.co.uk), the BIM audit platform. It adds a **BIMPulse** tab
to Revit with three tools:

| | |
|---|---|
| **BIMPulse Dashboard** | Your BIMPulse audit issues inside Revit. Sign in, open a saved audit, then select, zoom, isolate or show the elements in 3D, fix values in the model, and resolve or comment on issues. Everything is shared live with the project team on the BIMPulse website. |
| **Data Exporter** | Exports the model's data for a BIMPulse audit: Model Health (warnings, CAD imports, in-place families) as JSON, and the Object Naming and Uniclass schedules as CSV. |
| **Geo & Levels Exporter** | Compares the model's Project Base Point and levels with a linked model, shows what matches, and exports the JSON for the Geolocation & Levels audit. |

## Install

1. **Install pyRevit** (free) from https://github.com/pyrevitlabs/pyRevit/releases. Pick the
   installer for your user: it doesn't need administrator rights. Close Revit first.
2. **Add BIMPulse**: open Command Prompt and run

   ```
   pyrevit extend ui BIMPulseTools https://github.com/PeterJS96/bimpulse-revit.git
   ```

   If it says `'pyrevit' is not recognized` (pyRevit installed for your user only), run:

   ```
   "%APPDATA%\pyRevit-Master\bin\pyrevit.exe" extend ui BIMPulseTools https://github.com/PeterJS96/bimpulse-revit.git
   ```

   Or in Revit: **pyRevit › Extensions**, and install an extension from that address.
3. Open Revit (or click **pyRevit › Reload**). The **BIMPulse** tab appears.

**Updating**: click **pyRevit › Update**, or run `pyrevit extensions update BIMPulseTools`.

Needs Revit 2021 or later and pyRevit 4.8 or later.

## Sign in

Click **BIMPulse Dashboard**, then **Browse BIMPulse**. Your browser opens the BIMPulse
**Connect Revit** page: sign in there with your BIMPulse account and click **Connect Revit**.
Your password is never typed into Revit, and Revit stays signed in until you sign out.

Working for a design consultant? The project team shares their project with your email address,
and you then see only your own company's issues.

## Help

Contact the team that invited you to BIMPulse, or use **Report a problem** on the BIMPulse
website.
