using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Threading;
using System.Web.Script.Serialization;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SW2URDF.URDF;
using SW2URDF.URDFExport;

// Calls the installed exporter DLL directly. Each export gets a private SW session.
// Input must be a disposable assembly copy: the exporter modifies document state.
public static partial class LegacyExportBridge
{
    private sealed class HeadlessPackageMessages : SW2URDF.UI.IMessageBox
    {
        public System.Windows.MessageBoxResult Show(string message)
        {
            Console.Error.WriteLine("Exporter notice: " + message);
            return System.Windows.MessageBoxResult.OK;
        }
        public System.Windows.MessageBoxResult Show(string message, string caption, System.Windows.MessageBoxButton buttons)
        {
            throw new InvalidOperationException("Exporter requires a decision: " + caption + ": " + message);
        }
    }
    [DllImport("ole32.dll")]
    private static extern int CreateBindCtx(uint reserved, out IBindCtx context);

    private static SldWorks FindSession(int pid)
    {
        IBindCtx context = null;
        IRunningObjectTable table = null;
        IEnumMoniker enumeration = null;
        try
        {
            Marshal.ThrowExceptionForHR(CreateBindCtx(0, out context));
            context.GetRunningObjectTable(out table);
            table.EnumRunning(out enumeration);
            var monikers = new IMoniker[1];
            while (enumeration.Next(1, monikers, IntPtr.Zero) == 0)
            {
                var moniker = monikers[0];
                try
                {
                    string name;
                    try { moniker.GetDisplayName(context, null, out name); }
                    catch (UnauthorizedAccessException) { continue; }
                    if (name == "SolidWorks_PID_" + pid)
                    {
                        object instance;
                        table.GetObject(moniker, out instance);
                        return (SldWorks)instance;
                    }
                }
                finally { Marshal.ReleaseComObject(moniker); }
            }
            return null;
        }
        finally
        {
            if (enumeration != null) Marshal.ReleaseComObject(enumeration);
            if (table != null) Marshal.ReleaseComObject(table);
            if (context != null) Marshal.ReleaseComObject(context);
        }
    }
    private static readonly string[] TogglePreferences = { "swSTLBinaryFormat", "swSTLDontTranslateToPositive", "swSTLShowInfoOnSave", "swSTLPreview", "swSTLComponentsIntoOneFile" };
    private static readonly string[] IntegerPreferences = { "swExportStlUnits", "swSTLQuality" };
    private static readonly string[] DoublePreferences = { "swViewTransitionHideShowComponent", "swSTLDeviation", "swSTLAngleTolerance" };

    private static Dictionary<string, object> Snapshot(ISldWorks app)
    {
        var values = new Dictionary<string, object>();
        foreach (string name in TogglePreferences)
            values[name] = app.GetUserPreferenceToggle((int)Enum.Parse(typeof(swUserPreferenceToggle_e), name));
        foreach (string name in IntegerPreferences)
            values[name] = app.GetUserPreferenceIntegerValue((int)Enum.Parse(typeof(swUserPreferenceIntegerValue_e), name));
        foreach (string name in DoublePreferences)
            values[name] = app.GetUserPreferenceDoubleValue((int)Enum.Parse(typeof(swUserPreferenceDoubleValue_e), name));
        return values;
    }

    // JSON round trips turn numbers into int/decimal; restore the API value types.
    private static Dictionary<string, object> NormalizePreferences(Dictionary<string, object> raw)
    {
        var values = new Dictionary<string, object>();
        foreach (string name in TogglePreferences) values[name] = Convert.ToBoolean(raw[name]);
        foreach (string name in IntegerPreferences) values[name] = Convert.ToInt32(raw[name]);
        foreach (string name in DoublePreferences) values[name] = Convert.ToDouble(raw[name], System.Globalization.CultureInfo.InvariantCulture);
        return values;
    }

