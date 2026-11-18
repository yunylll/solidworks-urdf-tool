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
            // Every frame gets its own 3D sketch. In the shared sketch SolidWorks merges new geometry
            // that coincides with an earlier frame's, and the new frame then lands 1 cm along its Y
            // axis from the requested origin (every joint sharing a position with an earlier one).
            if (ActiveSWModel.SketchManager.ActiveSketch != null)
            {
                ActiveSWModel.SketchManager.Insert3DSketch(true);
                ActiveSWModel.ClearSelection2(true);
            }
            ActiveSWModel.SketchManager.Insert3DSketch(true);
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
        # Progress for job queries: one step per Link built.
        text = patch(text, key, 'progressBar.UpdateTitle("Building link: " + node.Name);', 'progressBar.UpdateTitle("Building link: " + node.Name);\n            SW2URDF.Headless.Progress.Step(node.Name);', 1)
    if key == "URDFExport/CommonSwOperations.cs":
        # Every show, hide, fix and unfix of the export selects its components first. Appending one
        # component at a time costs time in proportion to the selection already made: 800 parts of the
        # workstation took 194 s with Select4, 0.9 s with one MultiSelect2 (joint recomputation spent
        # 15 of every 15.5 minutes per Link fixing and unfixing about 640 components this way).
        text = patch(text, key, "            foreach (Component2 component in components)\n            {\n                component.Select4(true, data, false);\n            }", "            SW2URDF.Headless.Selection.Append(model, components, data);", 1)
    if key == "URDF/Link.cs":
        for name in ("Inertial", "Visual", "Collision"):
            text = patch(text, key, f"if ({name} != null)", f"if ({name} != null && !isFixedFrame)", 1)
    if key == "URDFExport/ExportHelper.cs":
        text = patch(text, key, "if (!child.isFixedFrame)\n                {\n                    ExportFiles(child, package, count, exportSTL, meshFormat);\n                }", "ExportFiles(child, package, count, exportSTL, meshFormat);", 1)
        text = patch(text, key, "// Copy the texture file (if it was specified) to the textures directory", "if (link.isFixedFrame) return;\n\n            // Copy the texture file (if it was specified) to the textures directory", 1)
        # Progress for job queries: one step per mesh.
        # A Link whose geometry an earlier passed export already meshed reuses that STL, moved into this
        # Link's frame, instead of showing its components and tessellating them again.
        text = patch(text, key, 'logger.Info(link.Name + ": Exporting STL with coordinate frame " + coordsysName);', 'logger.Info(link.Name + ": Exporting STL with coordinate frame " + coordsysName);\n            SW2URDF.Headless.Progress.Step(link.Name);\n            if (SW2URDF.Headless.Meshes.TryReuse(link.Name, windowsMeshFilename)) return true;', 1)
        # The export hides every top-level component, then shows each Link's components to save its STL.
        # Showing a sub-assembly that sits inside a hidden sub-assembly makes it visible but not its parts,
        # so a Link that lists it whole got an STL without it (all 144 vials of a workstation, 0 triangles
        # in a two-level fixture). Showing and hiding every part below the listed components avoids that.
        text = patch(text, key, "CommonSwOperations.ShowComponents(ActiveSWModel, link.SWComponents);", "CommonSwOperations.ShowComponents(ActiveSWModel, SW2URDF.Headless.Visibility.WithParts(link.SWComponents));", 2)
        text = patch(text, key, "CommonSwOperations.HideComponents(ActiveSWModel, link.SWComponents);", "CommonSwOperations.HideComponents(ActiveSWModel, SW2URDF.Headless.Visibility.WithParts(link.SWComponents));", 2)
        # Restoring visibility component by component took 10 of 38 minutes on a 700-part model.
        # The tool exports from a private copy that is closed without saving, so it skips it.
        text = patch(text, key, "CommonSwOperations.ShowAllComponents(ActiveSWModel, hiddenComponents);", "if (!SW2URDF.Headless.Options.DiscardDocumentAfterExport) CommonSwOperations.ShowAllComponents(ActiveSWModel, hiddenComponents);", 1)
    if key == "URDF/Robot.cs":
        # Upstream's header says the file was made by the original add-in; credit it accurately instead.
        text = patch(text, key,
                     r'''" This URDF was automatically created by SolidWorks to URDF Exporter! " +
                "Originally created by Stephen Brawner (brawner@gmail.com) \r\n" +
                string.Format("     Commit Version: {0}  Build Version: {1}\r\n", commitVersion, buildVersion) +
                "     For more information, please see http://wiki.ros.org/sw_urdf_exporter "''',
                     r'''" Generated by solidworks-urdf-tool (https://github.com/yunylll/solidworks-urdf-tool)\r\n" +
                "     Export core adapted from ros/solidworks_urdf_exporter, Copyright 2020 Stephen Brawner (MIT)\r\n" +
                string.Format("     Core version: {0}  Build: {1}\r\n", commitVersion, buildVersion) +
                "     http://wiki.ros.org/sw_urdf_exporter "''', 1)
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
    public static class Options {
        // Set when the document is a private copy closed without saving after the export.
        public static bool DiscardDocumentAfterExport;
    }
    public static class Meshes {
        public sealed class Plan {
            public string Source, Sha256, Job;
            public double[] FromFrame, ToFrame; // Link to assembly, 4x4 row-major, of the earlier and this export
        }
        public static readonly System.Collections.Generic.Dictionary<string, Plan> Reuse = new System.Collections.Generic.Dictionary<string, Plan>();
        public static readonly System.Collections.Generic.Dictionary<string, object> Reused = new System.Collections.Generic.Dictionary<string, object>();

        // Writes the planned earlier STL to target, its vertices and normals moved from the earlier Link
        // frame to this one. Any doubt (changed file, unreadable mesh) leaves the export to SolidWorks.
        public static bool TryReuse(string link, string target) {
            Plan plan;
            if (!Reuse.TryGetValue(link, out plan)) return false;
            try {
                byte[] data = System.IO.File.ReadAllBytes(plan.Source);
                using (var sha = System.Security.Cryptography.SHA256.Create())
                    if (BitConverter.ToString(sha.ComputeHash(data)).Replace("-", "").ToLowerInvariant() != plan.Sha256) throw new System.IO.IOException("the file changed since it was exported");
                if (data.Length < 84) throw new System.IO.IOException("not a binary STL");
                int count = BitConverter.ToInt32(data, 80);
                if (data.Length != 84 + 50L * count) throw new System.IO.IOException("not a binary STL of " + count + " triangles");
                // Old Link frame to new: inverse(new) * old, both rigid.
                var a = plan.FromFrame; var b = plan.ToFrame;
                var m = new double[12];
                for (int r = 0; r < 3; r++) {
                    for (int c = 0; c < 3; c++) m[4 * r + c] = b[r] * a[c] + b[4 + r] * a[4 + c] + b[8 + r] * a[8 + c];
                    m[4 * r + 3] = b[r] * (a[3] - b[3]) + b[4 + r] * (a[7] - b[7]) + b[8 + r] * (a[11] - b[11]);
                }
                bool identity = true;
                for (int r = 0; r < 3; r++) for (int c = 0; c < 4; c++) if (Math.Abs(m[4 * r + c] - (r == c ? 1 : 0)) > 1e-12) identity = false;
                if (!identity) {
                    for (long t = 0; t < count; t++) {
                        int offset = (int)(84 + 50 * t);
                        for (int v = 0; v < 4; v++) {
                            int at = offset + 12 * v;
                            double x = BitConverter.ToSingle(data, at), y = BitConverter.ToSingle(data, at + 4), z = BitConverter.ToSingle(data, at + 8);
                            double w = v == 0 ? 0 : 1; // the normal turns, the vertices also move
                            for (int r = 0; r < 3; r++) BitConverter.GetBytes((float)(m[4 * r] * x + m[4 * r + 1] * y + m[4 * r + 2] * z + w * m[4 * r + 3])).CopyTo(data, at + 4 * r);
                        }
                    }
                }
                System.IO.File.WriteAllBytes(target, data);
                Reused[link] = new System.Collections.Generic.Dictionary<string, object> { { "source", plan.Source }, { "job_id", plan.Job }, { "moved", !identity } };
                Console.Error.WriteLine("[" + DateTime.Now.ToString("HH:mm:ss") + "]   Reused the mesh of " + link + " from job " + plan.Job + (identity ? "" : " (moved into the new Link frame)"));
                return true;
            } catch (Exception e) {
                Console.Error.WriteLine("Mesh of " + link + " not reused (" + e.Message + "); exporting it.");
                return false;
            }
        }
    }
    public static class Selection {
        // Adds the components to the selection in one call; falls back to one call per component if
        // SolidWorks selected fewer than asked.
        public static void Append(SolidWorks.Interop.sldworks.ModelDoc2 model, System.Collections.Generic.List<SolidWorks.Interop.sldworks.Component2> components, SolidWorks.Interop.sldworks.SelectData data) {
            if (components == null || components.Count == 0) return;
            var manager = (SolidWorks.Interop.sldworks.SelectionMgr)model.SelectionManager;
            int before = manager.GetSelectedObjectCount2(-1);
            var distinct = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var items = new System.Collections.Generic.List<System.Runtime.InteropServices.DispatchWrapper>();
            foreach (var component in components) if (distinct.Add(component.Name2)) items.Add(new System.Runtime.InteropServices.DispatchWrapper(component));
            // The exporter selects with mark -1, which means "any mark" when reading a selection; show,
            // hide, fix and unfix ignore the mark, so 0 serves. Passing an object[] with append=true to an
            // empty selection, MultiSelect2 selected nothing; a DispatchWrapper[] without append selects all.
            var multiple = manager.CreateSelectData();
            if (data.Mark >= 0) multiple.Mark = data.Mark;
            model.Extension.MultiSelect2(items.ToArray(), before != 0, multiple);
            int count = manager.GetSelectedObjectCount2(-1);
            if (count >= before + items.Count) return;
            // Select only the components that are missing, one at a time.
            var selected = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 1; i <= count; i++) {
                var item = manager.GetSelectedObject6(i, -1) as SolidWorks.Interop.sldworks.Component2;
                if (item != null) selected.Add(item.Name2);
            }
            int missing = 0;
            foreach (var component in components) if (!selected.Contains(component.Name2)) { component.Select4(true, data, false); missing++; }
            Console.Error.WriteLine("MultiSelect2 left out " + missing + " of " + items.Count + " components; selected them one at a time.");
        }
    }
    public static class Visibility {
        // The parts that make up the components: each listed part, and below each listed sub-assembly
        // every part that is not suppressed or an envelope (mass properties include the same parts,
        // hidden ones too). Showing a part shows its sub-assemblies with it; selecting a sub-assembly
        // together with its own parts makes SolidWorks select fewer items than asked.
        public static System.Collections.Generic.List<SolidWorks.Interop.sldworks.Component2> WithParts(System.Collections.Generic.List<SolidWorks.Interop.sldworks.Component2> components) {
            var all = new System.Collections.Generic.List<SolidWorks.Interop.sldworks.Component2>();
            var seen = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
            System.Action<SolidWorks.Interop.sldworks.Component2> add = null;
            add = component => {
                if (!seen.Add(component.Name2)) return;
                var children = component.GetChildren() as object[];
                if (children == null || children.Length == 0) { all.Add(component); return; }
                foreach (object child in children) {
                    var item = (SolidWorks.Interop.sldworks.Component2)child;
                    if (!item.IsSuppressed() && !item.IsEnvelope()) add(item);
                }
            };
            foreach (var component in components) add(component);
            return all;
        }
    }
    // Stage and step counters for job queries, written to a JSON file the tool reads.
    public static class Progress {
        public static string FilePath;
        private static readonly object Gate = new object();
        private static string stage = "", detail = "";
        private static int current, total;
        private static DateTime stageStarted = DateTime.UtcNow, lastWrite = DateTime.MinValue;
        public static void Stage(string name, string message, int steps = 0) {
            lock (Gate) {
                stage = name; detail = ""; current = 0; total = steps; stageStarted = DateTime.UtcNow;
                Console.Error.WriteLine("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + message + (steps > 0 ? " (" + steps + ")" : ""));
                Write();
            }
        }
        public static void Step(string item) {
            lock (Gate) {
                current++; detail = item ?? "";
                // Steps of large models come in hundreds; the file need not follow each one.
                if (current == total || (DateTime.UtcNow - lastWrite).TotalSeconds >= 1) {
                    Console.Error.WriteLine("[" + DateTime.Now.ToString("HH:mm:ss") + "]   " + current + (total > 0 ? "/" + total : "") + " " + detail);
                    Write();
                }
            }
        }
        private static void Write() {
            lastWrite = DateTime.UtcNow;
            if (string.IsNullOrEmpty(FilePath)) return;
            try {
                string json = new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(new { stage = stage, detail = detail, current = current, total = total, stage_started_at = stageStarted.ToString("o"), updated_at = lastWrite.ToString("o") });
                string temporary = FilePath + ".tmp";
                System.IO.File.WriteAllText(temporary, json);
                if (System.IO.File.Exists(FilePath)) System.IO.File.Replace(temporary, FilePath, null);
                else System.IO.File.Move(temporary, FilePath);
            } catch (System.IO.IOException) { } catch (UnauthorizedAccessException) { } // A reader may hold the file briefly.
        }
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
