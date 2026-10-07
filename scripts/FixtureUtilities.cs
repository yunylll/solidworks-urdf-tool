using System;
using System.Diagnostics;
using System.IO;
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

public static class NestedFixtureBuilder
{
    [STAThread]
    public static int Main(string[] args)
    {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        if (args.Length != 2) return 2;
        try { LegacyExportBridge.CreateNestedFixture(args[0], args[1]); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