    private static bool SamePreference(object expected, object actual)
    {
        if (expected is double && actual is double)
        {
            double a = (double)expected, b = (double)actual;
            return Math.Abs(a - b) <= 1e-12 * Math.Max(1.0, Math.Max(Math.Abs(a), Math.Abs(b)));
        }
        return object.Equals(expected, actual);
    }

    private static void Restore(ISldWorks app, Dictionary<string, object> values)
    {
        foreach (var entry in values)
        {
            if (entry.Key == "swSTLQuality") continue;
            if (entry.Value is bool)
                app.SetUserPreferenceToggle((int)Enum.Parse(typeof(swUserPreferenceToggle_e), entry.Key), (bool)entry.Value);
            else if (entry.Value is int)
                app.SetUserPreferenceIntegerValue((int)Enum.Parse(typeof(swUserPreferenceIntegerValue_e), entry.Key), (int)entry.Value);
            else
                app.SetUserPreferenceDoubleValue((int)Enum.Parse(typeof(swUserPreferenceDoubleValue_e), entry.Key), (double)entry.Value);
        }
        app.SetUserPreferenceIntegerValue((int)swUserPreferenceIntegerValue_e.swSTLQuality, (int)values["swSTLQuality"]);
    }

    private static void ConfigureWorkspaceLog(string directory)
    {
        // The original logger otherwise overwrites a log in the user's home.
        var initialized = typeof(SW2URDF.Utilities.Logger).GetField("Initialized", BindingFlags.NonPublic | BindingFlags.Static);
        if (initialized == null) throw new InvalidOperationException("Unsupported exporter logger ABI.");
        initialized.SetValue(null, true);
        var layout = new log4net.Layout.PatternLayout("%date %-5level %message%newline");
        layout.ActivateOptions();
        var appender = new log4net.Appender.RollingFileAppender();
        appender.File = Path.Combine(directory, "bridge-export.log");
        appender.AppendToFile = false;
        appender.Layout = layout;
        appender.ImmediateFlush = true;
        appender.ActivateOptions();
        log4net.Config.BasicConfigurator.Configure(appender);
    }

    // Starts a hidden SolidWorks owned by this run and records it for PID+start-time cleanup.
    private static SldWorks StartPrivateSession(string output, string workingDirectory, Dictionary<string, object> report, out Process privateProcess)
    {
        privateProcess = null;
        string swFolder = Microsoft.Win32.Registry.GetValue(@"HKEY_LOCAL_MACHINE\SOFTWARE\SolidWorks\SOLIDWORKS 2026\Setup", "SolidWorks Folder", null) as string;
        if (string.IsNullOrEmpty(swFolder)) throw new InvalidOperationException("SolidWorks 2026 installation was not found.");
        var startInfo = new ProcessStartInfo(Path.Combine(swFolder, "SLDWORKS.exe"), "/r") {
            UseShellExecute = false, WindowStyle = ProcessWindowStyle.Hidden,
            WorkingDirectory = workingDirectory
        };
        // Any auto-loaded legacy add-in must write its logs inside this run.
        string root = Path.GetPathRoot(output);
        startInfo.EnvironmentVariables["HOMEDRIVE"] = root.TrimEnd('\\');
        startInfo.EnvironmentVariables["HOMEPATH"] = output.Substring(root.Length - 1);
        privateProcess = Process.Start(startInfo);
        int pid = privateProcess.Id;
        report["solidworksPid"] = pid;
        File.WriteAllText(Path.Combine(output, "session.json"), new JavaScriptSerializer().Serialize(new { pid = pid, started = privateProcess.StartTime.ToString("o") }));
        SldWorks app = null;
        var startup = Stopwatch.StartNew();
        while (app == null && startup.Elapsed.TotalSeconds < 120)
        {
            if (privateProcess.HasExited) throw new InvalidOperationException("Private SolidWorks exited during startup.");
            try { app = FindSession(pid); }
            catch (InvalidCastException) { app = null; } // ROT may appear before COM is ready.
            catch (COMException e)
            {
                if (e.ErrorCode != unchecked((int)0x80010114) && e.ErrorCode != unchecked((int)0x800401E3)) throw;
                app = null;
            }
            if (app == null) Thread.Sleep(500);
        }
        if (app == null) throw new TimeoutException("Private SolidWorks did not register its COM session within 120 seconds.");
        if (app.GetProcessID() != pid) throw new InvalidOperationException("SolidWorks session PID mismatch.");
        return app;
    }

