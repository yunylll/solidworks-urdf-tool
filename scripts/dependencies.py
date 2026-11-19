"""Classify the saved references of a model (the native dependency scan) into errors, warnings and fixes.

The native tool lists every saved reference of the model and of each file it references, with
GetDocumentDependencies2 and without opening any document (bridge.dependencyScan). This module decides,
consistently with the snapshot logic (ToolOperations.StageSourceSnapshot / RequireLoadedComponents):

* a missing file that an active component uses fails (it fails later when the snapshot opens anyway);
* a missing file that only suppressed components use, or that no component uses, is a warning;
* without a component list (no open document, no earlier job of this CAD version) a missing file is a
  warning: the export fails only if an active component turns out to use it;
* 3D Interconnect caches (...\\Temp\\swx*\\IC~~\\) are warnings, virtual components (...\\VC~~\\...^...) are
  stored inside their assembly and fine, existing files outside the model folder are copied (info);
* missing STEP/IGES/... files a part was imported from are warnings (the part keeps its bodies);
* a snapshot or Pack and Go destination longer than Windows' MAX_PATH fails.
"""
from __future__ import annotations

import ntpath
import re

MAX_PATH = 259
IMPORT_CACHE = re.compile(r"\\Temp\\swx[^\\]+\\IC~~\\", re.I)
VIRTUAL = re.compile(r"\\Temp\\swx[^\\]+\\VC~~\\", re.I)
USER_PATH = re.compile(r"^[A-Za-z]:\\Users\\([^\\]+)\\", re.I)
LIBRARY = re.compile(r"\\SOLIDWORKS Data[^\\]*\\|\\Toolbox\\|\\Design Library\\|\\SolidWorks parts library\\", re.I)
# The native snapshot folder name: 16 hexadecimal characters per source directory.
SNAPSHOT_KEY = 16


def is_virtual(path):
    return "^" in ntpath.basename(path or "") and bool(VIRTUAL.search(path or ""))


def kind_of(path, exists, user, model_dir):
    if is_virtual(path):
        return "virtual"
    if IMPORT_CACHE.search(path):
        return "import_cache"
    if exists:
        inside = ntpath.normcase(ntpath.abspath(path)).startswith(ntpath.normcase(ntpath.abspath(model_dir)).rstrip("\\") + "\\")
        return "file" if inside else "external"
    if not path.lower().endswith((".sldprt", ".sldasm")):
        # A STEP/IGES/... file a part was imported from: the part keeps the bodies it imported.
        return "import_source"
    if LIBRARY.search(path):
        return "library"
    owner = USER_PATH.match(path)
    if owner and owner.group(1).lower() not in {(user or "").lower(), "public"}:
        return "foreign_user"
    return "missing"


def suggestion(kind, path, user):
    name = ntpath.basename(path)
    if kind == "library":
        return (f"Toolbox / Design Library file not on this computer. Install the library at this path, or point SolidWorks to the folder that has it "
                f"(Tools > Options > Hole Wizard/Toolbox, File Locations), or copy {name} into the model folder (SolidWorks looks there first), "
                f"or suppress the component in the active configuration and save.")
    if kind == "foreign_user":
        owner = USER_PATH.match(path).group(1)
        return (f"Saved on another computer or account (C:\\Users\\{owner}, this account is {user}). Copy {name} into the model folder, "
                f"or replace the component with a file on this computer and save the assembly.")
    if kind == "import_cache":
        return ("A 3D Interconnect import cache from another SolidWorks session; the native part keeps its saved bodies, so nothing is needed "
                "unless that part exports empty. Otherwise break the link or re-import the source file and save.")
    if kind == "import_source":
        return f"The file {name} a part was imported from; the part keeps its imported bodies, so nothing is needed unless you want to update the import."
    if kind == "missing":
        return f"Restore {name} at this path or in the model folder, or suppress or replace the component in the active configuration and save."
    if kind == "external":
        return "Outside the model folder; the tool copies it into its snapshot. Pack and Go in SolidWorks keeps the model self-contained."
    return None


