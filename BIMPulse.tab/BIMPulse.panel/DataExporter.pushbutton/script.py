# -*- coding: utf-8 -*-
import os
import csv
import datetime
import webbrowser
import clr
import json
import traceback
from pyrevit import revit, DB, UI, forms

# .NET and Windows Imports for Excel and Clipboard
clr.AddReference('System.Windows.Forms')
from System.Windows.Forms import Clipboard

# --- SETUP ---
doc = revit.doc
app = doc.Application

# ==========================================
# MASTER EXCLUSION FILTER
# ==========================================
EXCLUDED_CATEGORIES = set([
    "Cable Trays", "Cameras", "Conduit Runs", "Conduits", "Detail Items", 
    "Duct Insulations", "Ducts", "Flex Ducts", "Flex Pipes", 
    "MEP Fabrication Hangers", "Raster Images", "RVT Links", 
    "Sheets", "Symbols", "Wires"
])

# ==========================================
# CORE ENGINES
# ==========================================
def get_safe_id(element_id):
    if hasattr(element_id, "Value"):
        return str(element_id.Value)
    return str(element_id.IntegerValue)

def get_param_value(element, param_names):
    for name in param_names:
        if name == "Name" and hasattr(element, "Name"):
            return element.Name
        param = element.LookupParameter(name)
        if param and param.HasValue:
            return param.AsString() or param.AsValueString()
        if hasattr(element, 'GetTypeId'):
            type_id = element.GetTypeId()
            if type_id != DB.ElementId.InvalidElementId:
                elem_type = doc.GetElement(type_id)
                if elem_type:
                    type_param = elem_type.LookupParameter(name)
                    if type_param and type_param.HasValue:
                        return type_param.AsString() or type_param.AsValueString()
    return ""

# ==========================================
# 1. MODEL HEALTH (JSON EXPORTER)
# ==========================================
def get_model_health_data():
    revit_version = app.VersionNumber
    file_path = doc.PathName
    is_cloud = doc.IsModelInCloud
    
    file_size_mb = "Cloud Model" if is_cloud else (round(os.path.getsize(file_path)/(1024.0*1024.0), 2) if (file_path and os.path.exists(file_path)) else "Unsaved")

    families = DB.FilteredElementCollector(doc).OfClass(DB.Family).ToElements()
    inplace_count = sum(1 for f in families if f.IsInPlace)
    import_instances = DB.FilteredElementCollector(doc).OfClass(DB.ImportInstance).ToElements()
    cad_imports = sum(1 for inst in import_instances if not inst.IsLinked)

    warnings = doc.GetWarnings()
    total_warnings = warnings.Count if warnings else 0
    warning_dict = {}
    if total_warnings > 0:
        for w in warnings:
            # Force conversion to safe Unicode to prevent JSON serialization crashes
            desc = unicode(w.GetDescriptionText())
            failing_ids = [get_safe_id(id) for id in w.GetFailingElements()]
            if desc not in warning_dict: warning_dict[desc] = {"count": 0, "ids": set()}
            warning_dict[desc]["count"] += 1
            warning_dict[desc]["ids"].update(failing_ids)

    warnings_breakdown = []
    for desc, data in warning_dict.items():
        desc_lower = desc.lower()
        severity = "High" if any(x in desc_lower for x in ["not enclosed", "overlap", "coordinate"]) else ("Medium" if any(x in desc_lower for x in ["duplicate", "disconnected"]) else "Low")
        id_list = list(data["ids"])
        id_string = ", ".join(id_list[:15]) + "... (+" + str(len(id_list) - 15) + " more)" if len(id_list) > 15 else ", ".join(id_list)
        warnings_breakdown.append({"Warning Type": desc, "Count": data["count"], "Severity": severity, "Element IDs": id_string})

    warnings_breakdown.sort(key=lambda x: x.get("Count", 0), reverse=True)
    return {"metrics": {"revit_version": revit_version, "total_warnings": total_warnings, "file_size_mb": file_size_mb, "imported_cad": cad_imports, "inplace_families": inplace_count}, "warnings_breakdown": warnings_breakdown}

# ==========================================
# 2-3. CSV EXPORTERS
# ==========================================
def export_object_naming(export_folder, doc_title):
    filepath = os.path.join(export_folder, doc_title + "_Object_Naming.csv")
    elements = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements()
    
    # Standard Python 2 binary write mode - perfectly compatible with IronPython open()
    with open(filepath, 'wb') as f:
        writer = csv.writer(f)
        writer.writerow(["Element ID", "Category", "Family", "Type"])
        
        for el in elements:
            try:
                cat = el.Category
                if not cat or cat.Name.startswith("<") or cat.CategoryType != DB.CategoryType.Model or cat.Name in EXCLUDED_CATEGORIES: 
                    continue
                
                type_id = el.GetTypeId()
                if type_id == DB.ElementId.InvalidElementId: 
                    continue
                    
                elem_type = doc.GetElement(type_id)
                if not elem_type: 
                    continue
                
                fam_name = ""
                if hasattr(elem_type, 'FamilyName'):
                    fam_name = elem_type.FamilyName
                else:
                    fam_param = elem_type.get_Parameter(DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME)
                    if fam_param and fam_param.HasValue:
                        fam_name = fam_param.AsString()

                type_name = ""
                type_param = elem_type.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
                if type_param and type_param.HasValue:
                    type_name = type_param.AsString()
                elif hasattr(elem_type, 'Name'):
                    type_name = elem_type.Name
                
                if not type_name:
                    type_name = DB.Element.Name.__get__(elem_type)

                # --- THE IRONPYTHON 2.7 ENCODING LAYER ---
                def clean_and_encode(val):
                    if val is None:
                        return ""
                    # 1. Force conversion to a unicode string object
                    s = unicode(val)
                    # 2. Safely swap out un-encodable smart quotes
                    s = s.replace(u'\u201c', '"').replace(u'\u201d', '"')
                    s = s.replace(u'\u2018', "'").replace(u'\u2019', "'")
                    # 3. Strip out commas to protect CSV formatting, then encode to raw UTF-8 bytes
                    return s.replace(",", "-").strip().encode('utf-8')

                writer.writerow([
                    get_safe_id(el.Id),
                    clean_and_encode(cat.Name), 
                    clean_and_encode(fam_name), 
                    clean_and_encode(type_name)
                ])
                
            except Exception:
                pass
    return filepath

