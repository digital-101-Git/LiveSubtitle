using System;
using System.IO;
using System.Runtime.CompilerServices;
using LiveSubtitle.App.Services;

namespace LiveSubtitle.App;

internal static class Program
{
    [STAThread]
    public static int Main(string[] args)
    {
        string? output = args.Length >= 2 && (args[0] == "--smoke-test" || args[0] == "--live-smoke") ? args[1] : null;
        try
        {
            Trace(output, "Managed entry point");
            return RunApplication(output);
        }
        catch (Exception ex)
        {
            if (output != null)
            {
                Directory.CreateDirectory(output);
                File.WriteAllText(Path.Combine(output, "diagnostic-error.txt"), ex.ToString());
            }
            else System.Windows.MessageBox.Show(UiText.T(ex.Message), UiText.T("LiveSubtitle 시작 오류"));
            return 1;
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static int RunApplication(string? output)
    {
        Trace(output, "Before App constructor");
        var app = new App();
        Trace(output, "Before App.InitializeComponent");
        app.InitializeComponent();
        Trace(output, "Before App.Run");
        return app.Run();
    }

    private static void Trace(string? output, string stage)
    {
        if (output == null) return;
        Directory.CreateDirectory(output);
        File.AppendAllText(Path.Combine(output, "diagnostic-entry.log"), $"{DateTime.UtcNow:O} {stage}\n");
    }
}
