# -*- coding: utf-8 -*-
from pyrevit import revit, DB, forms, script
import math
import json
import traceback

doc = revit.doc

# --- UNIT CONVERSION HELPERS ---
def feet_to_mm(feet_val):
    """Converts internal decimal feet to millimeters."""
    return round(feet_val * 304.8, 1)

def rad_to_deg(rad_val):
    """Converts internal radians to degrees and matches Revit's inverted True North UI."""
    deg = math.degrees(rad_val)
    # Normalize to 0-360
    deg = deg % 360.0
    
    # Revit UI displays True North rotation inverted relative to the API's mathematical angle
    if deg != 0.0:
        deg = 360.0 - deg
        
    if deg == 360.0:
        deg = 0.0
        
    return round(deg, 2)

def get_bp_data(document):
    """Safely extracts the exact visual parameters directly from the Project Base Point element."""
    ew, ns, elev, angle = 0.0, 0.0, 0.0, 0.0
    
    # Safely get angle from ActiveProjectLocation as a highly reliable baseline
    try:
        loc = document.ActiveProjectLocation
        if loc:
            pos = loc.GetProjectPosition(DB.XYZ.Zero)
            angle = pos.Angle
    except Exception:
        pass
        
    try:
        # Use the explicit BasePoint class to safely grab coordinates (Works for Host & Links)
        base_points = DB.FilteredElementCollector(document).OfClass(DB.BasePoint).ToElements()
        
        for bp in base_points:
            # IsShared == False guarantees we are looking at the Project Base Point, not the Survey Point
            if not bp.IsShared:
                # SharedPosition pulls the exact E/W, N/S, Elev values shown in the properties panel
                ew = bp.SharedPosition.X
                ns = bp.SharedPosition.Y
                elev = bp.SharedPosition.Z
                
                # Override angle if the parameter is explicitly available on the point itself
                angle_param = bp.get_Parameter(DB.BuiltInParameter.BASEPOINT_ANGLETONRTH_PARAM)
                if angle_param and angle_param.HasValue:
                    angle = angle_param.AsDouble()
                break
    except Exception:
        pass # If anything fails, it falls back to the baseline 0.0 safety net
        
    return ew, ns, elev, angle

