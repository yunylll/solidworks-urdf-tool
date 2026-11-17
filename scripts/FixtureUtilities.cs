using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using SolidWorks.Interop.sldworks;

// Test-only fixture authoring. Copies and rewrites only workspace-owned sample CAD.
public static partial class LegacyExportBridge
{
    public static void CreateNestedFixture(string original, string destination)
    {
        original = Path.GetFullPath(original);
        destination = Path.GetFullPath(destination);
        string workspace = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..")) + Path.DirectorySeparatorChar;
        if (!original.StartsWith(workspace, StringComparison.OrdinalIgnoreCase) || !destination.StartsWith(workspace, StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Fixture paths must be inside the workspace.");
        if (Directory.Exists(destination)) throw new IOException("Fixture destination already exists.");
        Directory.CreateDirectory(Path.Combine(destination, "assembly"));
        Directory.CreateDirectory(Path.Combine(destination, "parts"));
        string folder = Path.GetDirectoryName(original);
        string assembly = Path.Combine(destination, "assembly", Path.GetFileName(original));
        File.Copy(original, assembly);
        string swFolder = Microsoft.Win32.Registry.GetValue(@"HKEY_LOCAL_MACHINE\SOFTWARE\SolidWorks\SOLIDWORKS 2026\Setup", "SolidWorks Folder", null) as string;
        SldWorks app = null;
        var start = new ProcessStartInfo(Path.Combine(swFolder, "SLDWORKS.exe"), "/r") { UseShellExecute = false, WindowStyle = ProcessWindowStyle.Hidden };
        string drive = Path.GetPathRoot(destination);
        start.EnvironmentVariables["HOMEDRIVE"] = drive.TrimEnd('\\');
        start.EnvironmentVariables["HOMEPATH"] = destination.Substring(drive.Length - 1);
        var process = Process.Start(start);
        File.WriteAllText(Path.Combine(destination, "session.json"), new JavaScriptSerializer().Serialize(new { pid = process.Id, started = process.StartTime.ToString("o") }));
        try
        {
            var watch = Stopwatch.StartNew();
            while (app == null && watch.Elapsed.TotalSeconds < 120)
            {
                try { app = FindSession(process.Id); } catch (COMException) { } catch (InvalidCastException) { }
                if (app == null) Thread.Sleep(500);
            }
            if (app == null || app.GetProcessID() != process.Id) throw new InvalidOperationException("Private fixture session unavailable.");
            app.Visible = false;
            app.SetCurrentWorkingDirectory(folder);
            string[] references = app.GetDocumentDependencies2(original, false, false, false) as string[] ?? new string[0];
            for (int i = 1; i < references.Length; i += 2)
            {
                string name = Path.GetFileName(references[i]);
                string from = Path.Combine(folder, name);
                string target = Path.Combine(destination, "parts", name);
                if (!File.Exists(target)) File.Copy(from, target);
                if (!app.ReplaceReferencedDocument(assembly, references[i], target)) throw new IOException("Cannot author nested test reference: " + references[i]);
            }
            Console.WriteLine(assembly);
        }
        finally
        {
            if (app != null) { app.ExitApp(); Marshal.ReleaseComObject(app); }
            process.Dispose();
        }
    }
}

public static partial class LegacyExportBridge
{
    // Copies an assembly and its parts into one workspace folder, then turns one component
    // into a virtual component stored inside the copied assembly.
    public static void CreateVirtualFixture(string original, string destination, string componentName)
    {
        original = Path.GetFullPath(original);
        destination = Path.GetFullPath(destination);
        string workspace = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..")) + Path.DirectorySeparatorChar;
        if (!original.StartsWith(workspace, StringComparison.OrdinalIgnoreCase) || !destination.StartsWith(workspace, StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Fixture paths must be inside the workspace.");
        if (Directory.Exists(destination)) throw new IOException("Fixture destination already exists: " + destination);
        Directory.CreateDirectory(destination);
        string folder = Path.GetDirectoryName(original);
        string assembly = Path.Combine(destination, Path.GetFileName(original));
        File.Copy(original, assembly);
        Process process;
        var app = StartFixtureSession(destination, out process);
        try
        {
            app.SetCurrentWorkingDirectory(folder);
            string[] references = app.GetDocumentDependencies2(original, false, false, false) as string[] ?? new string[0];
            for (int i = 1; i < references.Length; i += 2)
            {
                string name = Path.GetFileName(references[i]);
                string target = Path.Combine(destination, name);
                if (!File.Exists(target)) File.Copy(Path.Combine(folder, name), target);
                if (!app.ReplaceReferencedDocument(assembly, references[i], target)) throw new IOException("Cannot author fixture reference: " + references[i]);
            }
            app.SetCurrentWorkingDirectory(destination);
            int errors = 0, warnings = 0;
            var doc = app.OpenDoc6(assembly, 2, 1, "", ref errors, ref warnings);
            if (doc == null) throw new IOException("Cannot open fixture copy: " + errors);
            Component2 component = null;
            foreach (Component2 item in ((AssemblyDoc)doc).GetComponents(true) as object[] ?? new object[0])
                if (item.Name2 == componentName) component = item;
            if (component == null) throw new ArgumentException("Component not found: " + componentName);
            if (!component.MakeVirtual2(false)) throw new InvalidOperationException("SolidWorks could not make " + componentName + " virtual.");
            if (!doc.Save3(1, ref errors, ref warnings)) throw new IOException("Cannot save fixture: " + errors);
            foreach (Component2 item in ((AssemblyDoc)doc).GetComponents(true) as object[] ?? new object[0])
                if (item.IsVirtual) Console.WriteLine("VIRTUAL=" + item.Name2);
            app.CloseDoc(doc.GetTitle());
            Console.WriteLine("FIXTURE=" + assembly);
        }
        finally
        {
            app.ExitApp();
            Marshal.ReleaseComObject(app);
            process.Dispose();
        }
    }

    // Copies an assembly and the part and assembly files next to it into one workspace folder, then
    // nests the copy inside new assemblies, levels deep: wrap_1 holds the copy, wrap_2 holds wrap_1, and
    // so on. A sub-assembly of the copy then sits levels + 1 deep, as racks of vials do in a workstation.
    // One private session does all of it and exits once.
    public static void CreateWrapperFixture(string original, string destination, int levels)
    {
        original = Path.GetFullPath(original);
        destination = Path.GetFullPath(destination);
        string workspace = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..")) + Path.DirectorySeparatorChar;
        if (!original.StartsWith(workspace, StringComparison.OrdinalIgnoreCase) || !destination.StartsWith(workspace, StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Fixture paths must be inside the workspace.");
        if (Directory.Exists(destination)) throw new IOException("Fixture destination already exists: " + destination);
        if (levels < 1 || levels > 5) throw new ArgumentException("levels must be 1 to 5.");
        string folder = Path.GetDirectoryName(original), model = Path.Combine(destination, "model");
        Directory.CreateDirectory(model);
        foreach (string file in Directory.GetFiles(folder))
        {
            string extension = Path.GetExtension(file).ToLowerInvariant();
            if (extension == ".sldprt" || extension == ".sldasm") File.Copy(file, Path.Combine(model, Path.GetFileName(file)));
        }
        Process process;
        var app = StartFixtureSession(destination, out process);
        try
        {
            app.SetCurrentWorkingDirectory(model);
            // Point every copied assembly at the copies, never at the originals.
            foreach (string assembly in Directory.GetFiles(model, "*.sldasm"))
            {
                string[] references = app.GetDocumentDependencies2(assembly, false, false, false) as string[] ?? new string[0];
                for (int i = 1; i < references.Length; i += 2)
                {
                    string target = Path.Combine(model, Path.GetFileName(references[i]));
                    if (!File.Exists(target)) throw new FileNotFoundException("Fixture reference is not next to the original assembly: " + references[i]);
                    if (!string.Equals(Path.GetFullPath(references[i]), target, StringComparison.OrdinalIgnoreCase) && !app.ReplaceReferencedDocument(assembly, references[i], target))
                        throw new IOException("Cannot author fixture reference: " + references[i]);
                }
            }
            string template = app.GetUserPreferenceStringValue((int)SolidWorks.Interop.swconst.swUserPreferenceStringValue_e.swDefaultTemplateAssembly);
            if (string.IsNullOrEmpty(template) || !File.Exists(template))
            {
                string templates = Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.CommonApplicationData), "SOLIDWORKS", "SOLIDWORKS 2026", "templates");
                template = Directory.Exists(templates) ? Directory.GetFiles(templates, "*.asmdot").FirstOrDefault() : null;
            }
            if (template == null) throw new FileNotFoundException("No SolidWorks assembly template found.");
            int errors = 0, warnings = 0;
            string inner = Path.Combine(model, Path.GetFileName(original));
            var opened = app.OpenDoc6(inner, 2, 1, "", ref errors, ref warnings);
            if (opened == null) throw new IOException("Cannot open fixture copy: " + errors);
            ModelDoc2 top = null;
            for (int level = 1; level <= levels; level++)
            {
                var wrapper = (ModelDoc2)app.NewDocument(template, 0, 0, 0);
                if (wrapper == null) throw new InvalidOperationException("Cannot create an assembly from " + template);
                var added = ((AssemblyDoc)wrapper).AddComponent5(inner, 0, "", false, "", 0, 0, 0);
                if (added == null) throw new InvalidOperationException("Cannot insert " + inner + " into wrapper level " + level);
                string path = Path.Combine(destination, "wrap_" + level + ".SLDASM");
                if (!wrapper.Extension.SaveAs(path, 0, 1, null, ref errors, ref warnings)) throw new IOException("Cannot save fixture " + path + ": " + errors);
                inner = path;
                top = wrapper;
            }
            ((AssemblyDoc)top).ResolveAllLightWeightComponents(false);
            foreach (Component2 item in ((AssemblyDoc)top).GetComponents(false) as object[] ?? new object[0])
                Console.WriteLine("COMPONENT=" + item.Name2 + "|" + item.GetPathName());
            Console.WriteLine("FIXTURE=" + inner);
            // Closing an assembly also closes its parts; the list of documents would go stale.
            app.CloseAllDocuments(true);
        }
        finally
        {
            app.ExitApp();
            Marshal.ReleaseComObject(app);
            process.Dispose();
        }
    }

    // Diagnostic: opens a workspace assembly read-only and prints, for each named component, its solid
    // bodies with their visibility and volume (m^3), one JSON line per component. Saves nothing.
    public static void DescribeBodies(string assembly, string[] names)
    {
        assembly = Path.GetFullPath(assembly);
        string workspace = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..")) + Path.DirectorySeparatorChar;
        if (!assembly.StartsWith(workspace, StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("The assembly must be inside the workspace.");
        string session = Path.Combine(workspace, "validation", "fixtures", "bodies-" + Guid.NewGuid().ToString("N").Substring(0, 8));
        Directory.CreateDirectory(session);
        Process process;
        var app = StartFixtureSession(session, out process);
        try
        {
            app.SetCurrentWorkingDirectory(Path.GetDirectoryName(assembly));
            int errors = 0, warnings = 0;
            var doc = app.OpenDoc6(assembly, 2, 1 | 2, "", ref errors, ref warnings); // silent, read-only
            if (doc == null) throw new IOException("Cannot open " + assembly + ": " + errors);
            ((AssemblyDoc)doc).ResolveAllLightWeightComponents(false);
            var all = (((AssemblyDoc)doc).GetComponents(false) as object[] ?? new object[0]).Cast<Component2>().ToDictionary(c => c.Name2, StringComparer.OrdinalIgnoreCase);
            var json = new JavaScriptSerializer();
            foreach (string name in names)
            {
                Component2 component;
                if (!all.TryGetValue(name, out component)) { Console.WriteLine(json.Serialize(new { component = name, found = false })); continue; }
                object info;
                var bodies = (component.GetBodies3((int)SolidWorks.Interop.swconst.swBodyType_e.swSolidBody, out info) as object[] ?? new object[0]).Cast<Body2>()
                    .Select(b => { var mass = b.GetMassProperties(1.0) as double[]; return new { name = b.Name, visible = b.Visible, volume_m3 = mass == null ? (double?)null : mass[3] }; }).ToList();
                Console.WriteLine(json.Serialize(new { component = name, found = true, path = component.GetPathName(), configuration = component.ReferencedConfiguration, hidden = component.IsHidden(false), bodies }));
            }
            // Closing an assembly also closes its parts; the list of documents would go stale.
            app.CloseAllDocuments(true);
        }
        finally
        {
            app.ExitApp();
            Marshal.ReleaseComObject(app);
            process.Dispose();
        }
    }

    private static SldWorks StartFixtureSession(string destination, out Process process)
    {
        string swFolder = Microsoft.Win32.Registry.GetValue(@"HKEY_LOCAL_MACHINE\SOFTWARE\SolidWorks\SOLIDWORKS 2026\Setup", "SolidWorks Folder", null) as string;
        var start = new ProcessStartInfo(Path.Combine(swFolder, "SLDWORKS.exe"), "/r") { UseShellExecute = false, WindowStyle = ProcessWindowStyle.Hidden };
        string drive = Path.GetPathRoot(destination);
        start.EnvironmentVariables["HOMEDRIVE"] = drive.TrimEnd('\\');
        start.EnvironmentVariables["HOMEPATH"] = destination.Substring(drive.Length - 1);
        process = Process.Start(start);
        File.WriteAllText(Path.Combine(destination, "session.json"), new JavaScriptSerializer().Serialize(new { pid = process.Id, started = process.StartTime.ToString("o") }));
        SldWorks app = null;
        var watch = Stopwatch.StartNew();
        while (app == null && watch.Elapsed.TotalSeconds < 120)
        {
            try { app = FindSession(process.Id); } catch (COMException) { } catch (InvalidCastException) { }
            if (app == null) Thread.Sleep(500);
        }
        if (app == null || app.GetProcessID() != process.Id) throw new InvalidOperationException("Private fixture session unavailable.");
        app.Visible = false;
        return app;
    }
}

public static class NestedFixtureBuilder
{
    [STAThread]
    public static int Main(string[] args)
    {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        try
        {
            if (args.Length == 4 && args[0] == "virtual") { LegacyExportBridge.CreateVirtualFixture(args[1], args[2], args[3]); return 0; }
            if (args.Length >= 3 && args[0] == "bodies") { LegacyExportBridge.DescribeBodies(args[1], args.Skip(2).ToArray()); return 0; }
            if (args.Length == 4 && args[0] == "wrap") { LegacyExportBridge.CreateWrapperFixture(args[1], args[2], int.Parse(args[3])); return 0; }
            if (args.Length != 2) return 2;
            LegacyExportBridge.CreateNestedFixture(args[0], args[1]);
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
