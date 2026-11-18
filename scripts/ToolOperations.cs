using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using MathNet.Numerics.LinearAlgebra;
using MathNet.Numerics.LinearAlgebra.Double;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SW2URDF.URDF;
using SW2URDF.URDFExport;
using SW2URDF.Utilities;

public sealed class ConfigDocument
{
    public int schema_version = 1;
    public string robot_name;
    public bool recompute_kinematics = true;
    public List<LinkSettings> links;
    // Part file names whose hidden solid bodies are left out of mass, inertia and CAD volume.
    public string[] exclude_hidden_bodies;
}
public sealed class LinkSettings
{
    public string name;
    public string parent;
    public string[] components;
    public string coordinate_system;
    public string mesh_quality = "coarse";
    public bool frame_only;
    public JointSettings joint;
}
public sealed class JointSettings
{
    public string name;
    public string type;
    public string axis_name;
    public double[] axis;
    public double[] xyz;
    public double[] rpy;
    public double? lower, upper, effort, velocity, damping, friction;
}

public static partial class LegacyExportBridge
{
    public sealed class SourceManifest
    {
        public string source_path;
        public bool unsaved_changes;
        public string configuration_name;
        public int component_count;
        public double mass_kg;
        public List<SourceComponent> components;
    }
    public sealed class SourceComponent
    {
        public string name, path;
        public bool is_virtual, suppressed, exists;
    }
    private static SourceManifest currentSourceManifest;
    private static HashSet<string> ignoredSavedReferences = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private static bool IsInterconnectCache(string path)
    {
        return System.Text.RegularExpressions.Regex.IsMatch(path ?? "", @"\\Temp\\swx[^\\]+\\IC~~\\", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    }
    // Virtual components live inside the assembly file; SolidWorks lists them at a
    // temporary VC~~ path ("<assembly>^<part>"). They belong to the opened snapshot
    // itself, so they are not external dependencies.
    private static bool IsVirtualComponent(string path)
    {
        return (path ?? "").Contains("^") && System.Text.RegularExpressions.Regex.IsMatch(path, @"\\Temp\\swx[^\\]+\\VC~~\\", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    }
    private static string StageSourceSnapshot(SldWorks app, string source, string output, Dictionary<string, object> report)
    {
        app.SetCurrentWorkingDirectory(Path.GetDirectoryName(source));
        object raw = app.GetDocumentDependencies2(source, true, true, false);
        string[] dependencies = raw == null ? new string[0] : ((IEnumerable)raw).Cast<object>().Select(Convert.ToString).ToArray();
        var files = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { Path.GetFullPath(source) };
        var staleCaches = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var unusedReferences = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        for (int i = 1; i < dependencies.Length; i += 2)
        {
            string file = dependencies[i];
            if (string.IsNullOrEmpty(file)) continue;
            if (!File.Exists(file))
            {
                // 3D Interconnect's saved importer cache is not an assembly part.
                // The native part must still load its saved bodies below.
                if (IsInterconnectCache(file)) { staleCaches.Add(file); continue; }
                // Saved references can outlive the files of inactive configurations or
                // suppressed components. The opened snapshot is checked for missing
                // active components before anything is exported.
                unusedReferences.Add(file); ignoredSavedReferences.Add(file); continue;
            }
            files.Add(Path.GetFullPath(file));
        }
        if (currentSourceManifest != null)
        {
            foreach (var component in currentSourceManifest.components)
            {
                // Virtual components are stored inside the assembly file itself; 3D Interconnect
                // components load from their native part, as when staging the original model.
                if (component.is_virtual || IsVirtualComponent(component.path) || IsInterconnectCache(component.path)) continue;
                if (!component.exists && !component.suppressed) throw new FileNotFoundException("Active component " + component.name + " has no file at " + component.path + ".", component.path);
                if (component.exists) files.Add(Path.GetFullPath(component.path));
            }
        }
        var before = files.ToDictionary(p => p, HashFile, StringComparer.OrdinalIgnoreCase);
        report["sourceHashesBefore"] = before;
        string snapshotRoot = Path.Combine(output, "snapshot");
        var mapping = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        SW2URDF.Headless.Progress.Stage("snapshot", "Copying " + files.Count + " source files and rewriting their references...", files.Count * 2);
        foreach (string file in files)
        {
            SW2URDF.Headless.Progress.Step(Path.GetFileName(file));
            // Preserve each source directory separately, including basename collisions.
            string directoryKey;
            using (var sha = SHA256.Create()) directoryKey = BitConverter.ToString(sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(Path.GetDirectoryName(file).ToLowerInvariant()))).Replace("-", "").Substring(0, 16);
            string folder = Path.Combine(snapshotRoot, directoryKey);
            Directory.CreateDirectory(folder);
            string target = Path.Combine(folder, Path.GetFileName(file));
            File.Copy(file, target, false);
            File.SetAttributes(target, File.GetAttributes(target) & ~FileAttributes.ReadOnly);
            mapping[file] = target;
        }
        // Only copies are passed as the document to modify. Originals are never opened.
        foreach (var entry in mapping)
        {
            SW2URDF.Headless.Progress.Step(Path.GetFileName(entry.Key));
            app.SetCurrentWorkingDirectory(Path.GetDirectoryName(entry.Key));
            object storedRaw = app.GetDocumentDependencies2(entry.Key, false, false, false);
            object resolvedRaw = app.GetDocumentDependencies2(entry.Key, false, true, false);
            string[] stored = storedRaw == null ? new string[0] : ((IEnumerable)storedRaw).Cast<object>().Select(Convert.ToString).ToArray();
            string[] resolved = resolvedRaw == null ? new string[0] : ((IEnumerable)resolvedRaw).Cast<object>().Select(Convert.ToString).ToArray();
            for (int i = 1; i < stored.Length; i += 2)
            {
                string copiedReference;
                string originalReference = stored[i];
                if (!mapping.TryGetValue(originalReference, out copiedReference) && (i >= resolved.Length || !mapping.TryGetValue(resolved[i], out copiedReference)))
                {
                    // Absent saved paths (old locations, importer sources) are not snapshot
                    // files; Pack and Go below still rejects any real external dependency.
                    if (!File.Exists(originalReference)) continue;
                    throw new IOException("Cannot map snapshot reference: " + entry.Key + " refers to " + originalReference + (i < resolved.Length ? " (resolved as " + resolved[i] + ")" : "") + ", which is not among the " + mapping.Count + " snapshot files.");
                }
                if (!app.ReplaceReferencedDocument(entry.Value, originalReference, copiedReference))
                    throw new IOException("Cannot rewrite snapshot reference in " + entry.Value + ": " + originalReference + " -> " + copiedReference);
            }
        }
        report["sourceSnapshotMapping"] = mapping;
        report["staleImportCacheReferences"] = staleCaches.ToArray();
        report["inactiveOrCachedSavedReferences"] = unusedReferences.ToArray();
        report["snapshotDirectory"] = snapshotRoot;
        return mapping[Path.GetFullPath(source)];
    }
    private static string HashFile(string path)
    {
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static string PrepareAssemblyCopy(ModelDoc2 doc, string source, string output, Dictionary<string, object> report)
    {
        string destination = Path.Combine(output, "cad");
        Directory.CreateDirectory(destination);
        var extension = doc.Extension;
        var pack = extension.GetPackAndGo();
        try
        {
            pack.IncludeDrawings = false;
            pack.IncludeSimulationResults = false;
            pack.IncludeToolboxComponents = true;
            pack.IncludeSuppressed = true;
            pack.FlattenToSingleFolder = true;
            object originalsRaw;
            if (!pack.GetDocumentNames(out originalsRaw)) throw new IOException("Pack and Go did not enumerate dependencies.");
            string[] originals = ((IEnumerable)originalsRaw).Cast<object>().Select(Convert.ToString).ToArray();
            if (originals.Length == 0) throw new IOException("Pack and Go found no documents for " + source + ".");
            var before = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string file in originals)
            {
                if (IsVirtualComponent(file)) continue;
                if (File.Exists(file)) before[file] = HashFile(file);
                else
                {
                    if (IsInterconnectCache(file) || ignoredSavedReferences.Contains(file)) continue;
                    throw new FileNotFoundException("An assembly dependency is missing: " + file, file);
                }
            }
            // A caller may already have audited original files before staging.
            bool staged = report.ContainsKey("sourceSnapshotMapping");
            if (!staged) report["sourceHashesBefore"] = before;
            if (staged)
            {
                string snapshotRoot = Path.GetFullPath(Convert.ToString(report["snapshotDirectory"])) + Path.DirectorySeparatorChar;
                foreach (string file in originals)
                    if (!IsInterconnectCache(file) && !IsVirtualComponent(file) && !ignoredSavedReferences.Contains(file) && !Path.GetFullPath(file).StartsWith(snapshotRoot, StringComparison.OrdinalIgnoreCase))
                        throw new IOException("Snapshot has an external dependency; refusing to run Pack and Go: " + file);
            }
            // Virtual components are stored inside their assembly. SolidWorks accepts only its own default
            // destination for them (an empty or renamed destination rejects the whole list, verified on a
            // model with 12 of them), so they keep it and travel inside the copied assembly.
            object defaultsRaw, defaultsStatus;
            if (!pack.GetDocumentSaveToNames(out defaultsRaw, out defaultsStatus)) throw new IOException("Cannot read Pack and Go default destinations for " + originals.Length + " documents.");
            string[] defaults = ((IEnumerable)defaultsRaw).Cast<object>().Select(Convert.ToString).ToArray();
            if (defaults.Length != originals.Length) throw new IOException("Pack and Go lists " + originals.Length + " documents but " + defaults.Length + " default destinations.");
            // Cached importer files and saved-but-absent references have nothing to copy: try an empty
            // destination first and the default one if SolidWorks refuses.
            string[] PlanTargets(bool missingEmpty)
            {
                var used = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                var plan = new string[originals.Length];
                for (int i = 0; i < originals.Length; i++)
                {
                    if (IsVirtualComponent(originals[i])) { plan[i] = defaults[i]; continue; }
                    if (!File.Exists(originals[i]) && (IsInterconnectCache(originals[i]) || ignoredSavedReferences.Contains(originals[i]))) { plan[i] = missingEmpty ? "" : defaults[i]; continue; }
                    string name = Path.GetFileName(originals[i]);
                    // Distinct directories may contain identically named vendor parts.
                    if (!used.Add(name))
                    {
                        name = Path.GetFileNameWithoutExtension(name) + "_dep_" + i + Path.GetExtension(name);
                        if (!used.Add(name)) throw new IOException("Cannot assign unique dependency filenames.");
                    }
                    plan[i] = Path.Combine(destination, name);
                }
                return plan;
            }
            report["packOriginals"] = originals;
            var attempts = new Dictionary<string, bool>();
            report["packSetAttempts"] = attempts;
            string[] targets = null;
            foreach (bool missingEmpty in new[] { true, false })
            {
                string[] plan = PlanTargets(missingEmpty);
                bool accepted = pack.SetDocumentSaveToNames(plan);
                attempts[missingEmpty ? "missing-empty" : "missing-default"] = accepted;
                if (accepted) { targets = plan; break; }
            }
            if (targets == null)
            {
                Func<int, string> kind = i => IsVirtualComponent(originals[i]) ? "virtual" : !File.Exists(originals[i]) ? "absent" : "file";
                var counts = Enumerable.Range(0, originals.Length).GroupBy(kind).Select(g => g.Count() + " " + g.Key);
                var plan = PlanTargets(true);
                var sample = Enumerable.Range(0, originals.Length).Where(i => kind(i) != "file").Concat(Enumerable.Range(0, originals.Length).Where(i => kind(i) == "file")).Take(8)
                    .Select(i => "[" + kind(i) + "] " + originals[i] + " -> '" + plan[i] + "' (default '" + defaults[i] + "')");
                throw new IOException("Pack and Go rejected both destination plans (" + string.Join(", ", attempts.Keys) + ") for " + originals.Length + " documents (" + string.Join(", ", counts) + "). First entries: " + string.Join("; ", sample) + ". Full lists: bridge.packOriginals and the job's bridge-result.json.");
            }
            object effectiveRaw, effectiveStatus;
            if (!pack.GetDocumentSaveToNames(out effectiveRaw, out effectiveStatus)) throw new IOException("Cannot verify Pack and Go destinations.");
            var effective = ((IEnumerable)effectiveRaw).Cast<object>().Select(Convert.ToString).ToArray();
            report["packDestinations"] = effective;
            if (effective.Length != targets.Length || effective.Where((value, index) => !string.Equals(value, targets[index], StringComparison.OrdinalIgnoreCase)).Any())
            {
                var differences = Enumerable.Range(0, Math.Min(effective.Length, targets.Length)).Where(i => !string.Equals(effective[i], targets[i], StringComparison.OrdinalIgnoreCase)).Take(8)
                    .Select(i => originals[i] + ": expected '" + targets[i] + "', SolidWorks kept '" + effective[i] + "'");
                throw new IOException("Pack and Go destination verification failed (" + targets.Length + " planned, " + effective.Length + " reported): " + string.Join("; ", differences));
            }
            SW2URDF.Headless.Progress.Stage("pack_and_go", "Copying " + originals.Length + " CAD dependencies through Pack and Go...");
            object results = extension.SavePackAndGo(pack);
            if (results == null) throw new IOException("Pack and Go returned no save status.");
            var statuses = ((IEnumerable)results).Cast<object>().Select(Convert.ToInt32).ToArray();
            report["packSaveStatuses"] = statuses;
            var failedSaves = Enumerable.Range(0, statuses.Length).Where(i => statuses[i] != 0 && !(statuses[i] == 3 && statuses.Length == targets.Length && targets[i] == "")).ToList();
            if (failedSaves.Count != 0)
                throw new IOException("Pack and Go could not save " + failedSaves.Count + " of " + statuses.Length + " documents: " + string.Join("; ", failedSaves.Take(8).Select(i => (i < originals.Length ? originals[i] + " -> '" + targets[i] + "'" : "#" + i) + ": " + (Enum.GetName(typeof(swPackAndGoSaveStatus_e), statuses[i]) ?? "status") + " (" + statuses[i] + ")")));
            var after = before.Keys.ToDictionary(p => p, HashFile, StringComparer.OrdinalIgnoreCase);
            if (!staged)
            {
                report["sourceHashesAfter"] = after;
                bool unchanged = before.All(p => after[p.Key] == p.Value);
                report["sourceUnchanged"] = unchanged;
                if (!unchanged) throw new IOException("Source CAD changed during preparation; refusing export. Changed files: " + string.Join(", ", before.Where(p => after[p.Key] != p.Value).Select(p => p.Key)));
            }
            var mapping = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < originals.Length; i++)
            {
                mapping[originals[i]] = targets[i];
                if (targets[i] != "" && File.Exists(originals[i]) && !IsVirtualComponent(originals[i]) && !File.Exists(targets[i])) throw new IOException("Pack and Go omitted " + originals[i]);
            }
            report["dependencyMapping"] = mapping;
            report["dependencyCount"] = originals.Length;
            string originalModel = Path.GetFullPath(source);
            string packed = mapping.FirstOrDefault(p => string.Equals(Path.GetFullPath(p.Key), originalModel, StringComparison.OrdinalIgnoreCase)).Value;
            if (string.IsNullOrEmpty(packed) || !File.Exists(packed)) throw new IOException("Packed top-level assembly was not found: expected the copy of " + originalModel + " at '" + packed + "'.");
            // Fingerprint of the copy as written, so a later job can reuse it after checking nothing changed.
            report["preparedHashes"] = Directory.GetFiles(destination).Where(f => f.EndsWith(".sldprt", StringComparison.OrdinalIgnoreCase) || f.EndsWith(".sldasm", StringComparison.OrdinalIgnoreCase)).ToDictionary(f => f, HashFile, StringComparer.OrdinalIgnoreCase);
            return packed;
        }
        finally
        {
            Marshal.ReleaseComObject(pack);
            Marshal.ReleaseComObject(extension);
        }
    }

