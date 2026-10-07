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
                if (!component.exists && !component.suppressed) throw new FileNotFoundException("Active model has a missing component.", component.path);
                if (component.exists) files.Add(Path.GetFullPath(component.path));
            }
        }
        var before = files.ToDictionary(p => p, HashFile, StringComparer.OrdinalIgnoreCase);
        report["sourceHashesBefore"] = before;
        string snapshotRoot = Path.Combine(output, "snapshot");
        var mapping = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string file in files)
        {
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
                    throw new IOException("Cannot map snapshot reference: " + originalReference);
                }
                if (!app.ReplaceReferencedDocument(entry.Value, originalReference, copiedReference))
                    throw new IOException("Cannot rewrite snapshot reference: " + originalReference);
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
            if (originals.Length == 0) throw new IOException("No files were found for Pack and Go.");
            var before = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string file in originals)
            {
                if (File.Exists(file)) before[file] = HashFile(file);
                else if (!file.Contains("^"))
                {
                    if (IsInterconnectCache(file) || ignoredSavedReferences.Contains(file)) continue;
                    throw new FileNotFoundException("An assembly dependency is missing.", file);
                }
            }
            // A caller may already have audited original files before staging.
            bool staged = report.ContainsKey("sourceSnapshotMapping");
            if (!staged) report["sourceHashesBefore"] = before;
            if (staged)
            {
                string snapshotRoot = Path.GetFullPath(Convert.ToString(report["snapshotDirectory"])) + Path.DirectorySeparatorChar;
                foreach (string file in originals)
                    if (!IsInterconnectCache(file) && !ignoredSavedReferences.Contains(file) && !Path.GetFullPath(file).StartsWith(snapshotRoot, StringComparison.OrdinalIgnoreCase))
                        throw new IOException("Snapshot has an external dependency; refusing to run Pack and Go: " + file);
            }
            var used = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var targets = new string[originals.Length];
            for (int i = 0; i < originals.Length; i++)
            {
                if (!File.Exists(originals[i]) && (IsInterconnectCache(originals[i]) || ignoredSavedReferences.Contains(originals[i]))) { targets[i] = ""; continue; }
                string name = Path.GetFileName(originals[i]);
                // Distinct directories may contain identically named vendor parts.
                if (!used.Add(name))
                {
                    name = Path.GetFileNameWithoutExtension(name) + "_dep_" + i + Path.GetExtension(name);
                    if (!used.Add(name)) throw new IOException("Cannot assign unique dependency filenames.");
                }
                targets[i] = Path.Combine(destination, name);
            }
            if (!pack.SetDocumentSaveToNames(targets)) throw new IOException("Pack and Go rejected destination filenames.");
            object effectiveRaw, effectiveStatus;
            if (!pack.GetDocumentSaveToNames(out effectiveRaw, out effectiveStatus)) throw new IOException("Cannot verify Pack and Go destinations.");
            var effective = ((IEnumerable)effectiveRaw).Cast<object>().Select(Convert.ToString).ToArray();
            report["packDestinations"] = effective;
            if (effective.Length != targets.Length || effective.Where((value, index) => !string.Equals(value, targets[index], StringComparison.OrdinalIgnoreCase)).Any())
                throw new IOException("Pack and Go destination verification failed.");
            Console.Error.WriteLine("Copying " + originals.Length + " CAD dependencies through Pack and Go...");
            object results = extension.SavePackAndGo(pack);
            if (results == null) throw new IOException("Pack and Go returned no save status.");
            var statuses = ((IEnumerable)results).Cast<object>().Select(Convert.ToInt32).ToArray();
            report["packSaveStatuses"] = statuses;
            if (statuses.Where((s, i) => s != 0 && !(s == 3 && statuses.Length == targets.Length && targets[i] == "")).Any()) throw new IOException("Pack and Go save failed: " + string.Join(",", statuses));
            var after = before.Keys.ToDictionary(p => p, HashFile, StringComparer.OrdinalIgnoreCase);
            if (!staged)
            {
                report["sourceHashesAfter"] = after;
                bool unchanged = before.All(p => after[p.Key] == p.Value);
                report["sourceUnchanged"] = unchanged;
                if (!unchanged) throw new IOException("Source CAD changed during preparation; refusing export.");
            }
            var mapping = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < originals.Length; i++)
            {
                mapping[originals[i]] = targets[i];
                if (targets[i] != "" && !File.Exists(targets[i]) && !originals[i].Contains("^")) throw new IOException("Pack and Go omitted " + originals[i]);
            }
            report["dependencyMapping"] = mapping;
            report["dependencyCount"] = originals.Length;
            string originalModel = Path.GetFullPath(source);
            string packed = mapping.FirstOrDefault(p => string.Equals(Path.GetFullPath(p.Key), originalModel, StringComparison.OrdinalIgnoreCase)).Value;
            if (string.IsNullOrEmpty(packed) || !File.Exists(packed)) throw new IOException("Packed top-level assembly was not found.");
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
        var nodes = new Dictionary<string, LinkNode>();
        foreach (var settings in config.links)
        {
            if (string.IsNullOrWhiteSpace(settings.name) || nodes.ContainsKey(settings.name)) throw new ArgumentException("Duplicate or empty link name.");
            var link = new Link { Name = settings.name, isFixedFrame = settings.frame_only, STLQualityFine = settings.mesh_quality == "fine" };
            link.Joint.CoordinateSystemName = settings.coordinate_system;
            foreach (string name in settings.components ?? new string[0])
            {
                Component2 component;
                if (!components.TryGetValue(name, out component)) throw new ArgumentException("Component not found: " + name);
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
                if (root != null) throw new ArgumentException("Multiple root links.");
                root = node;
            }
            else
            {
                LinkNode parent;
                if (!nodes.TryGetValue(settings.parent, out parent) || parent == node) throw new ArgumentException("Invalid parent link.");
                parent.Nodes.Add(node);
                node.Link.Parent = parent.Link;
                node.Link.Joint.Parent.Name = parent.Link.Name;
                node.Link.Joint.Child.Name = node.Link.Name;
            }
        }
        if (root == null) throw new ArgumentException("No root link.");
        var visited = new HashSet<string>();
        Action<LinkNode> visit = null;
        visit = node => { if (!visited.Add(node.Link.Name)) throw new ArgumentException("Cycle in Link tree."); foreach (LinkNode child in node.Nodes) visit(child); };
        visit(root);
        if (visited.Count != nodes.Count) throw new ArgumentException("Disconnected Link tree.");
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

    public const string AssemblyOriginFrame = "Assembly Origin";
    private const string AutomaticFrame = "Automatically Generate";

    private static Matrix<double> FramePose(ExportHelper helper, string name)
    {
        MathTransform transform = null;
        try { transform = helper.GetToolFrameTransform(name); }
        catch (NullReferenceException) { }
        if (transform == null) throw new InvalidOperationException("Coordinate system not found in the assembly: " + name);
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
                    if (PoseDifference(FramePose(helper, joint.CoordinateSystemName), configured) > 1e-6)
                        throw new InvalidOperationException("The coordinate system created for " + joint.Name + " does not match its configured origin.");
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

    // Returns mass, center of mass (3) and the 3x3 moment of inertia at the center of mass.
    private static double[] ComponentMassProperties(ModelDoc2 model, IEnumerable<Component2> components, MathTransform frame)
    {
        var extension = model.Extension;
        var mass = (IMassProperty2)extension.CreateMassProperty2();
        try
        {
            mass.UseSystemUnits = true;
            mass.IncludeHiddenBodiesOrComponents = true;
            mass.SelectedItems = components.Select(c => new DispatchWrapper(c)).ToArray();
            if (frame != null) mass.SetCoordinateSystem(frame);
            // Without this the properties still describe the previous selection.
            if (!mass.Recalculate()) throw new InvalidOperationException("Component mass properties could not be recalculated.");
            var center = (double[])mass.CenterOfMass;
            var moment = (double[])mass.GetMomentOfInertia((int)swMomentsOfInertiaReferenceFrame_e.swMomentsOfInertiaReferenceFrame_CenterOfMass);
            return new[] { mass.Mass, center[0], center[1], center[2] }.Concat(moment).ToArray();
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
        var cad = links.ToDictionary(l => l, l => ComponentMassProperties(model, l.SWComponents, helper.GetToolFrameTransform(l.Joint.CoordinateSystemName)));
        var disagreeing = new List<string>();
        var overrides = new List<object>();
        foreach (var link in links)
        {
            var values = cad[link];
            if (Close(values[0], link.Inertial.Mass.Value, 1e-5, 1e-12))
            {
                var center = link.Inertial.Origin.GetXYZ();
                if (!Enumerable.Range(0, 3).All(i => Close(values[1 + i], center[i], 0, 1e-6))) disagreeing.Add(link.Name + " center of mass");
                continue;
            }
            var components = link.SWComponents
                .Select(c => new { name = c.Name2, cad = ComponentMassProperties(model, new[] { c }, null)[0], geometric = GeometricMass(model, new[] { c }) })
                .Where(c => !Close(c.cad, c.geometric, 1e-5, 1e-12))
                .Select(c => new Dictionary<string, object> { { "component", c.name }, { "cad_mass_kg", c.cad }, { "geometric_mass_kg", c.geometric } }).ToList();
            overrides.Add(new Dictionary<string, object> { { "link", link.Name }, { "cad_mass_kg", values[0] }, { "geometric_mass_kg", link.Inertial.Mass.Value }, { "components", components } });
        }
        report["massOverrides"] = overrides;
        double total = links.Sum(l => cad[l][0]), assembly = Convert.ToDouble(report["cadAssemblyMassKg"]);
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
}
