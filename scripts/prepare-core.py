"""Generate a reviewable, headless subset of upstream SW2URDF source."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "vendor" / "solidworks_urdf_exporter" / "SW2URDF"
DEST = ROOT / "build" / "core-source"
DEST.mkdir(parents=True, exist_ok=True)
files = []
for folder in ("URDF", "ROS", "Legacy", "Versioning", "Utilities", "URDFExport/CSV"):
    files.extend((UPSTREAM / folder).glob("*.cs"))
for name in ("ExportHelper.cs", "ExportHelperExtension.cs", "CommonSwOperations.cs", "ConfigurationSerialization.cs", "URDFPackage.cs"):
    files.append(UPSTREAM / "URDFExport" / name)
files.append(UPSTREAM / "UI" / "IMessageBox.cs")
manifest = []
problems = []
TOOL_HOOKS = '''        internal string CreateToolBaseFrame(bool zIsUp)
        {
            CreateBaseRefOrigin(zIsUp);
            return "Origin_global";
        }

        // Creates a coordinate system at a pose in assembly coordinates under a unique name.
        internal string CreateToolFrame(Matrix<double> pose, string name)
        {
            if (referenceSketchName == null)
            {
                referenceSketchName = Setup3DSketch();
            }
            string unique = name;
            for (int i = 2; ActiveSWModel.Extension.SelectByID2(unique, "COORDSYS", 0, 0, 0, false, 0, null, 0); i++)
            {
                unique = name + i.ToString();
            }
            ActiveSWModel.ClearSelection2(true);
            Origin origin = new Origin(true);
            origin.SetXYZ(MathOps.GetXYZ(pose));
            origin.SetRPY(MathOps.GetRPY(pose));
            CreateRefOrigin(origin, unique);
            ActiveSWModel.ClearSelection2(true);
            return unique;
        }

        internal MathTransform GetToolFrameTransform(string name)
        {
            return GetCoordinateSystemTransform(name);
        }
'''


def patch(text, relative, old, new, expected):
    # Upstream drift must stop the build, never silently skip an adaptation.
    found = text.count(old)
    if found != expected:
        problems.append(f"{relative}: expected {expected} match(es), found {found}: {old[:60]!r}")
    return text.replace(old, new)


for source in files:
    relative = source.relative_to(UPSTREAM)
    target = DEST / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    original = source.read_text(encoding="utf-8-sig")
    text = original
    key = relative.as_posix()
    # Errors must become exceptions, never dialogs, when running outside SW UI.
    if key == "URDFExport/ExportHelper.cs":
        text = patch(text, key, "System.Windows.Forms.MessageBox.Show(", "SW2URDF.Headless.Errors.Show(", 1)
        text = patch(text, key, "MessageBox.Show(", "SW2URDF.Headless.Errors.Show(", 1)
    if key == "URDFExport/ExportHelperExtension.cs":
        text = patch(text, key, "MessageBox.Show(", "SW2URDF.Headless.Errors.Show(", 1)
    if key == "URDFExport/ConfigurationSerialization.cs":
        text = patch(text, key, "MessageBox.Show(", "SW2URDF.Headless.Errors.Show(", 4)
    if key == "URDFExport/ExportHelperExtension.cs":
        text = patch(text, key, "bool error = CreateJoint(parent, node.Link);\n                if (error)", "bool success = CreateJoint(parent, node.Link);\n                if (!success)", 1)
        text = patch(text, key, "if (ComputeInertialValues)", "if (ComputeInertialValues && !node.Link.isFixedFrame)", 1)
        text = patch(text, key, "if (ComputeVisualCollision)", "if (ComputeVisualCollision && !node.Link.isFixedFrame)", 1)
        text = patch(text, key, "MassProperty swMass = ActiveSWModel.Extension.CreateMassProperty();", "MassProperty swMass = ActiveSWModel.Extension.CreateMassProperty();\n            swMass.UseSystemUnits = true;", 3)
        text = patch(text, key, "IMassProperty swMass = swModel.Extension.CreateMassProperty();", "IMassProperty swMass = swModel.Extension.CreateMassProperty();\n            swMass.UseSystemUnits = true;", 1)
        # Hooks for the tool: build link frames from configured joints and read them back.
        text = patch(text, key, "        //Creates the Origin_global coordinate system\n", TOOL_HOOKS + "\n        //Creates the Origin_global coordinate system\n", 1)
    if key == "URDF/Link.cs":
        for name in ("Inertial", "Visual", "Collision"):
            text = patch(text, key, f"if ({name} != null)", f"if ({name} != null && !isFixedFrame)", 1)
    if key == "URDFExport/ExportHelper.cs":
        text = patch(text, key, "if (!child.isFixedFrame)\n                {\n                    ExportFiles(child, package, count, exportSTL, meshFormat);\n                }", "ExportFiles(child, package, count, exportSTL, meshFormat);", 1)
        text = patch(text, key, "// Copy the texture file (if it was specified) to the textures directory", "if (link.isFixedFrame) return;\n\n            // Copy the texture file (if it was specified) to the textures directory", 1)
    if key == "URDF/URDFAttribute.cs":
        text = patch(text, key, "Value.GetType()", "Value?.GetType()", 5)
    target.write_text(text, encoding="utf-8")
    manifest.append({"source": str(relative), "source_sha256": hashlib.sha256(original.encode()).hexdigest(), "adapted": text != original})

if problems:
    raise SystemExit("Upstream source no longer matches the headless adaptations:\n" + "\n".join(problems))

(DEST / "Headless.cs").write_text('''using System;
using System.Windows;
using System.Windows.Forms;
namespace SW2URDF.Headless {
    public static class Errors {
        public static void Show(string message) { throw new InvalidOperationException(message); }
        public static DialogResult Show(string message, string caption, MessageBoxButtons buttons) { throw new InvalidOperationException(caption + ": " + message); }
    }
}
namespace SW2URDF.UI {
    public sealed class MessageBoxHelper : IMessageBox {
        public MessageBoxResult Show(string message) { Console.Error.WriteLine(message); return MessageBoxResult.OK; }
        public MessageBoxResult Show(string message, string caption, MessageBoxButton buttons) { throw new InvalidOperationException(caption + ": " + message); }
    }
}
''', encoding="utf-8")
(DEST / "AssemblyInfo.cs").write_text('''using System.Reflection;
[assembly: AssemblyTitle("SolidWorks URDF 2026 Headless Core")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]
[assembly: AssemblyInformationalVersion("2.0.0-sw2026")]
''', encoding="utf-8")
(ROOT / "build" / "core-source-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(f"Prepared {len(files)} upstream source files with explicit headless adaptations.")