    private static void CloseAllPrivateDocuments(SldWorks app)
    {
        object[] documents = app.GetDocuments() as object[];
        if (documents == null) return;
        foreach (object document in documents)
        {
            var model = (ModelDoc2)document;
            app.CloseDoc(model.GetTitle());
            Marshal.ReleaseComObject(document);
        }
    }

    private static List<Dictionary<string, object>> DescribeComponents(ModelDoc2 model)
    {
        var list = new List<Dictionary<string, object>>();
        object[] components = ((AssemblyDoc)model).GetComponents(false) as object[];
        if (components == null) return list;
        foreach (object item in components)
        {
            var component = (Component2)item;
            list.Add(new Dictionary<string, object> { { "name", component.Name2 }, { "path", component.GetPathName() }, { "suppressed", component.IsSuppressed() }, { "fixed", component.IsFixed() } });
            Marshal.ReleaseComObject(component);
        }
        return list;
    }

    // Rotation (9 values) and translation in meters of each Link's first component, in the assembly frame.
    private static Dictionary<string, object> DescribeComponentPoses(LinkNode root)
    {
        var poses = new Dictionary<string, object>();
        Action<LinkNode> visit = null;
        visit = node => {
            var link = node.Link;
            if (link.SWComponents.Count != 0)
            {
                var component = link.SWComponents[0];
                var transform = component.Transform2;
                var data = transform == null ? null : transform.ArrayData as double[];
                if (data != null && data.Length >= 12)
                    poses[link.Name] = new Dictionary<string, object> { { "component", component.Name2 }, { "rotation", data.Take(9).ToArray() }, { "translation", data.Skip(9).Take(3).ToArray() } };
                if (transform != null) Marshal.ReleaseComObject(transform);
            }
            foreach (LinkNode child in node.Nodes) visit(child);
        };
        visit(root);
        return poses;
    }