def check_compliance():
    # 1. FIND AND SELECT LINKED MODELS
    links = DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements()
    
    link_docs = {}
    for link in links:
        linked_doc = link.GetLinkDocument()
        if linked_doc:
            link_docs[linked_doc.Title] = linked_doc

    if not link_docs:
        forms.alert("No loaded linked models found in the project.", exitscript=True)

    # Trigger the selection window BEFORE the output window claims the UI thread
    selected_title = forms.SelectFromList.show(
        list(link_docs.keys()),
        title="Select Linked Model for Comparison",
        button_name="Compare"
    )

    # Safe exit if user clicks cancel
    if not selected_title:
        script.exit()
        
    # Safeguard: pyRevit sometimes returns a list of 1 string instead of a direct string
    if isinstance(selected_title, list):
        if len(selected_title) > 0:
            selected_title = selected_title[0]
        else:
            script.exit()

    # 2. INITIALIZE OUTPUT WINDOW 
    output = script.get_output()

    # 3. WRAP MAIN EXECUTION IN TRY/EXCEPT TO PREVENT SILENT BLANK CRASHES
    try:
        # --- INJECT DASHBOARD CSS ---
        css = """
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
            h2, h3 { color: #333; margin-bottom: 5px; }
            .dashboard-panel { border: 1px solid #ccc; border-radius: 8px; padding: 15px; margin-bottom: 20px; background-color: #fafafa; }
            .data-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }
            .data-table th, .data-table td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            .data-table th { background-color: #eee; font-weight: bold; }
            .row-match { background-color: #d4edda !important; color: #155724; }
            .row-mismatch { background-color: #f8d7da !important; color: #721c24; }
            .status-pass { color: #28a745; font-weight: bold; }
            .status-fail { color: #dc3545; font-weight: bold; }
            .summary-banner { padding: 15px; border-radius: 8px; font-size: 18px; font-weight: bold; margin-bottom: 20px; }
            .banner-pass { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .banner-fail { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        </style>
        """
        output.print_html(css)

        link_doc = link_docs[selected_title]
        compliance_issues = 0
        tolerance = 0.001 
        
        # 4. CHECK COORDINATES DIRECTLY FROM BASE POINT ELEMENT
        host_ew, host_ns, host_elev, host_angle = get_bp_data(doc)
        link_ew, link_ns, link_elev, link_angle = get_bp_data(link_doc)

        # Protect against 360 vs 0 degree mathematical wrap-around inside the API
        angle_diff = abs(host_angle - link_angle)
        angle_match = angle_diff < tolerance or abs(angle_diff - 2 * math.pi) < tolerance

        coord_compliant = (
            abs(host_ew - link_ew) < tolerance and
            abs(host_ns - link_ns) < tolerance and
            abs(host_elev - link_elev) < tolerance and
            angle_match
        )
        
        if not coord_compliant:
            compliance_issues += 1

        coord_checks = [
            {"name": "East/West", "host": feet_to_mm(host_ew), "link": feet_to_mm(link_ew)},
            {"name": "North/South", "host": feet_to_mm(host_ns), "link": feet_to_mm(link_ns)},
            {"name": "Elevation", "host": feet_to_mm(host_elev), "link": feet_to_mm(link_elev)},
            {"name": "Angle", "host": rad_to_deg(host_angle), "link": rad_to_deg(link_angle)}
        ]

        coord_html = """
        <div class="dashboard-panel">
            <h3>🌍 Coordinates Comparison</h3>
            <table class="data-table">
                <tr>
                    <th>Parameter</th>
                    <th>Host (Current)</th>
                    <th>Link (Expected)</th>
                    <th>Status</th>
                </tr>
        """

        for item in coord_checks:
            match = abs(item["host"] - item["link"]) < 0.1
            # Secondary safety check for 360 degree matching in UI representation
            if item["name"] == "Angle" and abs(item["host"] - item["link"]) >= 359.9:
                match = True
                
            if not match:
                row_class = "row-mismatch"
                status_text = "Mismatch"
            else:
                row_class = "row-match"
                status_text = "Match"
                
            coord_html += """
                <tr class="{0}">
                    <td>{1}</td>
                    <td>{2}</td>
                    <td>{3}</td>
                    <td>{4}</td>
                </tr>
            """.format(row_class, item['name'], item['host'], item['link'], status_text)
        coord_html += "</table></div>"
        
        coordinates_data = {
            "compliant": coord_compliant,
            "host": {
                "East/West": feet_to_mm(host_ew),
                "North/South": feet_to_mm(host_ns),
                "Elevation": feet_to_mm(host_elev),
                "Angle": rad_to_deg(host_angle)
            },
            "link": {
                "East/West": feet_to_mm(link_ew),
                "North/South": feet_to_mm(link_ns),
                "Elevation": feet_to_mm(link_elev),
                "Angle": rad_to_deg(link_angle)
            }
        }

        # 5. CHECK LEVELS
        host_levels = {l.Name: l.Elevation for l in DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()}
        link_levels = {l.Name: l.Elevation for l in DB.FilteredElementCollector(link_doc).OfClass(DB.Level).ToElements()}

        all_level_names = sorted(set(host_levels.keys()).union(set(link_levels.keys())))
        
        level_html = """
        <div class="dashboard-panel">
            <h3>🏢 Levels Comparison</h3>
            <table class="data-table">
                <tr>
                    <th>Level Name</th>
                    <th>Host Elevation (Current)</th>
                    <th>Expected Elevation (Link)</th>
                    <th>Status</th>
                </tr>
        """

        levels_details = []
        level_issues = 0
        
        for name in all_level_names:
            h_elev = host_levels.get(name, None)
            l_elev = link_levels.get(name, None)

            if h_elev is None:
                status = "Missing in Host"
                row_class = "row-mismatch"
                level_issues += 1
                compliance_issues += 1
            elif l_elev is None:
                status = "Missing in Link"
                row_class = "row-mismatch"
                level_issues += 1
                compliance_issues += 1
            elif abs(h_elev - l_elev) > tolerance:
                status = "Mismatch"
                row_class = "row-mismatch"
                level_issues += 1
                compliance_issues += 1
            else:
                status = "Match"
                row_class = "row-match"

            h_val_mm = feet_to_mm(h_elev) if h_elev is not None else "N/A"
            l_val_mm = feet_to_mm(l_elev) if l_elev is not None else "N/A"

            level_html += """
                <tr class="{0}">
                    <td>{1}</td>
                    <td>{2}</td>
                    <td>{3}</td>
                    <td>{4}</td>
                </tr>
            """.format(row_class, name, h_val_mm, l_val_mm, status)
            
            levels_details.append({
                "Level Name": name,
                "Host Elevation (Current)": h_val_mm,
                "Expected Elevation (Link)": l_val_mm,
                "Status": status
            })

        level_html += "</table></div>"

        # 6. RENDER DASHBOARD HEADER & HTML
        output.print_html("<h2>QA/QC Report: Host vs <code>{}</code></h2>".format(selected_title))
        
        if compliance_issues == 0:
            output.print_html("<div class='summary-banner banner-pass'>🎉 100% Compliant! No issues found.</div>")
        else:
            output.print_html("<div class='summary-banner banner-fail'>🚨 Non-Compliant: {} total issues found.</div>".format(compliance_issues))

        output.print_html(coord_html)
        output.print_html(level_html)

        # 7. EXPORT JSON FOR BIMPULSE
        report_data = {
            "host_model": doc.Title,
            "linked_model": selected_title,
            "overall_compliant": (compliance_issues == 0),
            "total_issues": compliance_issues,
            "coordinates": coordinates_data,
            "levels": {
                "compliant": (level_issues == 0),
                "details": levels_details
            }
        }
            
        export_path = forms.save_file(file_ext='json', default_name='QAQC_Report_{}'.format(doc.Title))
        
        if export_path:
            with open(export_path, 'w') as json_file:
                json.dump(report_data, json_file, indent=4)
            output.print_md("📁 **Raw data exported to:** `{}`".format(export_path))

    except Exception as e:
        # FATAL ERROR CATCH: Prints exact line and reason for failure
        output.print_html("<div class='summary-banner banner-fail'>🚨 CRITICAL ERROR ENCOUNTERED</div>")
        output.print_html("<pre style='background:#f8f9fa; padding:15px; border: 1px solid #ccc;'>{}</pre>".format(traceback.format_exc()))

if __name__ == '__main__':
    check_compliance()