def export_uniclass(export_folder, doc_title):
    filepath = os.path.join(export_folder, doc_title + "_Uniclass_Audit.csv")
    all_elements = list(DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements()) + list(DB.FilteredElementCollector(doc).WhereElementIsElementType().ToElements()) + list(DB.FilteredElementCollector(doc).OfClass(DB.MEPSystem).ToElements())
    
    with open(filepath, 'wb') as f:
        writer = csv.writer(f)
        writer.writerow(["Element ID", "Category", "Uniclass Pr", "Uniclass Pr Description", "Uniclass Ss", "Uniclass Ss Description", "Uniclass EF", "Uniclass EF Description"])
        
        # Reusable IronPython 2.7 encoding safe-guard for Uniclass
        def clean_and_encode_uniclass(val, allow_commas=True):
            if val is None:
                return ""
            # 1. Force conversion to a unicode string object
            s = unicode(val)
            # 2. Safely swap out un-encodable smart characters
            s = s.replace(u'\u201c', '"').replace(u'\u201d', '"')
            s = s.replace(u'\u2018', "'").replace(u'\u2019', "'")
            s = s.replace(u'\u2013', "-").replace(u'\u2014', "-") # Cleans complex en/em-dashes
            
            if not allow_commas:
                s = s.replace(",", "-")
                
            # 3. Encode to raw UTF-8 bytes so the CSV writer processes it perfectly
            return s.strip().encode('utf-8')

        for el in all_elements:
            try:
                cat = el.Category
                if not cat or cat.Name.startswith("<") or cat.Name in EXCLUDED_CATEGORIES: 
                    continue
                    
                pr = get_param_value(el, ["Uniclass Pr", "Uniclass_Pr", "Uniclass2015_Pr", "Classification.Uniclass.Pr.Number"])
                pr_d = get_param_value(el, ["Uniclass Pr Description", "Uniclass_Pr_Desc", "Uniclass2015_Pr_Description", "Classification.Uniclass.Pr.Description"])
                ss = get_param_value(el, ["Uniclass Ss", "Uniclass_Ss", "Uniclass2015_Ss", "Classification.Uniclass.Ss.Number"])
                ss_d = get_param_value(el, ["Uniclass Ss Description", "Uniclass_Ss_Desc", "Uniclass2015_Ss_Description", "Classification.Uniclass.Ss.Description"])
                ef = get_param_value(el, ["Uniclass EF", "Uniclass_EF", "Uniclass2015_EF", "Classification.Uniclass.EF.Number"])
                ef_d = get_param_value(el, ["Uniclass EF Description", "Uniclass_EF_Desc", "Uniclass2015_EF_Description", "Classification.Uniclass.EF.Description"])
                
                if any([pr, ss, ef]): 
                    # We ALLOW commas in Descriptions because csv.writer handles them natively 
                    # by wrapping the text block in quotes, as long as it's pre-encoded to raw bytes!
                    writer.writerow([
                        get_safe_id(el.Id),
                        clean_and_encode_uniclass(cat.Name, allow_commas=False), 
                        clean_and_encode_uniclass(pr, allow_commas=False), 
                        clean_and_encode_uniclass(pr_d, allow_commas=True), 
                        clean_and_encode_uniclass(ss, allow_commas=False), 
                        clean_and_encode_uniclass(ss_d, allow_commas=True), 
                        clean_and_encode_uniclass(ef, allow_commas=False), 
                        clean_and_encode_uniclass(ef_d, allow_commas=True)
                    ])
            except Exception: 
                pass
    return filepath

# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    export_folder = forms.pick_folder()
    if not export_folder: return
        
    doc_title = doc.Title.replace(".rvt", "")
    
    # 1. JSON Export
    # 1. JSON Export
    health_data = get_model_health_data()
    json_path = os.path.join(export_folder, "HealthReport_" + doc_title + ".json")
    
    # Changed 'w' to 'wb' for binary compatibility with IronPython json.dump
    with open(json_path, 'wb') as f:
        json.dump(health_data, f, indent=4)
        
    # 2. CSV Audits
    export_object_naming(export_folder, doc_title)
    export_uniclass(export_folder, doc_title)
    
    Clipboard.SetText(export_folder)
    webbrowser.open('https://bimpulse.whitecode.co.uk')
    
    forms.alert("Model Health, Object Naming and Uniclass files exported.\n\nThe folder path is copied: upload the files in BIMPulse.",
                title="Export Complete", warn_icon=False)

if __name__ == '__main__':
    main()