    private static double[] Numbers(object value)
    {
        return ((IEnumerable)value).Cast<object>().Select(v => Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture)).ToArray();
    }

    // Compares the placements with those recorded for the CAD version the configuration was made for;
    // the rows go to the report for the caller to explain, and any row over a tolerance stops the run.
    private static void CheckComponentPoses(Dictionary<string, object> report, string expectedPath, double toleranceM, double toleranceDeg)
    {
        var expected = new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<Dictionary<string, Dictionary<string, object>>>(File.ReadAllText(expectedPath));
        var current = (Dictionary<string, object>)report["linkComponentPoses"];
        var rows = new List<Dictionary<string, object>>();
        var missing = new List<string>();
        bool exceeded = false;
        foreach (var item in expected)
        {
            object found;
            var now = current.TryGetValue(item.Key, out found) ? (Dictionary<string, object>)found : null;
            if (now == null || !string.Equals(Convert.ToString(now["component"]), Convert.ToString(item.Value["component"]), StringComparison.OrdinalIgnoreCase)) { missing.Add(item.Key); continue; }
            double[] oldT = Numbers(item.Value["translation"]), newT = (double[])now["translation"], oldR = Numbers(item.Value["rotation"]), newR = (double[])now["rotation"];
            double distance = Math.Sqrt(Enumerable.Range(0, 3).Sum(i => (newT[i] - oldT[i]) * (newT[i] - oldT[i])));
            // trace(A^T B) is the sum of the element-wise products of two rotation matrices.
            double trace = Enumerable.Range(0, 9).Sum(i => oldR[i] * newR[i]);
            double angle = Math.Acos(Math.Max(-1, Math.Min(1, (trace - 1) / 2))) * 180 / Math.PI;
            bool over = distance > toleranceM || angle > toleranceDeg;
            exceeded |= over;
            rows.Add(new Dictionary<string, object> { { "link", item.Key }, { "component", item.Value["component"] }, { "old_translation", oldT }, { "new_translation", newT }, { "distance_m", distance }, { "angle_deg", angle }, { "exceeds_tolerance", over } });
        }
        report["componentPoseCheck"] = new Dictionary<string, object> { { "rows", rows }, { "not_compared", missing }, { "exceeded", exceeded }, { "tolerance_m", toleranceM }, { "tolerance_deg", toleranceDeg } };
        if (exceeded) throw new InvalidOperationException("Link components moved since the CAD version the configuration was made for (see componentPoseCheck).");
    }

    private static double? OptionalValue(Func<double> value)
    {
        try { return value(); } catch (NullReferenceException) { return null; }
    }

    private static ConfigDocument DescribeConfig(LinkNode root, string robotName)
    {
        var config = new ConfigDocument { robot_name = robotName, links = new List<LinkSettings>(), recompute_kinematics = true };
        Action<LinkNode, string> add = null;
        add = (node, parent) => {
            var link = node.Link;
            JointSettings joint = null;
            if (parent != null) joint = new JointSettings {
                name = link.Joint.Name, type = link.Joint.Type, axis_name = link.Joint.AxisName,
                axis = link.Joint.Axis.GetXYZ(), xyz = link.Joint.Origin.GetXYZ(), rpy = link.Joint.Origin.GetRPY(),
                lower = OptionalValue(() => link.Joint.Limit.Lower), upper = OptionalValue(() => link.Joint.Limit.Upper),
                effort = OptionalValue(() => link.Joint.Limit.Effort), velocity = OptionalValue(() => link.Joint.Limit.Velocity),
                damping = OptionalValue(() => link.Joint.Dynamics.Damping), friction = OptionalValue(() => link.Joint.Dynamics.Friction)
            };
            config.links.Add(new LinkSettings { name = link.Name, parent = parent, components = link.SWComponents.Select(c => c.Name2).ToArray(), coordinate_system = link.Joint.CoordinateSystemName, frame_only = link.isFixedFrame, mesh_quality = link.STLQualityFine ? "fine" : "coarse", joint = joint });
            foreach (LinkNode child in node.Nodes) add(child, link.Name);
        };
        add(root, null);
        return config;
    }

    private static LinkNode BuildConfigTree(ModelDoc2 model, ConfigDocument config)
    {
        if (config == null || config.schema_version != 1 || config.links == null || config.links.Count == 0) throw new ArgumentException("Invalid JSON configuration.");
        var all = ((AssemblyDoc)model).GetComponents(false) as object[] ?? new object[0];
        var components = all.Cast<Component2>().ToDictionary(c => c.Name2, StringComparer.OrdinalIgnoreCase);
        excludedHiddenParts.Clear();
        if (config.exclude_hidden_bodies != null)
        {
            var files = new HashSet<string>(components.Values.Select(c => Path.GetFileName(c.GetPathName() ?? "")), StringComparer.OrdinalIgnoreCase);
            var unknown = config.exclude_hidden_bodies.Where(f => !files.Contains(f)).ToList();
            if (unknown.Count != 0) throw new ArgumentException("exclude_hidden_bodies names part files that no component of the assembly uses: " + string.Join(", ", unknown) + ". " + string.Join(" ", unknown.Select(f => SimilarNames(f, files))));
            foreach (string file in config.exclude_hidden_bodies) excludedHiddenParts.Add(file);
        }
        var nodes = new Dictionary<string, LinkNode>();
        foreach (var settings in config.links)
        {
            if (string.IsNullOrWhiteSpace(settings.name) || nodes.ContainsKey(settings.name)) throw new ArgumentException(string.IsNullOrWhiteSpace(settings.name) ? "A Link has an empty name." : "Duplicate Link name: " + settings.name);
            var link = new Link { Name = settings.name, isFixedFrame = settings.frame_only, STLQualityFine = settings.mesh_quality == "fine" };
            link.Joint.CoordinateSystemName = settings.coordinate_system;
            foreach (string name in settings.components ?? new string[0])
            {
                Component2 component;
                if (!components.TryGetValue(name, out component)) throw new ArgumentException("Link " + settings.name + " names component '" + name + "', which is not in the assembly. " + SimilarNames(name, components.Keys));
                link.SWComponents.Add(component);
            }
            if (settings.joint != null)
            {
                var joint = settings.joint;
                link.Joint.Name = joint.name;
                link.Joint.Type = joint.type;
                link.Joint.AxisName = joint.axis_name;
                if (joint.axis != null) link.Joint.Axis.SetXYZ(joint.axis);
                if (joint.xyz != null) link.Joint.Origin.SetXYZ(joint.xyz);
                if (joint.rpy != null) link.Joint.Origin.SetRPY(joint.rpy);
                if (joint.lower.HasValue) link.Joint.Limit.Lower = joint.lower.Value;
                if (joint.upper.HasValue) link.Joint.Limit.Upper = joint.upper.Value;
                if (joint.effort.HasValue) link.Joint.Limit.Effort = joint.effort.Value;
                if (joint.velocity.HasValue) link.Joint.Limit.Velocity = joint.velocity.Value;
                if (joint.damping.HasValue) link.Joint.Dynamics.Damping = joint.damping.Value;
                if (joint.friction.HasValue) link.Joint.Dynamics.Friction = joint.friction.Value;
            }
            nodes[settings.name] = new LinkNode(link);
        }
        LinkNode root = null;
        foreach (var settings in config.links)
        {
            var node = nodes[settings.name];
            node.IsBaseNode = settings.parent == null;
            if (settings.parent == null)
            {
                if (root != null) throw new ArgumentException("Multiple root Links: " + root.Link.Name + " and " + settings.name + " both have parent null.");
                root = node;
            }
            else
            {
                LinkNode parent;
                if (!nodes.TryGetValue(settings.parent, out parent) || parent == node) throw new ArgumentException("Link " + settings.name + " has parent '" + settings.parent + "', " + (parent == node ? "which is itself." : "which is not a Link of the configuration."));
                parent.Nodes.Add(node);
                node.Link.Parent = parent.Link;
                node.Link.Joint.Parent.Name = parent.Link.Name;
                node.Link.Joint.Child.Name = node.Link.Name;
            }
        }
        if (root == null) throw new ArgumentException("No root link.");
        var visited = new HashSet<string>();
        Action<LinkNode> visit = null;
        visit = node => { if (!visited.Add(node.Link.Name)) throw new ArgumentException("Cycle in Link tree at " + node.Link.Name + "."); foreach (LinkNode child in node.Nodes) visit(child); };
        visit(root);
        if (visited.Count != nodes.Count) throw new ArgumentException("Links not reachable from root " + root.Link.Name + ": " + string.Join(", ", nodes.Keys.Where(k => !visited.Contains(k))));
        return root;
    }

    private static void ValidateResolvedTree(LinkNode node)
    {
        if (!node.Link.isFixedFrame && node.Link.SWComponents.Count == 0) throw new InvalidOperationException("Link has no CAD components: " + node.Link.Name);
        foreach (LinkNode child in node.Nodes) ValidateResolvedTree(child);
    }

    // Unassigned solid parts silently drop mass; a part owned through both itself
    // and an assigned parent sub-assembly is counted twice.
    private static void CheckComponentCoverage(ModelDoc2 model, LinkNode root, out List<string> unassigned, out List<string> overlapping)
    {
        var assigned = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        Action<LinkNode> collect = null;
        collect = node => { foreach (Component2 c in node.Link.SWComponents) assigned.Add(c.Name2); foreach (LinkNode child in node.Nodes) collect(child); };
        collect(root);
        unassigned = new List<string>();
        overlapping = new List<string>();
        var all = ((AssemblyDoc)model).GetComponents(false) as object[] ?? new object[0];
        foreach (Component2 component in all)
        {
            if (component.IsSuppressed() || component.IsEnvelope()) continue;
            bool ancestorAssigned = false, ancestorSuppressed = false;
            for (var parent = component.GetParent(); parent != null; parent = parent.GetParent())
            {
                if (parent.IsSuppressed()) ancestorSuppressed = true;
                if (assigned.Contains(parent.Name2)) ancestorAssigned = true;
            }
            if (ancestorSuppressed) continue;
            bool own = assigned.Contains(component.Name2);
            if (own && ancestorAssigned) overlapping.Add(component.Name2);
            if (own || ancestorAssigned) continue;
            if (!(component.GetPathName() ?? "").EndsWith(".sldprt", StringComparison.OrdinalIgnoreCase)) continue;
            object info;
            var bodies = component.GetBodies3((int)SolidWorks.Interop.swconst.swBodyType_e.swSolidBody, out info) as object[];
            if (bodies != null && bodies.Length != 0) unassigned.Add(component.Name2);
        }
    }

    private static void RequireComponentCoverage(List<string> unassigned, List<string> overlapping)
    {
        Func<List<string>, string> sample = names => string.Join(", ", names.Take(20)) + (names.Count > 20 ? ", ..." : "");
        if (overlapping.Count != 0)
            throw new InvalidOperationException(overlapping.Count + " component(s) are assigned both directly and through a parent sub-assembly: " + sample(overlapping));
        if (unassigned.Count != 0)
            throw new InvalidOperationException(unassigned.Count + " solid part component(s) are not assigned to any Link; assign them (e.g. to the base link) so the URDF mass matches the CAD: " + sample(unassigned));
    }
    // Saved references to absent files are tolerated only if no active component uses them.
    private static void RequireLoadedComponents(ModelDoc2 model, Dictionary<string, object> report)
    {
        var missing = new List<string>();
        var suppressedMissing = new List<string>();
        foreach (Component2 component in ((AssemblyDoc)model).GetComponents(false) as object[] ?? new object[0])
        {
            string path = component.GetPathName() ?? "";
            bool absent = !File.Exists(path);
            if (component.IsSuppressed()) { if (absent) suppressedMissing.Add(component.Name2 + " (" + path + ")"); }
            else if (absent || component.GetModelDoc2() == null) missing.Add(component.Name2 + " (" + path + ")");
        }
        report["suppressedMissingComponents"] = suppressedMissing;
        if (missing.Count != 0)
            throw new FileNotFoundException(missing.Count + " active component(s) reference files that are not on this computer: " + string.Join("; ", missing.Take(20)) + ". Restore the files, or suppress the components in the active configuration and save.");
    }

    // Names the set bits of a SolidWorks status code, e.g. "swFileNotFoundError (2)".
    private static string DescribeFlags(Type flags, int value)
    {
        if (value == 0) return "none (0)";
        var names = Enum.GetValues(flags).Cast<object>().Select(Convert.ToInt32).Distinct()
            .Where(bit => bit != 0 && (bit & (bit - 1)) == 0 && (value & bit) == bit).Select(bit => Enum.GetName(flags, bit)).ToList();
        if (names.Count == 0 && Enum.IsDefined(flags, value)) names.Add(Enum.GetName(flags, value));
        return (names.Count == 0 ? "unknown" : string.Join(" | ", names)) + " (" + value + ")";
    }

    private static string SimilarNames(string wanted, IEnumerable<string> available)
    {
        Func<string, string, int> distance = (a, b) =>
        {
            var row = Enumerable.Range(0, b.Length + 1).ToArray();
            for (int i = 1; i <= a.Length; i++)
            {
                int previous = row[0];
                row[0] = i;
                for (int j = 1; j <= b.Length; j++)
                {
                    int current = row[j];
                    row[j] = Math.Min(Math.Min(row[j] + 1, row[j - 1] + 1), previous + (char.ToLowerInvariant(a[i - 1]) == char.ToLowerInvariant(b[j - 1]) ? 0 : 1));
                    previous = current;
                }
            }
            return row[b.Length];
        };
        var similar = available.Select(n => new { name = n, cost = distance(wanted, n) }).Where(n => n.cost <= Math.Max(3, wanted.Length / 3)).OrderBy(n => n.cost).ThenBy(n => n.name).Take(5).Select(n => n.name).ToList();
        return similar.Count != 0 ? "Similar names: " + string.Join(", ", similar) + "." : "Run inspect to list component names (inspection result: components).";
    }

    // A reused prepared model must load nothing from outside its private copy.
    private static void RequireDocumentsInside(SldWorks app, string directory)
    {
        string root = Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var outside = new List<string>();
        foreach (object item in app.GetDocuments() as object[] ?? new object[0])
        {
            string path = ((ModelDoc2)item).GetPathName() ?? "";
            if (path.Length != 0 && !IsVirtualComponent(path) && !IsInterconnectCache(path) && !Path.GetFullPath(path).StartsWith(root, StringComparison.OrdinalIgnoreCase)) outside.Add(path);
            Marshal.ReleaseComObject(item);
        }
        if (outside.Count != 0)
            throw new IOException(outside.Count + " document(s) of the reused prepared model loaded from outside its private copy " + root + ": " + string.Join(", ", outside.Take(10)) + ". Prepare the model again.");
    }

    private static int CountLinks(LinkNode node, bool withGeometry)
    {
        int own = withGeometry && node.Link.isFixedFrame ? 0 : 1;
        foreach (LinkNode child in node.Nodes) own += CountLinks(child, withGeometry);
        return own;
    }

    // The joints as the exporter resolved them, for a check run to report.
    private static List<Dictionary<string, object>> DescribeJoints(LinkNode root)
    {
        var joints = new List<Dictionary<string, object>>();
        Action<LinkNode> visit = null;
        visit = node => {
            foreach (LinkNode child in node.Nodes)
            {
                var joint = child.Link.Joint;
                joints.Add(new Dictionary<string, object> {
                    { "name", joint.Name }, { "type", joint.Type }, { "parent", node.Link.Name }, { "child", child.Link.Name },
                    { "coordinate_system", joint.CoordinateSystemName }, { "xyz", joint.Origin.GetXYZ() }, { "rpy", joint.Origin.GetRPY() },
                    { "axis", joint.Type == "fixed" ? null : joint.Axis.GetXYZ() } });
                visit(child);
            }
        };
        visit(root);
        return joints;
    }

    public const string AssemblyOriginFrame = "Assembly Origin";
    private const string AutomaticFrame = "Automatically Generate";

    private static Matrix<double> FramePose(ExportHelper helper, string name)
    {
        MathTransform transform = null;
        try { transform = helper.GetToolFrameTransform(name); }
        catch (NullReferenceException) { }
        if (transform == null) throw new InvalidOperationException("Coordinate system '" + name + "' was not found in the top-level assembly. Use a coordinate system of the assembly itself (not of a part or sub-assembly), \"Assembly Origin\" for the root Link, or \"Automatically Generate\".");
        return MathOps.GetTransformation(transform);
    }

    private static double PoseDifference(Matrix<double> a, Matrix<double> b)
    {
        double worst = 0;
        for (int row = 0; row < 3; row++)
            for (int column = 0; column < 4; column++)
                worst = Math.Max(worst, Math.Abs(a[row, column] - b[row, column]));
        return worst;
    }

    private static string FormatVector(double[] values)
    {
        return "[" + string.Join(", ", values.Select(v => (Math.Abs(v) < 1e-12 ? 0 : v).ToString("0.#########", System.Globalization.CultureInfo.InvariantCulture))) + "]";
    }

    // The exporter builds Link frames only while it recomputes kinematics. Configured
    // joints would otherwise leave "Automatically Generate" meshes and inertia in
    // assembly coordinates (and the base Link alone in its Y-up Origin_global), so
    // every frame is built from the configured joint chain instead.
    private static void PrepareLinkFrames(ExportHelper helper, LinkNode root, bool recompute, Dictionary<string, object> report)
    {
        var rootJoint = root.Link.Joint;
        if (rootJoint.CoordinateSystemName == AssemblyOriginFrame)
            rootJoint.CoordinateSystemName = helper.CreateToolFrame(DenseMatrix.CreateIdentity(4), "Origin_assembly");
        else if (!recompute && rootJoint.CoordinateSystemName == AutomaticFrame)
            rootJoint.CoordinateSystemName = helper.CreateToolBaseFrame(true);
        report["baseCoordinateSystem"] = rootJoint.CoordinateSystemName;
        if (recompute) return;
        var frames = new Dictionary<string, object>();
        var mismatches = new List<string>();
        var createdMismatches = new List<string>();
        Action<LinkNode, Matrix<double>> visit = null;
        visit = (node, pose) => {
            frames[node.Link.Name] = new Dictionary<string, object> { { "coordinate_system", node.Link.Joint.CoordinateSystemName }, { "assembly_xyz", MathOps.GetXYZ(pose) }, { "assembly_rpy", MathOps.GetRPY(pose) } };
            foreach (LinkNode child in node.Nodes)
            {
                var joint = child.Link.Joint;
                var configured = pose * MathOps.GetTransformation(joint.Origin.GetXYZ(), joint.Origin.GetRPY());
                var childPose = configured;
                if (joint.CoordinateSystemName == AutomaticFrame)
                {
                    joint.CoordinateSystemName = helper.CreateToolFrame(configured, "Origin_" + joint.Name);
                    var created = FramePose(helper, joint.CoordinateSystemName);
                    double difference = PoseDifference(created, configured);
                    if (difference > 1e-6)
                        createdMismatches.Add(joint.Name + ": the created coordinate system '" + joint.CoordinateSystemName + "' differs from its configured origin by " + difference.ToString("0.#########", System.Globalization.CultureInfo.InvariantCulture) + " (assembly frame: configured xyz " + FormatVector(MathOps.GetXYZ(configured)) + " rpy " + FormatVector(MathOps.GetRPY(configured)) + ", created xyz " + FormatVector(MathOps.GetXYZ(created)) + " rpy " + FormatVector(MathOps.GetRPY(created)) + ")");
                }
                else
                {
                    childPose = FramePose(helper, joint.CoordinateSystemName);
                    if (PoseDifference(childPose, configured) > 1e-6)
                    {
                        var local = pose.Inverse() * childPose;
                        mismatches.Add(joint.Name + ": coordinate system '" + joint.CoordinateSystemName + "' is at xyz " + FormatVector(MathOps.GetXYZ(local)) + " rpy " + FormatVector(MathOps.GetRPY(local)) + " in its parent Link frame, but the configuration gives xyz " + FormatVector(joint.Origin.GetXYZ()) + " rpy " + FormatVector(joint.Origin.GetRPY()));
                    }
                }
                visit(child, childPose);
            }
        };
        visit(root, FramePose(helper, rootJoint.CoordinateSystemName));
        report["linkFrames"] = frames;
        if (createdMismatches.Count != 0)
            throw new InvalidOperationException("Generated coordinate systems do not reproduce the configured joint origins: " + string.Join("; ", createdMismatches));
        if (mismatches.Count != 0)
            throw new InvalidOperationException("Configured joint origins disagree with named coordinate systems. Use the coordinate system values, or set coordinate_system to \"Automatically Generate\": " + string.Join("; ", mismatches));
    }

    // Recomputed kinematics come from the remaining degrees of freedom of each child
    // Link's first component, with the parent Link's components held fixed. Anything
    // the exporter cannot see becomes a fixed joint without an error.
    private static void RequireConfiguredJointTypes(LinkNode root, ConfigDocument supplied, Dictionary<string, object> report)
    {
        var configured = supplied.links.Where(l => l.joint != null).ToDictionary(l => l.name, l => l.joint.type);
        var detected = new Dictionary<string, object>();
        var problems = new List<string>();
        Action<LinkNode> visit = null;
        visit = node => {
            foreach (LinkNode child in node.Nodes)
            {
                var link = child.Link;
                string wanted;
                detected[link.Joint.Name] = link.Joint.Type;
                if (!link.isFixedFrame && configured.TryGetValue(link.Name, out wanted) && wanted != link.Joint.Type)
                    problems.Add(link.Joint.Name + ": configured " + wanted + ", detected " + link.Joint.Type + " from '" + (link.SWMainComponent == null ? "?" : link.SWMainComponent.Name2) + "'");
                visit(child);
            }
        };
        visit(root);
        report["detectedJointTypes"] = detected;
        if (problems.Count != 0)
            throw new InvalidOperationException("recompute_kinematics detected different joint types than configured: " + string.Join("; ", problems) + ". Motion is detected only from the first component of each child Link; mates inside flexible sub-assemblies or through other Links are not seen. List a component mated directly to the parent Link first, or set recompute_kinematics to false and give each joint's xyz, rpy and axis.");
    }

    private static void CollectBodies(Component2 component, List<Body2> bodies)
    {
        object info;
        foreach (Body2 body in component.GetBodies3((int)swBodyType_e.swSolidBody, out info) as object[] ?? new object[0]) bodies.Add(body);
        foreach (Component2 child in component.GetChildren() as object[] ?? new object[0]) CollectBodies(child, bodies);
    }

    private static double GeometricMass(ModelDoc2 model, IEnumerable<Component2> components)
    {
        var bodies = new List<Body2>();
        foreach (var component in components) CollectBodies(component, bodies);
        if (bodies.Count == 0) return 0;
        var extension = model.Extension;
        var mass = extension.CreateMassProperty();
        try
        {
            mass.UseSystemUnits = true;
            if (!mass.AddBodies(bodies.ToArray())) throw new InvalidOperationException("Failed to add bodies to mass properties.");
            return mass.Mass;
        }
        finally
        {
            Marshal.ReleaseComObject(mass);
            Marshal.ReleaseComObject(extension);
        }
    }

    // SolidWorks' time for one mass property call grows with the square of its component count: 100 parts
    // of the workstation took 5.5 s, 800 took 106 s (a 1722-part Link 7 minutes). Larger lists go in
    // chunks of this size, combined exactly; each call also costs about a second whatever its size.
    // SW_URDF_MASS_CHUNK sets another size, for the regression test that compares chunked and whole results.
    private static readonly int MassPropertyChunk = int.TryParse(System.Environment.GetEnvironmentVariable("SW_URDF_MASS_CHUNK"), out int chunk) && chunk > 0 ? chunk : 100;

    // Returns mass, center of mass (3), the 3x3 moment of inertia at the center of mass and the volume.
    private static double[] ComponentMassProperties(ModelDoc2 model, IEnumerable<Component2> components, MathTransform frame, bool includeHidden = true)
    {
        var list = components.ToList();
        if (list.Count <= MassPropertyChunk) return ComponentMassPropertiesOnce(model, list, frame, includeHidden);
        var chunks = new List<double[]>();
        for (int i = 0; i < list.Count; i += MassPropertyChunk) chunks.Add(ComponentMassPropertiesOnce(model, list.Skip(i).Take(MassPropertyChunk), frame, includeHidden));
        return CombineMassProperties(chunks);
    }

    // Mass properties of each part document (file and configuration), in the part's own frame: mass,
    // center (3), moments at the center (SolidWorks order and sign) and volume. Overrides in the part
    // are honored, hidden bodies included. Null when the document is not loaded or a component uses a
    // configuration that is not the document's active one.
    private static readonly Dictionary<string, double[]> partMassProperties = new Dictionary<string, double[]>(StringComparer.OrdinalIgnoreCase);

    // Part files the configuration names in exclude_hidden_bodies: their hidden solid bodies (for example
    // the source of a mirror that mates still use) are not weighed and not part of the CAD volume.
    private static readonly HashSet<string> excludedHiddenParts = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    private static bool ExcludesHidden(Component2 part)
    {
        return excludedHiddenParts.Count != 0 && excludedHiddenParts.Contains(Path.GetFileName(part.GetPathName() ?? ""));
    }

    private static double[] PartMassProperties(Component2 part, bool? includeHidden = null)
    {
        bool hidden = includeHidden ?? !ExcludesHidden(part);
        string key = part.GetPathName() + "|" + part.ReferencedConfiguration + "|" + hidden;
        double[] known;
        if (partMassProperties.TryGetValue(key, out known)) return known;
        known = null;
        try
        {
            var document = part.GetModelDoc2() as ModelDoc2;
            var active = document == null ? null : document.ConfigurationManager.ActiveConfiguration;
            if (active != null && string.Equals(active.Name, part.ReferencedConfiguration, StringComparison.Ordinal))
            {
                var extension = document.Extension;
                var mass = (IMassProperty2)extension.CreateMassProperty2();
                mass.UseSystemUnits = true;
                mass.IncludeHiddenBodiesOrComponents = hidden;
                if (mass.Recalculate())
                {
                    var center = (double[])mass.CenterOfMass;
                    var moment = (double[])mass.GetMomentOfInertia((int)swMomentsOfInertiaReferenceFrame_e.swMomentsOfInertiaReferenceFrame_CenterOfMass);
                    known = new[] { mass.Mass, center[0], center[1], center[2] }.Concat(moment).Concat(new[] { mass.Volume }).ToArray();
                }
                Marshal.ReleaseComObject(mass);
                Marshal.ReleaseComObject(extension);
            }
        }
        catch (Exception e) when (e is COMException || e is NullReferenceException || e is InvalidCastException) { known = null; }
        partMassProperties[key] = known;
        return known;
    }

    // Whether a part document's own mass properties equal what SolidWorks weighs for an instance of it in
    // the assembly. They need not: a material assigned to the component in the assembly changes its mass
    // without touching the part (13.6 g of the workstation's pipette head). Unverified documents are
    // weighed by SolidWorks instance by instance.
    private static readonly Dictionary<string, bool> partVerified = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
    private static readonly List<string> partMismatches = new List<string>();

    private static string PartKey(Component2 part) { return part.GetPathName() + "|" + part.ReferencedConfiguration; }

    private static bool Agree(double[] a, double[] b)
    {
        double size = Math.Max(1e-12, Enumerable.Range(4, 9).Max(i => Math.Max(Math.Abs(a[i]), Math.Abs(b[i]))));
        return Close(a[0], b[0], 1e-9, 1e-15) && Enumerable.Range(1, 3).All(i => Math.Abs(a[i] - b[i]) <= 1e-9) && Enumerable.Range(4, 9).All(i => Math.Abs(a[i] - b[i]) <= 1e-7 * size);
    }

    // Compares one instance of each document with SolidWorks, 100 at a time; a group that disagrees is
    // halved until the documents that differ are found.
    private static void VerifyPartDocuments(ModelDoc2 model, IEnumerable<Component2> instances)
    {
        var pending = instances.Where(c => !partVerified.ContainsKey(PartKey(c)) && PartMassProperties(c) != null)
            .GroupBy(PartKey, StringComparer.OrdinalIgnoreCase).Select(g => g.First()).ToList();
        Action<List<Component2>> check = null;
        check = items => {
            var expected = CombineMassProperties(items.Select(c => PlacedPartMassProperties(PartMassProperties(c), MathOps.GetTransformation(c.Transform2))).ToList());
            var actual = ComponentMassPropertiesOnce(model, items, null, !ExcludesHidden(items[0]));
            if (Agree(expected, actual)) { foreach (var c in items) partVerified[PartKey(c)] = true; return; }
            if (items.Count == 1) { partVerified[PartKey(items[0])] = false; partMismatches.Add(items[0].Name2 + " (" + Path.GetFileName(items[0].GetPathName()) + ")"); return; }
            check(items.Take(items.Count / 2).ToList());
            check(items.Skip(items.Count / 2).ToList());
        };
        foreach (var group in pending.GroupBy(ExcludesHidden).Select(g => g.ToList()))
            for (int i = 0; i < group.Count; i += MassPropertyChunk) check(group.Skip(i).Take(MassPropertyChunk).ToList());
    }

    // A part instance's mass properties in the Link frame: the center moves with the instance's pose,
    // the inertia turns with it (R J R^T on the true tensor, whose products are minus SolidWorks' ones).
    private static double[] PlacedPartMassProperties(double[] own, Matrix<double> partToLink)
    {
        var j = new double[9];
        for (int i = 0; i < 9; i++) j[i] = (i % 4 == 0 ? 1 : -1) * own[4 + i];
        var turned = new double[9];
        for (int r = 0; r < 3; r++)
            for (int s = 0; s < 3; s++)
            {
                double sum = 0;
                for (int a = 0; a < 3; a++)
                    for (int b = 0; b < 3; b++) sum += partToLink[r, a] * j[3 * a + b] * partToLink[s, b];
                turned[3 * r + s] = (r == s ? 1 : -1) * sum;
            }
        var center = new double[3];
        for (int r = 0; r < 3; r++) center[r] = partToLink[r, 0] * own[1] + partToLink[r, 1] * own[2] + partToLink[r, 2] * own[3] + partToLink[r, 3];
        return new[] { own[0], center[0], center[1], center[2] }.Concat(turned).Concat(new[] { own[13] }).ToArray();
    }

    // A Link's mass properties from those of its part documents, each read once: the workstation repeats
    // one pipette tip 1434 times, which SolidWorks weighed at 25 s per 100 instances. Sub-assemblies whose
    // own mass properties are overridden (or may be), parts in a non-active configuration, scaled
    // instances and anything unreadable are weighed by SolidWorks itself and combined with the rest.
    // SW_URDF_MASS_METHOD=items weighs every Link by SolidWorks alone, for comparison.
    // Mass of hidden bodies left out per Link (exclude_hidden_bodies), and instances it could not apply to.
    private static readonly Dictionary<string, double> excludedHiddenMass = new Dictionary<string, double>();
    private static readonly List<string> exclusionNotApplied = new List<string>();

    private static double[] LinkMassProperties(ModelDoc2 model, Link link, MathTransform frame, Dictionary<string, bool?> flags)
    {
        var components = link.SWComponents;
        if (System.Environment.GetEnvironmentVariable("SW_URDF_MASS_METHOD") == "items") return ComponentMassProperties(model, components, frame);
        var toLink = frame == null ? DenseMatrix.CreateIdentity(4) : MathOps.GetTransformation(frame).Inverse();
        var parts = new List<Component2>();
        var weighed = new List<Component2>();
        Action<Component2> add = null;
        add = component => {
            if (IsAssemblyComponent(component))
            {
                if (OwnOverride(component, flags) != false) { weighed.Add(component); return; }
                foreach (Component2 child in component.GetChildren() as object[] ?? new object[0])
                    if (!child.IsSuppressed() && !child.IsEnvelope()) add(child);
                return;
            }
            var transform = component.Transform2;
            var data = transform == null ? null : transform.ArrayData as double[];
            if (PartMassProperties(component) == null || data == null || data.Length < 13 || Math.Abs(data[12] - 1) > 1e-12) weighed.Add(component);
            else parts.Add(component);
        };
        foreach (var component in components) add(component);
        VerifyPartDocuments(model, parts);
        var contributions = new List<double[]>();
        double excluded = 0;
        foreach (var part in parts)
        {
            if (partVerified.TryGetValue(PartKey(part), out bool verified) && verified)
            {
                contributions.Add(PlacedPartMassProperties(PartMassProperties(part), toLink * MathOps.GetTransformation(part.Transform2)));
                if (ExcludesHidden(part)) excluded += PartMassProperties(part, true)[0] - PartMassProperties(part)[0];
            }
            else weighed.Add(part);
        }
        // SolidWorks weighs these with their hidden bodies: leaving them out there would also leave out
        // hidden components, so the exclusion is reported as not applied instead.
        exclusionNotApplied.AddRange(weighed.Where(ExcludesHidden).Select(c => c.Name2));
        if (weighed.Count != 0) contributions.Add(ComponentMassProperties(model, weighed, frame));
        excludedHiddenMass[link.Name] = excluded;
        return CombineMassProperties(contributions);
    }

    // Mass properties of a union from those of its parts, all in one frame: masses and volumes add, the
    // center of mass is the mass-weighted mean, and each moment of inertia moves from its own center of
    // mass to the common one by the parallel-axis theorem. SolidWorks gives the products of inertia as
    // +integral(x y dm) (the URDF writer negates them), so they move by +m dx dy, the moments by
    // m (|d|^2 - dx^2) and so on.
    internal static double[] CombineMassProperties(List<double[]> parts)
    {
        double mass = parts.Sum(p => p[0]);
        var center = new double[3];
        if (mass > 0) for (int k = 0; k < 3; k++) center[k] = parts.Sum(p => p[0] * p[1 + k]) / mass;
        var moment = new double[9];
        foreach (var p in parts)
        {
            var d = new[] { p[1] - center[0], p[2] - center[1], p[3] - center[2] };
            double squared = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
            for (int r = 0; r < 3; r++)
                for (int s = 0; s < 3; s++)
                    moment[3 * r + s] += p[4 + 3 * r + s] + p[0] * (r == s ? squared - d[r] * d[r] : d[r] * d[s]);
        }
        return new[] { mass, center[0], center[1], center[2] }.Concat(moment).Concat(new[] { parts.Sum(p => p[13]) }).ToArray();
    }

    private static double[] ComponentMassPropertiesOnce(ModelDoc2 model, IEnumerable<Component2> components, MathTransform frame, bool includeHidden = true)
    {
        var extension = model.Extension;
        var mass = (IMassProperty2)extension.CreateMassProperty2();
        try
        {
            mass.UseSystemUnits = true;
            mass.IncludeHiddenBodiesOrComponents = includeHidden;
            mass.SelectedItems = components.Select(c => new DispatchWrapper(c)).ToArray();
            if (frame != null) mass.SetCoordinateSystem(frame);
            // Without this the properties still describe the previous selection.
            if (!mass.Recalculate()) throw new InvalidOperationException("Component mass properties could not be recalculated.");
            var center = (double[])mass.CenterOfMass;
            var moment = (double[])mass.GetMomentOfInertia((int)swMomentsOfInertiaReferenceFrame_e.swMomentsOfInertiaReferenceFrame_CenterOfMass);
            return new[] { mass.Mass, center[0], center[1], center[2] }.Concat(moment).Concat(new[] { mass.Volume }).ToArray();
        }
        finally
        {
            Marshal.ReleaseComObject(mass);
            Marshal.ReleaseComObject(extension);
        }
    }

    private static bool Close(double a, double b, double relative, double absolute)
    {
        return Math.Abs(a - b) <= absolute + relative * Math.Max(Math.Abs(a), Math.Abs(b));
    }

    // The exporter's Link inertia comes from summing body moments, which on Links
    // of several parts came out 3-5x below the inertia of the exported meshes, and
    // its masses ignore mass properties overridden in the CAD. Component mass
    // properties match the meshes and honor overrides. They replace the exporter's
    // values once mass and center of mass agree on every Link without an override
    // and the Links sum to the assembly mass.
    private static void ApplyComponentMassProperties(ExportHelper helper, ModelDoc2 model, LinkNode root, Dictionary<string, object> report)
    {
        report["massPropertiesSource"] = "exporter";
        try { TransferComponentMassProperties(helper, model, root, report); }
        catch (Exception e) when (e is COMException || e is InvalidOperationException)
        {
            // The exporter's own values stay; the CAD mass check then reports any gap.
            report["massPropertiesError"] = e.Message;
        }
        report["massOverridesApplied"] = Convert.ToString(report["massPropertiesSource"]) == "components" && report.ContainsKey("massOverrides") && ((List<object>)report["massOverrides"]).Count != 0;
    }

    private static void TransferComponentMassProperties(ExportHelper helper, ModelDoc2 model, LinkNode root, Dictionary<string, object> report)
    {
        var links = new List<Link>();
        Action<LinkNode> collect = null;
        collect = node => { if (!node.Link.isFixedFrame && node.Link.SWComponents.Count != 0) links.Add(node.Link); foreach (LinkNode child in node.Nodes) collect(child); };
        collect(root);
        SW2URDF.Headless.Progress.Stage("mass_properties", "Reading component mass properties of each Link...", links.Count);
        // Override flags of each document, read once (see OwnOverride).
        var flags = new Dictionary<string, bool?>(StringComparer.OrdinalIgnoreCase);
        var cad = links.ToDictionary(l => l, l => { SW2URDF.Headless.Progress.Step(l.Name); return LinkMassProperties(model, l, helper.GetToolFrameTransform(l.Joint.CoordinateSystemName), flags); });
        // Kept for the mesh check even if the transfer below stops.
        report["linkCadVolumes"] = cad.ToDictionary(item => item.Key.Name, item => (object)item.Value[13]);
        report["partMassCheck"] = new Dictionary<string, object> { { "documents_verified", partVerified.Count(p => p.Value) }, { "documents_weighed_in_assembly", partMismatches } };
        double excludedTotal = excludedHiddenMass.Values.Sum();
        if (excludedHiddenParts.Count != 0)
        {
            report["excludedHiddenBodyMassKg"] = excludedTotal;
            report["excludedHiddenBodies"] = new Dictionary<string, object> { { "files", excludedHiddenParts.ToArray() }, { "mass_kg_by_link", excludedHiddenMass.Where(e => e.Value > 0).ToDictionary(e => e.Key, e => (object)e.Value) }, { "not_applied_to", exclusionNotApplied.Distinct().ToArray() } };
        }
        var disagreeing = new List<string>();
        var overrides = new List<object>();
        // Instances of one part file and configuration share their masses; large links repeat the
        // same fasteners and profiles hundreds of times (e.g. 610 instances of 124 files in a large assembly).
        var componentMasses = new Dictionary<string, double[]>(StringComparer.OrdinalIgnoreCase);
        Func<Component2, double[]> massesOf = c =>
        {
            string key = c.GetPathName() + "|" + c.ReferencedConfiguration;
            double[] known;
            if (!componentMasses.TryGetValue(key, out known))
                componentMasses[key] = known = new[] { ComponentMassProperties(model, new[] { c }, null)[0], GeometricMass(model, new[] { c }) };
            return known;
        };
        // Weighing each component twice in the whole assembly costs seconds (14 minutes for 610 components);
        // the override flags of each document are read once instead, and only flagged components are weighed.
        foreach (var link in links)
        {
            var values = cad[link];
            // The exporter weighs hidden bodies too; its center of mass then differs by design.
            double left = excludedHiddenMass.TryGetValue(link.Name, out double leftOut) ? leftOut : 0;
            if (Close(values[0], link.Inertial.Mass.Value - left, 1e-5, 1e-12))
            {
                var center = link.Inertial.Origin.GetXYZ();
                if (left == 0 && !Enumerable.Range(0, 3).All(i => Close(values[1 + i], center[i], 0, 1e-6))) disagreeing.Add(link.Name + " center of mass");
                continue;
            }
            SW2URDF.Headless.Progress.Stage("mass_overrides", "Mass of " + link.Name + " differs from its geometry; checking its components for overrides...", link.SWComponents.Count);
            var components = link.SWComponents
                .Where(c => { SW2URDF.Headless.Progress.Step(c.Name2); return MayCarryOverride(c, flags); })
                .Select(c => { var known = massesOf(c); return new { name = c.Name2, cad = known[0], geometric = known[1] }; })
                .Where(c => !Close(c.cad, c.geometric, 1e-5, 1e-12))
                .Select(c => new Dictionary<string, object> { { "component", c.name }, { "cad_mass_kg", c.cad }, { "geometric_mass_kg", c.geometric } }).ToList();
            overrides.Add(new Dictionary<string, object> { { "link", link.Name }, { "cad_mass_kg", values[0] }, { "geometric_mass_kg", link.Inertial.Mass.Value }, { "components", components } });
        }
        report["massOverrides"] = overrides;
        double total = links.Sum(l => cad[l][0]), assembly = Convert.ToDouble(report["cadAssemblyMassKg"]) - excludedTotal;
        if (!Close(total, assembly, 1e-6, 1e-8)) disagreeing.Add("sum of Link masses " + total + " kg != assembly " + assembly + " kg");
        if (disagreeing.Count != 0)
        {
            report["massPropertiesCrossCheckFailed"] = disagreeing;
            return;
        }
        var exporterInertia = new Dictionary<string, object>();
        foreach (var link in links)
        {
            var values = cad[link];
            var old = link.Inertial.Inertia;
            exporterInertia[link.Name] = new[] { link.Inertial.Mass.Value, old.Ixx, old.Ixy, old.Ixz, old.Iyy, old.Iyz, old.Izz };
            link.Inertial.Mass.Value = values[0];
            link.Inertial.Origin.SetXYZ(new[] { values[1], values[2], values[3] });
            link.Inertial.Inertia.SetMomentMatrix(values.Skip(4).Take(9).ToArray());
        }
        report["exporterInertia"] = exporterInertia;
        report["massPropertiesSource"] = "components";
    }

    // Whether the mass properties of a document (for the configuration a component uses) are overridden;
    // null when that configuration is not the document's active one, which the flags would not describe.
    private static bool? OwnOverride(Component2 component, Dictionary<string, bool?> flags)
    {
        string key = component.GetPathName() + "|" + component.ReferencedConfiguration;
        bool? flag;
        if (flags.TryGetValue(key, out flag)) return flag;
        flag = null;
        try
        {
            var document = component.GetModelDoc2() as ModelDoc2;
            var active = document == null ? null : document.ConfigurationManager.ActiveConfiguration;
            if (active != null && string.Equals(active.Name, component.ReferencedConfiguration, StringComparison.Ordinal))
            {
                var extension = document.Extension;
                var mass = extension.CreateMassProperty2() as IMassProperty2;
                var options = mass == null ? null : mass.GetOverrideOptions() as IMassPropertyOverrideOptions;
                if (options != null) flag = options.OverrideMass || options.OverrideCenterOfMass || options.OverrideMomentsOfInertia;
                if (options != null) Marshal.ReleaseComObject(options);
                if (mass != null) Marshal.ReleaseComObject(mass);
                Marshal.ReleaseComObject(extension);
            }
        }
        // Flags that cannot be read leave the component to be weighed as before.
        catch (Exception e) when (e is COMException || e is NullReferenceException || e is InvalidCastException) { flag = null; }
        flags[key] = flag;
        return flag;
    }

    // A component whose own document or any document below it overrides its mass properties, or that
    // cannot be ruled out (a component in a non-active configuration); the others weigh what their geometry does.
    private static bool MayCarryOverride(Component2 component, Dictionary<string, bool?> flags)
    {
        if (OwnOverride(component, flags) != false) return true;
        foreach (Component2 child in component.GetChildren() as object[] ?? new object[0])
            if (!child.IsSuppressed() && !child.IsEnvelope() && MayCarryOverride(child, flags)) return true;
        return false;
    }

    private static bool IsAssemblyComponent(Component2 component)
    {
        return (component.GetPathName() ?? "").EndsWith(".sldasm", StringComparison.OrdinalIgnoreCase);
    }

    // The part components below a sub-assembly that carry geometry: not suppressed, not envelopes.
    private static void CollectLeafParts(Component2 component, List<Component2> parts)
    {
        foreach (Component2 child in component.GetChildren() as object[] ?? new object[0])
        {
            if (child.IsSuppressed() || child.IsEnvelope()) continue;
            if (IsAssemblyComponent(child)) CollectLeafParts(child, parts);
            else parts.Add(child);
        }
    }

    // Sub-assemblies a Link lists as a whole. Their parts are exported and weighed with the Link,
    // but a configuration naming the parts is easier to review and keeps working if the
    // sub-assembly is restructured, so the tool reports them with the parts they stand for.
    private static List<Dictionary<string, object>> DescribeSubassemblyComponents(LinkNode root)
    {
        var rows = new List<Dictionary<string, object>>();
        Action<LinkNode> visit = null;
        visit = node => {
            if (!node.Link.isFixedFrame)
                for (int i = 0; i < node.Link.SWComponents.Count; i++)
                {
                    var component = node.Link.SWComponents[i];
                    if (!IsAssemblyComponent(component)) continue;
                    var parts = new List<Component2>();
                    CollectLeafParts(component, parts);
                    rows.Add(new Dictionary<string, object> { { "link", node.Link.Name }, { "component", component.Name2 }, { "index", i }, { "depth", component.Name2.Count(c => c == '/') + 1 }, { "parts", parts.Select(p => p.Name2).ToArray() } });
                }
            foreach (LinkNode child in node.Nodes) visit(child);
        };
        visit(root);
        return rows;
    }

    private static double[][] BoxOf(Component2 component)
    {
        var box = component.GetBox(false, false) as double[];
        if (box == null || box.Length < 6 || box.Any(v => double.IsNaN(v) || double.IsInfinity(v)) || box[3] < box[0] || box[4] < box[1] || box[5] < box[2]) return null;
        return new[] { new[] { box[0], box[1], box[2] }, new[] { box[3], box[4], box[5] } };
    }

    // Solid bodies hidden inside a part (e.g. the source body a Mirror feature kept): mass properties
    // include them, the STL does not, so they explain a mesh smaller than the CAD volume.
    // Hidden bodies belong to the part document, so instances of one file and configuration share them.
    private static readonly Dictionary<string, List<Dictionary<string, object>>> hiddenBodiesByDocument = new Dictionary<string, List<Dictionary<string, object>>>(StringComparer.OrdinalIgnoreCase);

    private static List<Dictionary<string, object>> HiddenBodies(Component2 part)
    {
        string key = part.GetPathName() + "|" + part.ReferencedConfiguration;
        List<Dictionary<string, object>> known;
        if (hiddenBodiesByDocument.TryGetValue(key, out known)) return known;
        var rows = hiddenBodiesByDocument[key] = new List<Dictionary<string, object>>();
        object info;
        foreach (Body2 body in part.GetBodies3((int)swBodyType_e.swSolidBody, out info) as object[] ?? new object[0])
        {
            if (body.Visible) continue;
            var mass = body.GetMassProperties(1.0) as double[];
            rows.Add(new Dictionary<string, object> { { "body", body.Name }, { "volume_m3", mass == null ? (object)null : mass[3] }, { "excluded_from_mass", ExcludesHidden(part) } });
        }
        return rows;
    }

    // What each Link mesh must contain, for the check after the export: the Link's CAD volume, the
    // frame its STL is written in (4x4, Link to assembly, meters) and the assembly-frame box of every
    // listed component, with the boxes of the parts of a listed sub-assembly. A component whose box
    // holds no mesh vertex did not reach the STL.
    private static Dictionary<string, object> DescribeLinkGeometry(ExportHelper helper, ModelDoc2 model, LinkNode root, Dictionary<string, object> report)
    {
        object known;
        var volumes = report.TryGetValue("linkCadVolumes", out known) ? (Dictionary<string, object>)known : new Dictionary<string, object>();
        var links = new Dictionary<string, object>();
        Action<LinkNode> visit = null;
        visit = node => {
            var link = node.Link;
            if (!link.isFixedFrame && link.SWComponents.Count != 0)
            {
                var transform = helper.GetToolFrameTransform(link.Joint.CoordinateSystemName);
                var frame = transform == null ? null : MathOps.GetTransformation(transform);
                object volume;
                if (!volumes.TryGetValue(link.Name, out volume)) volume = ComponentMassProperties(model, link.SWComponents, null)[13];
                var components = new List<Dictionary<string, object>>();
                foreach (var component in link.SWComponents)
                {
                    var row = new Dictionary<string, object> { { "name", component.Name2 }, { "assembly", IsAssemblyComponent(component) }, { "box_m", BoxOf(component) } };
                    if (IsAssemblyComponent(component))
                    {
                        var parts = new List<Component2>();
                        CollectLeafParts(component, parts);
                        row["parts"] = parts.Select(p => {
                            var part = new Dictionary<string, object> { { "name", p.Name2 }, { "box_m", BoxOf(p) } };
                            var hidden = HiddenBodies(p);
                            if (hidden.Count != 0) part["hidden_bodies"] = hidden;
                            return part;
                        }).ToList();
                    }
                    else
                    {
                        var hidden = HiddenBodies(component);
                        if (hidden.Count != 0) row["hidden_bodies"] = hidden;
                    }
                    components.Add(row);
                }
                links[link.Name] = new Dictionary<string, object> {
                    { "cad_volume_m3", volume },
                    { "frame_to_assembly", frame == null ? null : Enumerable.Range(0, 4).Select(r => Enumerable.Range(0, 4).Select(c => frame[r, c]).ToArray()).ToArray() },
                    { "components", components },
                    { "mesh_key", SafeMeshKey(model, link, volume) } };
                string[] keyRows;
                if (meshKeyRows.TryGetValue(link.Name, out keyRows)) ((Dictionary<string, object>)links[link.Name])["mesh_key_rows"] = keyRows;
            }
            foreach (LinkNode child in node.Nodes) visit(child);
        };
        visit(root);
        return links;
    }

    // Raise when the way a Link's STL is made changes, so that no older mesh is reused.
    private const string MeshExportRevision = "2"; // 2: every part of a listed sub-assembly is shown
    private static readonly Dictionary<string, string> partFileHashes = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    private static string documentBox;

    // What a Link's STL depends on, its frame aside: the bytes and configuration of every part shown for
    // it and where each sits in the assembly, its mesh quality, the size of the whole document (SolidWorks
    // derives the coarse and fine tolerances from it) and the Link's CAD volume, which also changes with
    // features cut in an assembly. Null when any of it cannot be read.
    private static string MeshKey(ModelDoc2 model, Link link, object volume)
    {
        if (!(volume is double)) return null;
        if (documentBox == null)
        {
            var box = ((AssemblyDoc)model).GetBox(0) as double[];
            if (box == null) return null;
            documentBox = string.Join(",", box.Select(v => v.ToString("0.000000", System.Globalization.CultureInfo.InvariantCulture)));
        }
        var rows = new List<string>();
        foreach (var part in SW2URDF.Headless.Visibility.WithParts(link.SWComponents).Where(c => !IsAssemblyComponent(c)))
        {
            string path = part.GetPathName();
            string hash;
            if (IsInterconnectCache(path) || IsVirtualComponent(path) || string.IsNullOrEmpty(path) || !File.Exists(path))
            {
                // Imported (3D Interconnect) and virtual parts live in files SolidWorks writes anew in each
                // session; their geometry stands in for the file: the document's mass properties and the
                // instance's box. Without them the Link is exported again.
                var own = PartMassProperties(part);
                var box = BoxOf(part);
                if (own == null || box == null) return null;
                // Nine significant digits: these numbers vary from session to session in their last digits,
                // and with hundreds of imported parts some value would cross a finer rounding every run.
                hash = "geometry:" + string.Join(",", own.Concat(box.SelectMany(b => b)).Select(v => (Math.Abs(v) < 1e-15 ? 0 : v).ToString("G9", System.Globalization.CultureInfo.InvariantCulture)));
            }
            else if (!partFileHashes.TryGetValue(path, out hash)) partFileHashes[path] = hash = HashFile(path);
            var transform = part.Transform2;
            var data = transform == null ? null : transform.ArrayData as double[];
            if (data == null) return null;
            rows.Add(hash + "|" + part.ReferencedConfiguration + "|" + string.Join(",", data.Take(13).Select(v => (Math.Abs(v) < 5e-10 ? 0 : v).ToString("0.000000000", System.Globalization.CultureInfo.InvariantCulture))));
        }
        rows.Sort(StringComparer.Ordinal);
        rows.Insert(0, "mesh " + MeshExportRevision + "|" + (link.STLQualityFine ? "fine" : "coarse") + "|" + documentBox + "|" + ((double)volume).ToString("G9", System.Globalization.CultureInfo.InvariantCulture));
        // A short digest of each line, so that two exports can be compared when a mesh was not reused.
        meshKeyRows[link.Name] = rows.Select(r => Digest(r).Substring(0, 12)).ToArray();
        return Digest(string.Join("\n", rows));
    }

    private static readonly Dictionary<string, string[]> meshKeyRows = new Dictionary<string, string[]>();

    private static string Digest(string text)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(text))).Replace("-", "").ToLowerInvariant();
    }

    // Without a key the Link is simply exported again.
    private static string SafeMeshKey(ModelDoc2 model, Link link, object volume)
    {
        try { return MeshKey(model, link, volume); }
        catch (Exception e) when (e is COMException || e is IOException || e is UnauthorizedAccessException) { return null; }
    }

    private static double[] Flatten(object value)
    {
        var numbers = new List<double>();
        foreach (object item in (IEnumerable)value)
        {
            if (item is IEnumerable && !(item is string)) numbers.AddRange(Flatten(item));
            else numbers.Add(Convert.ToDouble(item, System.Globalization.CultureInfo.InvariantCulture));
        }
        return numbers.ToArray();
    }

    // Meshes of earlier passed exports with the same key (request.mesh_reuse: key -> stl, sha256,
    // frame_to_assembly, job_id) are copied instead of exported; TryReuse checks each file again.
    private static void PlanMeshReuse(Dictionary<string, object> request, Dictionary<string, object> report)
    {
        object found;
        if (request == null || !request.TryGetValue("mesh_reuse", out found) || !report.ContainsKey("linkGeometry")) return;
        var candidates = new System.Web.Script.Serialization.JavaScriptSerializer { MaxJsonLength = int.MaxValue }.Deserialize<Dictionary<string, Dictionary<string, object>>>(File.ReadAllText(Convert.ToString(found)));
        var planned = new Dictionary<string, object>();
        foreach (var item in (Dictionary<string, object>)report["linkGeometry"])
        {
            var row = (Dictionary<string, object>)item.Value;
            Dictionary<string, object> candidate;
            if (row["mesh_key"] == null || row["frame_to_assembly"] == null || !candidates.TryGetValue((string)row["mesh_key"], out candidate)) continue;
            SW2URDF.Headless.Meshes.Reuse[item.Key] = new SW2URDF.Headless.Meshes.Plan {
                Source = Convert.ToString(candidate["stl"]), Sha256 = Convert.ToString(candidate["sha256"]), Job = Convert.ToString(candidate["job_id"]),
                FromFrame = Flatten(candidate["frame_to_assembly"]), ToFrame = Flatten(row["frame_to_assembly"]) };
            planned[item.Key] = candidate["job_id"];
        }
        report["meshReusePlanned"] = planned;
    }
}
