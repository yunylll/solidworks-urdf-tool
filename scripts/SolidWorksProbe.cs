using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

// Typed COM calls avoid PowerShell's dependency on dispatch type-info lookup.
public static class SolidWorksProbe
{
    [STAThread]
    public static int Main(string[] args)
    {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        ISldWorks app = null;
        bool owned = false;
        try
        {
            try { app = (ISldWorks)Marshal.GetActiveObject("SldWorks.Application"); }
            catch (COMException e)
            {
                if (e.ErrorCode != unchecked((int)0x800401E3)) throw;
                if (Array.IndexOf(args, "--start") < 0)
                {
                    Console.WriteLine("STATUS=no_running_session");
                    return 2;
                }
                if (Process.GetProcessesByName("SLDWORKS").Length != 0)
                    throw new InvalidOperationException("An inaccessible SolidWorks session is already running.");
                app = (ISldWorks)Activator.CreateInstance(Type.GetTypeFromProgID("SldWorks.Application", true));
                owned = true;
            }
            Console.WriteLine("REVISION=" + app.RevisionNumber());
            Console.WriteLine("STL_QUALITY=" + app.GetUserPreferenceIntegerValue((int)swUserPreferenceIntegerValue_e.swSTLQuality));
            IModelDoc2 model = app.IActiveDoc2;
            if (model == null) Console.WriteLine("ACTIVE_DOCUMENT=none");
            else
            {
                try
                {
                    Console.WriteLine("ACTIVE_DOCUMENT=" + model.GetPathName());
                    Console.WriteLine("DOCUMENT_TYPE=" + model.GetType());
                    Console.WriteLine("UNSAVED_CHANGES=" + model.GetSaveFlag());
                    if (Array.IndexOf(args, "--inspect") >= 0 && model.GetType() == 2)
                    {
                        IAssemblyDoc assembly = (IAssemblyDoc)model;
                        object[] components = assembly.GetComponents(false) as object[];
                        Console.WriteLine("COMPONENT_INSTANCES=" + (components == null ? 0 : components.Length));
                        if (components != null)
                        {
                            var summary = new System.Collections.Generic.List<object>();
                            foreach (object component in components)
                            {
                                IComponent2 item = (IComponent2)component;
                                summary.Add(new { name = item.Name2, path = item.GetPathName(), is_virtual = item.IsVirtual, suppressed = item.IsSuppressed(), exists = System.IO.File.Exists(item.GetPathName()) });
                                if (component != null && Marshal.IsComObject(component)) Marshal.ReleaseComObject(component);
                            }
                            Console.WriteLine("COMPONENT_SUMMARY=" + new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(summary));
                        var extension = model.Extension;
                        var mass = (IMassProperty2)extension.CreateMassProperty2();
                        mass.UseSystemUnits = true;
                        mass.IncludeHiddenBodiesOrComponents = true;
                        var configuration = model.ConfigurationManager.ActiveConfiguration;
                        Console.WriteLine("SOURCE_MANIFEST=" + new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(new {
                            source_path = model.GetPathName(), unsaved_changes = model.GetSaveFlag(),
                            configuration_name = configuration.Name, component_count = summary.Count,
                            mass_kg = mass.Mass, components = summary
                        }));
                        Marshal.ReleaseComObject(mass);
                        Marshal.ReleaseComObject(extension);
                        Marshal.ReleaseComObject(configuration);
                        }
                    }
                }
                finally { Marshal.ReleaseComObject(model); }
            }
            Console.WriteLine("STATUS=connected");
            return 0;
        }
        catch (Exception e)
        {
            Console.WriteLine("STATUS=failed");
            Console.WriteLine("ERROR=" + e.Message);
            Console.WriteLine("HRESULT=0x" + e.HResult.ToString("X8"));
            return 1;
        }
        finally
        {
            if (app != null)
            {
                if (owned)
                {
                    try
                    {
                        object[] documents = app.GetDocuments() as object[];
                        bool empty = documents == null || documents.Length == 0;
                        if (documents != null)
                            foreach (object document in documents)
                                if (document != null && Marshal.IsComObject(document)) Marshal.ReleaseComObject(document);
                        if (empty)
                        {
                            app.ExitApp();
                            Console.WriteLine("CLEANUP=closed_empty_session_started_by_probe");
                        }
                        else Console.WriteLine("CLEANUP=left_open_because_documents_are_present");
                    }
                    catch (Exception e) { Console.WriteLine("CLEANUP_ERROR=" + e.Message); }
                }
                else Console.WriteLine("CLEANUP=existing_session_left_open");
                Marshal.ReleaseComObject(app);
            }
        }
    }
}
