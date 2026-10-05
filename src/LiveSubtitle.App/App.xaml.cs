using System;
using System.IO;
using System.Windows;
using System.Windows.Threading;
using LiveSubtitle.App.Services;

namespace LiveSubtitle.App;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        UiText.SetLanguage("ko");
        bool diagnostic = e.Args.Length >= 2 && (e.Args[0] == "--smoke-test" || e.Args[0] == "--live-smoke");
        string? smokeOutput = diagnostic ? e.Args[1] : null;
        int? livePid = e.Args.Length >= 3 && e.Args[0] == "--live-smoke" && int.TryParse(e.Args[2], out int pid) ? pid : null;
        if (smokeOutput != null) { Directory.CreateDirectory(smokeOutput); File.WriteAllText(Path.Combine(smokeOutput, "diagnostic-startup.log"), $"{DateTime.UtcNow:O} App startup\n"); }
        DispatcherUnhandledException += (_, args) =>
        {
            if (smokeOutput != null)
            {
                File.WriteAllText(Path.Combine(smokeOutput, "diagnostic-error.txt"), args.Exception.ToString());
                Shutdown(1);
            }
            else MessageBox.Show(UiText.T(args.Exception.Message), UiText.T("LiveSubtitle 오류"), MessageBoxButton.OK, MessageBoxImage.Error);
            args.Handled = true;
        };
        MainWindow = new MainWindow(smokeOutput, livePid);
        if (smokeOutput != null) File.AppendAllText(Path.Combine(smokeOutput, "diagnostic-startup.log"), $"{DateTime.UtcNow:O} MainWindow constructed\n");
        MainWindow.Show();
        if (smokeOutput != null)
        {
            File.AppendAllText(Path.Combine(smokeOutput, "diagnostic-startup.log"), $"{DateTime.UtcNow:O} MainWindow.Show returned\n");
            Dispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, new Action(async () => await ((MainWindow)MainWindow).RunDiagnosticAsync()));
        }
    }
}
