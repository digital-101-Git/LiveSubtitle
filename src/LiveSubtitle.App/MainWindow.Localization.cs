using System;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using LiveSubtitle.App.Services;

namespace LiveSubtitle.App;

public partial class MainWindow
{
    private bool _showRunningStatus, _translationTestPending;
    private string _uiSettingsRoot = AppContext.BaseDirectory;

    private void LoadPresentationSettings()
    {
        if (_smokeOutput != null) return;
        foreach (string candidate in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            for (DirectoryInfo? directory = new(candidate); directory != null; directory = directory.Parent)
            {
                if (!File.Exists(Path.Combine(directory.FullName, "engine", "server.py")) &&
                    !File.Exists(Path.Combine(directory.FullName, "config", "ui-settings.json"))) continue;
                _uiSettingsRoot = directory.FullName;
                _ui = UiSettings.Load(_uiSettingsRoot);
                LoadUiSettings();
                return;
            }
        }
        _ui = UiSettings.Load(_uiSettingsRoot);
        LoadUiSettings();
    }

    private void UiLanguage_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_initialized || UiLanguageBox.SelectedItem is not ComboBoxItem item) return;
        _ui.UiLanguage = item.Tag?.ToString() ?? "ko";
        UiText.SetLanguage(_ui.UiLanguage);
        SaveUiSettings();
    }

    private void UiText_LanguageChanged(object? sender, EventArgs e)
    {
        if (!Dispatcher.CheckAccess()) { Dispatch(RefreshLocalizedUi); return; }
        RefreshLocalizedUi();
    }

    private void RefreshLocalizedUi()
    {
        if (!_initialized) return;
        // Refresh presentation only. Never reapply engine, audio, overlay or model settings.
        UpdateLanguageHint(); UpdateModeAppearance(); UpdateControls();
        foreach (AudioTarget item in AudioSources.Items) item.RefreshDisplayName();
        if (_running) SessionLabel.Text = $"● {SourceLanguageName} → {TargetLanguageName}";
        if (_showRunningStatus) UpdateRunningStatusDetail();
        if (_translationTestPending) LocalizeText(TestResult, "준비된 로컬 모델로 {0} 번역 중…", TargetLanguageName);
        RefreshTrayText();
    }

    private void SetRunningStatus()
    {
        SetStatus("실시간 자막 실행 중", "", "#3BD8AD");
        _showRunningStatus = true;
        UpdateRunningStatusDetail();
    }

    private void UpdateRunningStatusDetail() =>
        StatusDetail.Text = SourceLanguageSummary + " · " + TargetLanguageSummary + " · " +
            UiText.T(GeminiMode.IsChecked == true ? "음성 → Gemini · 번역 → 이 PC" : "음성 인식과 번역이 모두 이 PC에서 실행됩니다.");

    private void RefreshTrayText()
    {
        if (_tray == null) return;
        if (_tray.ContextMenuStrip is { } menu && menu.Items.Count >= 4)
        {
            menu.Items[0].Text = UiText.T("LiveSubtitle 열기");
            menu.Items[1].Text = UiText.T("자막 중지");
            menu.Items[3].Text = UiText.T("완전히 종료");
        }
        string tooltip = "LiveSubtitle · " + StatusText.Text;
        _tray.Text = tooltip[..Math.Min(63, tooltip.Length)];
    }

    private static void LocalizeText(TextBlock control, string source, params object?[] args) =>
        UiText.Bind(control, TextBlock.TextProperty, source, args);

    private static void LocalizeText(TextBox control, string source, params object?[] args) =>
        UiText.Bind(control, TextBox.TextProperty, source, args);
}