def classify(scan, known_components=None, known_source=None):
    """dependency_check from bridge.dependencyScan; known_components: [{name, path, suppressed}] or None."""
    references = scan.get("references") or []
    model = scan.get("model") or ""
    user = scan.get("user") or ""
    output = scan.get("output") or ""
    model_dir = ntpath.dirname(model)
    by_name = {}
    for component in known_components or []:
        by_name.setdefault(ntpath.basename(component.get("path") or "").lower(), []).append(component)
    rows, errors, warnings, counts = [], [], [], {}
    for ref in references:
        path = ref["path"]
        if ntpath.normcase(path) == ntpath.normcase(model):
            continue
        exists = bool(ref.get("exists"))
        kind = kind_of(path, exists, user, model_dir)
        parents = ref.get("referenced_by") or []
        row = {"path": path, "kind": kind, "exists": exists, "referenced_by": parents}
        where = f" (referenced by {', '.join(ntpath.basename(p) for p in parents[:3])}{', ...' if len(parents) > 3 else ''})" if parents else ""
        if not exists and kind != "virtual":
            name = ntpath.basename(path).lower()
            users = by_name.get(name, [])
            active = [c["name"] for c in users if not c.get("suppressed")]
            suppressed = [c["name"] for c in users if c.get("suppressed")]
            if kind in {"import_cache", "import_source"}:
                row["usage"] = kind
                severity = "warning"
            elif known_components is None:
                row["usage"] = "unknown"
                severity = "warning"
            elif active:
                row["usage"], row["active_components"] = "active", active
                severity = "error"
            elif suppressed:
                row["usage"], row["suppressed_components"] = "suppressed", suppressed
                severity = "warning"
            else:
                row["usage"] = "unused"
                severity = "warning"
            row["severity"] = severity
            row["suggestion"] = suggestion(kind, path, user)
            label = {"library": "Toolbox/library file missing", "foreign_user": "File on another computer", "import_cache": "Import cache missing", "missing": "File missing", "import_source": "Import source file missing"}[kind]
            usage = {"active": f"used by active component(s) {', '.join(active[:5])}{', ...' if len(active) > 5 else ''} - the export fails", "suppressed": f"used only by suppressed component(s) {', '.join(suppressed[:5])}",
                     "unused": "no component of the active configuration uses it", "unknown": "fails only if an active component uses it (no component list yet: run inspect, or open the model in SolidWorks)",
                     "import_cache": "the native part loads its saved bodies", "import_source": "the part keeps its imported bodies"}[row["usage"]]
            (errors if severity == "error" else warnings).append(f"{label}: {path}{where}; {usage}. Fix: {row['suggestion']}")
        else:
            row["severity"] = "info" if kind == "external" else "ok"
            if kind == "external":
                row["suggestion"] = suggestion(kind, path, user)
        if exists and kind not in {"virtual", "import_cache"} and output:
            name = ntpath.basename(path)
            cad = ntpath.join(output, "cad", name)
            snapshot = ntpath.join(output, "snapshot", "0" * SNAPSHOT_KEY, name)
            longest = max(len(cad), len(snapshot))
            row["destination_length"] = longest
            if longest > MAX_PATH:
                row["severity"] = "error"
                row["suggestion"] = f"Shorten the file name {name!r} ({len(name)} characters) or move the tool workspace to a shorter folder: the copy would be {longest} characters, Windows allows {MAX_PATH}."
                errors.append(f"Path too long for the snapshot/Pack and Go copy: {path} -> {longest} characters (limit {MAX_PATH}). Fix: {row['suggestion']}")
        counts[kind] = counts.get(kind, 0) + 1
        rows.append(row)
    names = {}
    for row in rows:
        if row["exists"] and row["kind"] in {"file", "external"}:
            names.setdefault(ntpath.basename(row["path"]).lower(), []).append(row["path"])
    duplicates = {k: v for k, v in names.items() if len(v) > 1}
    result = {"passed": not errors, "errors": errors, "warnings": warnings, "counts": counts, "references": rows, "seconds": scan.get("seconds"),
              "usage_source": known_source if known_components is not None else None}
    if duplicates:
        result["duplicate_file_names"] = duplicates
    result["summary"] = (f"{len(rows)} reference(s): " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + f"; {len(errors)} error(s), {len(warnings)} warning(s)"
                         + (f"; component usage from {known_source}" if known_components is not None else "; component usage unknown"))
    return result


def describe(check):
    """One message listing every error of a dependency check."""
    lines = [f"Dependency check failed: {len(check['errors'])} problem(s) must be fixed before the model can be exported ({check['summary']})."]
    lines += [f"{i}. {e}" for i, e in enumerate(check["errors"], 1)]
    if check["warnings"]:
        lines.append(f"Also {len(check['warnings'])} warning(s), see dependency_check.warnings.")
    return "\n".join(lines)