    private static void RequireSolidWorks2026(SldWorks app, Dictionary<string, object> report)
    {
        string revision = app.RevisionNumber();
        report["revision"] = revision;
        if (!revision.StartsWith("34.")) throw new InvalidOperationException("This build requires SolidWorks 2026 (API major 34).");
        app.Visible = false;
    }

    // Recovery for a run that was killed before restoring the user's STL preferences.
    private static int RestorePreferencesMain(Dictionary<string, object> request)
    {
        string output = Path.GetFullPath(Convert.ToString(request["output_dir"]));
        Directory.CreateDirectory(output);
        var report = new Dictionary<string, object> { { "operation", "restore_preferences" }, { "status", "failed" } };
        SldWorks app = null;
        Process privateProcess = null;
        bool owned = false;
        int code = 1;
        try
        {
            var target = NormalizePreferences(new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(Convert.ToString(request["preferences_path"]))));
            report["target"] = target;
            Console.Error.WriteLine("Starting private SolidWorks session to restore STL preferences...");
            app = StartPrivateSession(output, output, report, out privateProcess);
            owned = true;
            RequireSolidWorks2026(app, report);
            var found = Snapshot(app);
            report["preferencesFound"] = found;
            var changed = new List<string>();
            foreach (var item in target) if (!SamePreference(item.Value, found[item.Key])) changed.Add(item.Key);
            report["changedKeys"] = changed;
            if (changed.Count != 0) Restore(app, target);
            var after = Snapshot(app);
            report["preferencesAfter"] = after;
            bool restored = true;
            foreach (var item in target) if (!SamePreference(item.Value, after[item.Key])) restored = false;
            report["preferencesRestored"] = restored;
            report["status"] = restored ? "restored" : "failed";
            code = restored ? 0 : 1;
        }
        catch (Exception e)
        {
            report["error"] = e.ToString();
            Console.Error.WriteLine(e.Message);
        }
        finally
        {
            if (app != null && owned)
            {
                // A normal exit persists the restored values to the user profile.
                try { app.ExitApp(); report["cleanup"] = "private_session_closed"; }
                catch (Exception e) { report["cleanupError"] = e.Message; code = 1; }
            }
            if (app != null) Marshal.ReleaseComObject(app);
            if (privateProcess != null) privateProcess.Dispose();
            string json = new JavaScriptSerializer().Serialize(report);
            File.WriteAllText(Path.Combine(output, "restore-result.json"), json);
            Console.WriteLine(json);
        }
        return code;
    }

    [STAThread]
    public static int Main(string[] args)
    {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        Dictionary<string, object> request = null;
        if (args.Length == 1 && File.Exists(args[0]))
        {
            request = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(args[0]));
            if (request.ContainsKey("operation") && Convert.ToString(request["operation"]) == "restore_preferences") return RestorePreferencesMain(request);
            args = new[] { Convert.ToString(request["model_path"]), Convert.ToString(request["output_dir"]), request.ContainsKey("package_name") ? Convert.ToString(request["package_name"]) : "robot_description" };
        }
        if (request == null || args.Length != 3) { Console.Error.WriteLine("Usage: SolidWorksUrdf.exe <request.json>; use tool_cli.py for normal calls."); return 2; }
        string modelPath = Path.GetFullPath(args[0]);
        string output = Path.GetFullPath(args[1]);
        string package = args[2];
        Directory.CreateDirectory(output);
        var report = new Dictionary<string, object>();
        report["status"] = "failed";
        report["input"] = modelPath;
        report["package"] = package;
        report["exporterVersion"] = FileVersionInfo.GetVersionInfo(typeof(ExportHelper).Assembly.Location).FileVersion;
        SldWorks app = null;
        ModelDoc2 doc = null;
        bool owned = false;
        Dictionary<string, object> preferences = null;
        int code = 1;
        Process privateProcess = null;
        string operation = request != null && request.ContainsKey("operation") ? Convert.ToString(request["operation"]) : "export";
        string configPath = request != null && request.ContainsKey("config_path") ? Convert.ToString(request["config_path"]) : null;
        bool pack = request != null;
        report["operation"] = operation;
        try
        {
            string extensionName = Path.GetExtension(modelPath).ToLowerInvariant();
            int documentType = extensionName == ".sldasm" ? 2 : extensionName == ".sldprt" ? 1 : 0;
            if (!File.Exists(modelPath) || documentType == 0)
                throw new ArgumentException("Input must be an existing assembly or part.");
            report["documentType"] = documentType;
            if (!System.Text.RegularExpressions.Regex.IsMatch(package, "^[a-z][a-z0-9_]*$"))
                throw new ArgumentException("Package name must use lowercase letters, digits and underscores.");
            if (operation != "export" && operation != "inspect" && operation != "prepare") throw new ArgumentException("Unsupported operation.");
            if (Directory.Exists(Path.Combine(output, package))) throw new IOException("Output package already exists.");
            ConfigureWorkspaceLog(output);
            URDFPackage.MessageBox = new HeadlessPackageMessages();
            Console.Error.WriteLine("Starting private SolidWorks session...");
            app = StartPrivateSession(output, Path.GetDirectoryName(modelPath), report, out privateProcess);
            owned = true;
            RequireSolidWorks2026(app, report);
            report["startupPreferences"] = Snapshot(app);
            // Persist before any change so a killed run can still be restored.
            File.WriteAllText(Path.Combine(output, "preferences-snapshot.json"), new JavaScriptSerializer().Serialize(report["startupPreferences"]));
            if (request != null && request.ContainsKey("source_manifest"))
            {
                currentSourceManifest = new JavaScriptSerializer().Deserialize<SourceManifest>(File.ReadAllText(Convert.ToString(request["source_manifest"])));
                if (currentSourceManifest.unsaved_changes || !string.Equals(Path.GetFullPath(currentSourceManifest.source_path), Path.GetFullPath(modelPath), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Active source manifest does not match the requested saved model.");
                report["activeSourceManifestVerified"] = true;
            }
            if (pack)
            {
                Console.Error.WriteLine("Creating source and dependency snapshots without opening originals...");
                modelPath = StageSourceSnapshot(app, modelPath, output, report);
            }
            app.SetCurrentWorkingDirectory(Path.GetDirectoryName(modelPath));
            int errors = 0, warnings = 0;
            Console.Error.WriteLine(pack ? "Opening source snapshot read-only..." : "Opening disposable assembly...");
            doc = app.OpenDoc6(modelPath, documentType,
                (int)swOpenDocOptions_e.swOpenDocOptions_Silent | (pack ? (int)swOpenDocOptions_e.swOpenDocOptions_ReadOnly : 0), currentSourceManifest == null ? "" : currentSourceManifest.configuration_name, ref errors, ref warnings);
            report["openErrors"] = errors;
            report["openWarnings"] = warnings;
            if (doc == null || errors != 0) throw new InvalidOperationException("Assembly open failed: " + errors);
            if (documentType == 2) ((AssemblyDoc)doc).ResolveAllLightWeightComponents(false);
            if (documentType == 2) RequireLoadedComponents(doc, report);
            if (pack)
            {
                string copiedAssembly = PrepareAssemblyCopy(doc, modelPath, output, report);
                app.CloseDoc(doc.GetTitle());
                Marshal.ReleaseComObject(doc);
                doc = null;
                // Close dependent source documents before opening writable copies.
                CloseAllPrivateDocuments(app);
                modelPath = copiedAssembly;
                report["prepared_model"] = copiedAssembly;
                app.SetCurrentWorkingDirectory(Path.GetDirectoryName(modelPath));
                errors = 0; warnings = 0;
                doc = app.OpenDoc6(modelPath, documentType, (int)swOpenDocOptions_e.swOpenDocOptions_Silent, currentSourceManifest == null ? "" : currentSourceManifest.configuration_name, ref errors, ref warnings);
                if (doc == null || errors != 0) throw new InvalidOperationException("Prepared assembly could not be opened: " + errors);
                if (documentType == 2) ((AssemblyDoc)doc).ResolveAllLightWeightComponents(false);
            }
            // Coarse/fine deviation is document-size dependent: compare in the
            // same active document, rather than comparing an empty SW session.
            preferences = Snapshot(app);
            report["preferencesBefore"] = preferences;
            var extension = doc.Extension;
            var mass = (IMassProperty2)extension.CreateMassProperty2();
            mass.UseSystemUnits = true;
            mass.IncludeHiddenBodiesOrComponents = true;
            report["cadAssemblyMassKg"] = mass.Mass;
            report["configurationName"] = doc.ConfigurationManager.ActiveConfiguration.Name;
            if (currentSourceManifest != null)
            {
                var actualComponents = ((AssemblyDoc)doc).GetComponents(false) as object[] ?? new object[0];
                report["activeInstanceCount"] = actualComponents.Length;
                if (actualComponents.Length != currentSourceManifest.component_count || Math.Abs(mass.Mass - currentSourceManifest.mass_kg) > Math.Max(1e-8, currentSourceManifest.mass_kg * 1e-6))
                    throw new InvalidOperationException("Prepared geometry differs from the verified active source model.");
                foreach (object item in actualComponents)
                {
                    var component = (Component2)item;
                    if (!component.IsSuppressed() && component.GetModelDoc2() == null) throw new InvalidOperationException("An active component did not load: " + component.Name2);
                    Marshal.ReleaseComObject(item);
                }
                report["activeGeometryVerified"] = true;
            }
            Marshal.ReleaseComObject(mass);
            Marshal.ReleaseComObject(extension);
            if (operation == "prepare")
            {
                report["components"] = documentType == 2 ? (object)DescribeComponents(doc) : new object[0];
                report["status"] = "prepared";
                code = 0;
                goto OperationComplete;
            }
            if (documentType == 1)
            {
                var partHelper = new ExportHelper(app);
                partHelper.SavePath = output;
                partHelper.PackageName = package;
                partHelper.CreateRobotFromActiveModel();
                partHelper.URDFRobot.Name = package;
                partHelper.URDFRobot.BaseLink.Name = "base_link";
                report["configuration"] = null;
                report["components"] = new object[0];
                if (operation == "inspect") { report["status"] = "inspected"; code = 0; goto OperationComplete; }
                if (!string.IsNullOrEmpty(configPath)) throw new ArgumentException("JSON Link trees apply to assemblies; part export is a single rigid link.");
                partHelper.ExportLink(false);
                string partUrdf = Path.Combine(output, package, "urdf", package + ".urdf");
                if (!File.Exists(partUrdf)) throw new IOException("Part export produced no URDF.");
                report["urdf"] = partUrdf;
                report["jointNames"] = new string[0];
                report["status"] = "exported";
                code = 0;
                goto OperationComplete;
            }
            bool configError;
            Console.Error.WriteLine("Loading saved URDF configuration...");
            LinkNode node;
            ConfigDocument supplied = null;
            if (!string.IsNullOrEmpty(configPath))
            {
                supplied = new JavaScriptSerializer().Deserialize<ConfigDocument>(File.ReadAllText(configPath));
                node = BuildConfigTree(doc, supplied);
            }
            else
            {
                node = ConfigurationSerialization.LoadBaseNodeFromModel(doc, out configError);
                if (configError) throw new InvalidOperationException("Incompatible saved URDF configuration.");
                if (node != null)
                {
                    var problemLinks = new List<string>();
                    CommonSwOperations.LoadSWComponents(doc, node, problemLinks);
                    if (problemLinks.Count != 0) throw new InvalidOperationException("Unresolved component references: " + string.Join(", ", problemLinks));
                }
            }
            report["configuration"] = node == null ? null : DescribeConfig(node, package);
            report["components"] = DescribeComponents(doc);
            List<string> unassigned = null, overlapping = null;
            if (node != null)
            {
                CheckComponentCoverage(doc, node, out unassigned, out overlapping);
                report["unassignedComponents"] = unassigned;
                report["overlappingComponents"] = overlapping;
            }
            if (operation == "inspect")
            {
                report["status"] = "inspected";
                code = 0;
                goto OperationComplete;
            }
            if (node == null) throw new InvalidOperationException("No saved URDF configuration. Inspect the assembly and provide a JSON configuration.");
            ValidateResolvedTree(node);
            RequireComponentCoverage(unassigned, overlapping);
            report["componentsResolved"] = true;
            var helper = new ExportHelper(app);
            helper.SetComputeInertial(true);
            helper.SetComputeJointKinematics(supplied == null || supplied.recompute_kinematics);
            helper.SetComputeJointLimits(supplied == null || supplied.recompute_kinematics);
            helper.SetComputeVisualCollision(true);
            helper.SavePath = output;
            helper.PackageName = package;
            PrepareLinkFrames(helper, node, supplied == null || supplied.recompute_kinematics, report);
            Console.Error.WriteLine("Building links through original ExportHelper...");
            if (!helper.CreateRobotFromTreeView(node)) throw new InvalidOperationException("Robot construction failed.");
            if (supplied != null && supplied.recompute_kinematics) RequireConfiguredJointTypes(node, supplied, report);
            ApplyComponentMassProperties(helper, doc, node, report);
            Console.Error.WriteLine("Exporting URDF and STL through original ExportHelper...");
            helper.ExportRobot(true, MeshExportFormat.STL);
            string urdf = Path.Combine(output, package, "urdf", package + ".urdf");
            if (!File.Exists(urdf)) throw new IOException("Exporter returned without producing a URDF.");
            report["urdf"] = urdf;
            report["jointNames"] = helper.GetJointNames();
            report["status"] = "exported";
            code = 0;
        OperationComplete: ;
        }
        catch (Exception e)
        {
            report["error"] = e.ToString();
            Console.Error.WriteLine(e.Message);
        }
        finally
        {
            if (app != null && owned)
            {
                try
                {
                    if (preferences != null)
                    {
                        Restore(app, preferences);
                        var after = Snapshot(app);
                        report["preferencesAfter"] = after;
                        bool restored = true;
                        foreach (var item in preferences) if (!SamePreference(item.Value, after[item.Key])) restored = false;
                        report["preferencesRestored"] = restored;
                        if (!restored) code = 1;
                    }
                    if (doc != null) { app.CloseDoc(doc.GetTitle()); Marshal.ReleaseComObject(doc); doc = null; }
                    app.ExitApp();
                    report["cleanup"] = "private_session_closed_without_saving";
                }
                catch (Exception e) { report["cleanupError"] = e.Message; code = 1; }
            }
            if (app != null) Marshal.ReleaseComObject(app);
            if (privateProcess != null) privateProcess.Dispose();
            if (report.ContainsKey("sourceHashesBefore"))
            {
                try
                {
                    var before = (Dictionary<string, string>)report["sourceHashesBefore"];
                    var after = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                    foreach (string file in before.Keys) after[file] = HashFile(file);
                    bool unchanged = true;
                    foreach (var item in before) if (item.Value != after[item.Key]) unchanged = false;
                    report["sourceHashesAfter"] = after;
                    report["sourceUnchanged"] = unchanged;
                    if (!unchanged) code = 1;
                }
                catch (Exception e) { report["sourceAuditError"] = e.Message; code = 1; }
            }
            string json = new JavaScriptSerializer().Serialize(report);
            File.WriteAllText(Path.Combine(output, "bridge-result.json"), json);
            Console.WriteLine(json);
        }
        return code;
    }
}
