using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using LiveSubtitle.App.Services;
using Forms = System.Windows.Forms;

namespace LiveSubtitle.App;

public sealed record ModelChoice(string Name, string Path, string Backend = "")
{
    public override string ToString() => Name;
}
public sealed record CaptionRow(string Time, string Translation, string Source, string Id);

public partial class MainWindow : Window
{
    private const string DefaultTranslationTest = "Hello! Thank you for watching today's live stream.";
    private const string JapaneseTranslationTest = "こんにちは！今日の配信をご覧いただきありがとうございます。";
    private readonly ObservableCollection<string> _logs = new();
    private readonly ObservableCollection<CaptionRow> _captions = new();
    private readonly CancellationTokenSource _lifetime = new();
    private readonly string? _smokeOutput;
    private readonly int? _liveSmokePid;
    private bool _diagnosticStarted, _diagnosticFirstCaption;
    private int _diagnosticPcmChunks;
    private EngineClient? _engine;
    private SubtitleSession? _session;
    private AudioCapture? _capture;
    private CancellationTokenSource? _sessionCts;
    private OverlayWindow? _overlay;
    private UiSettings _ui = new();
    private Forms.NotifyIcon? _tray;
    private bool _initialized, _connected, _busy, _running, _stopping, _exiting, _trayNoticeShown;
    private bool _audioSourcesInitialized;
    private bool _clearingLogs;
    private long _epoch;

    public MainWindow(string? smokeOutput = null, int? liveSmokePid = null)
    {
        _smokeOutput = smokeOutput;
        _liveSmokePid = liveSmokePid;
        UiText.SetLanguage(UiText.Language);
        InitializeComponent();
        UiText.LanguageChanged += UiText_LanguageChanged;
        Closed += (_, _) => UiText.LanguageChanged -= UiText_LanguageChanged;
        CaptionList.ItemsSource = _captions; LogList.ItemsSource = _logs;
        LoadPresentationSettings();
        if (smokeOutput == null) SetupTray();
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        _initialized = true;
        RefreshLocalizedUi();
        if (_smokeOutput != null) return;
        RefreshSources();
        await ConnectAsync();
    }

    internal async Task RunDiagnosticAsync()
    {
        if (_diagnosticStarted || _smokeOutput == null) return;
        _diagnosticStarted = true; _initialized = true;
        File.AppendAllText(Path.Combine(_smokeOutput, "diagnostic-startup.log"), $"{DateTime.UtcNow:O} Diagnostic dispatcher started\n");
        if (_liveSmokePid.HasValue) await LiveSmokeTestAsync(_smokeOutput, _liveSmokePid.Value);
        else await SmokeTestAsync(_smokeOutput);
    }

    private async Task ConnectAsync()
    {
        if (_busy || _running || _exiting) return;
        _busy = true; _connected = false; UpdateControls();
        SetStatus("로컬 엔진 연결 중", "이 PC의 전사·번역 엔진을 확인합니다.", "#E6B75C");
        try
        {
            if (_engine == null)
            {
                _engine = new EngineClient(); _engine.Log += AddLog;
                _uiSettingsRoot = _engine.Root;
                _overlay = new OverlayWindow(_ui);
                _overlay.PlacementCommitted += (_, _) => SaveUiSettings();
                ApplyOverlaySettings();
            }
            await _engine.EnsureReadyAsync(_lifetime.Token);
            JsonElement settings = await _engine.GetAsync("v1/settings", _lifetime.Token);
            await RefreshModelsAsync();
            ApplyEngineSettings(settings);
            _connected = true;
            SetStatus("연결됨 · 준비 대기", "모델을 선택하고 ‘실시간 자막 시작’을 누르세요.", "#3BD8AD");
            AddLog("브라우저, 마이크 또는 시스템 전체 소리를 입력으로 선택할 수 있습니다. 번역은 항상 로컬입니다.");
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) { ReportError("엔진 연결 실패", ex); }
        finally { _busy = false; UpdateControls(); }
    }

    private void ApplyEngineSettings(JsonElement settings)
    {
        bool previous = _initialized; _initialized = false;
        string mode = Text(settings, "mode");
        GeminiMode.IsChecked = mode.StartsWith("gemini", StringComparison.OrdinalIgnoreCase);
        LocalMode.IsChecked = GeminiMode.IsChecked != true;
        ApplyLanguageSettings(settings);
        SelectModel(TranslationModels, Text(settings, "translation_model"));
        SelectModel(AsrModels, Text(settings, "asr_model"));
        ApplyAsrOptions(settings);
        bool keySet = settings.TryGetProperty("gemini_key_set", out var key) && key.ValueKind == JsonValueKind.True;
        SaveGeminiKey.IsChecked = settings.TryGetProperty("save_gemini_key", out var saveKey) && saveKey.ValueKind == JsonValueKind.True;
        LocalizeText(KeyStatus, keySet ? "엔진에 API 키가 설정되어 있습니다. 변경할 때만 입력하세요." : "API 키를 입력하세요. 저장하지 않으면 이번 실행에서만 사용합니다.");
        _initialized = previous; UpdateModeAppearance(); UpdateLanguageHint(); UpdateTranslationModelHint();
    }

    private static void SelectModel(ComboBox control, string selected)
    {
        if (string.IsNullOrWhiteSpace(selected)) return;
        foreach (ModelChoice model in control.Items)
            if (model.Path.Equals(selected, StringComparison.OrdinalIgnoreCase) || model.Name.Equals(selected, StringComparison.OrdinalIgnoreCase)) { control.SelectedItem = model; return; }
    }

    private void ApplyLanguageSettings(JsonElement settings)
    {
        SelectLanguage(LanguageBox, Text(settings, "language"));
        // Older settings have no target language; the first choice remains Korean.
        SelectLanguage(TargetLanguageBox, Text(settings, "target_language"));
    }

    private static void SelectLanguage(ComboBox control, string language)
    {
        control.SelectedIndex = 0;
        foreach (ComboBoxItem item in control.Items)
            if ((string?)item.Tag == language) { control.SelectedItem = item; break; }
    }

    private async Task RefreshModelsAsync()
    {
        if (_engine == null) return;
        string previousTranslation = (TranslationModels.SelectedItem as ModelChoice)?.Path ?? "";
        string previousAsr = (AsrModels.SelectedItem as ModelChoice)?.Path ?? "";
        JsonElement result = await _engine.GetAsync("v1/models", _lifetime.Token);
        TranslationModels.ItemsSource = ReadModels(result, "translation");
        AsrModels.ItemsSource = ReadModels(result, "asr");
        if (TranslationModels.Items.Count > 0) TranslationModels.SelectedIndex = 0;
        if (AsrModels.Items.Count > 0) AsrModels.SelectedIndex = 0;
        SelectModel(TranslationModels, previousTranslation); SelectModel(AsrModels, previousAsr);
        AddLog(UiText.F($"모델 목록: 번역 {TranslationModels.Items.Count}개 / 음성 인식 {AsrModels.Items.Count}개"));
    }

    private static List<ModelChoice> ReadModels(JsonElement value, string key)
    {
        var models = new List<ModelChoice>();
        if (value.TryGetProperty(key, out var array) && array.ValueKind == JsonValueKind.Array)
            foreach (var item in array.EnumerateArray())
            {
                string path = Text(item, "path"); string name = Text(item, "name");
                if (path.Length > 0) models.Add(new ModelChoice(name.Length > 0 ? name : Path.GetFileName(path), path, Text(item, "backend")));
            }
        return models;
    }

    private string Mode => GeminiMode.IsChecked == true ? "gemini" : "local";
    private string AsrProfile => (AsrProfileBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "legacy";
    private bool AsrProfileCompatible => AsrProfile != "alignatt" || (AsrModels.SelectedItem as ModelChoice)?.Backend == "whisper";
    private string SourceLanguage => (LanguageBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto";
    private string SourceLanguageName => UiText.T(SourceLanguage switch { "ko" => "한국어", "en" => "영어", "zh" => "중국어", "ja" => "일본어", _ => "자동 감지" });
    private string SourceLanguageSummary => UiText.T(SourceLanguage == "auto" ? "입력 언어: {0}" : "입력 언어: {0} (지정)", SourceLanguageName);
    private string TargetLanguage => (TargetLanguageBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "ko";
    private string TargetLanguageName => UiText.T(TargetLanguage switch { "en" => "영어", "zh" => "중국어", "ja" => "일본어", _ => "한국어" });
    private string TargetLanguageSummary => UiText.T("번역 언어: {0}", TargetLanguageName);

    private void LanguageBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_initialized) UpdateLanguageHint();
    }

    private void TargetLanguageBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_initialized) UpdateModeAppearance();
    }

    private void TranslationModel_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_initialized) UpdateTranslationModelHint();
    }

    private void UpdateTranslationModelHint()
    {
        if (TestInput == null || ModelPreparationHint == null || TranslationModels == null) return;
        string name = Path.GetFileName(((TranslationModels.SelectedItem as ModelChoice)?.Path ?? "").Replace('\\', '/'));
        bool japaneseOnly = System.Text.RegularExpressions.Regex.IsMatch(name,
            @"(?:^|[^a-z0-9])ja[-_]ko[-_]vn[-_]12b[-_]v2(?:$|[^a-z0-9])",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.CultureInvariant);
        // Replace only the stock examples; never overwrite a user's custom text.
        if (japaneseOnly && TestInput.Text == DefaultTranslationTest) TestInput.Text = JapaneseTranslationTest;
        else if (!japaneseOnly && TestInput.Text == JapaneseTranslationTest) TestInput.Text = DefaultTranslationTest;
        LocalizeText(ModelPreparationHint, japaneseOnly
            ? "모델 준비에는 시간이 걸릴 수 있습니다. 시작 전에 모델을 메모리에 불러옵니다.\n일본어→한국어 전용입니다. 입력: 일본어/자동 · 번역: 한국어"
            : "모델 준비에는 시간이 걸릴 수 있습니다. 시작 전에 모델을 메모리에 불러옵니다.\n번역 결과만 출력합니다. Qwen 모델은 사고 모드를 자동으로 해제합니다.");
    }

    private void UpdateLanguageHint()
    {
        if (LanguageHint == null) return;
        if (SourceLanguage == "auto") LocalizeText(LanguageHint, "지정하지 않으면 기존처럼 입력 언어를 자동 감지합니다.");
        else LocalizeText(LanguageHint, "{0}를 입력 언어로 사용합니다. 변경하려면 자막을 중지한 뒤 다시 시작하세요.", SourceLanguageName);
    }

    private async Task SaveEngineSettingsAsync(CancellationToken ct)
    {
        if (_engine == null) throw new InvalidOperationException("로컬 엔진이 연결되지 않았습니다.");
        ValidateAsrProfile();
        var settings = new Dictionary<string, object> { ["mode"] = Mode, ["language"] = SourceLanguage, ["target_language"] = TargetLanguage,
            ["asr_profile"] = AsrProfile, ["asr_hints"] = ReadAsrHints(), ["qwen_boundary_recheck"] = BoundaryRecheckBox.IsChecked == true };
        if (TranslationModels.SelectedItem is ModelChoice translation) settings["translation_model"] = translation.Path;
        if (AsrModels.SelectedItem is ModelChoice asr) settings["asr_model"] = asr.Path;
        if (!string.IsNullOrWhiteSpace(GeminiKey.Password))
        {
            settings["gemini_api_key"] = GeminiKey.Password.Trim();
            settings["save_gemini_key"] = SaveGeminiKey.IsChecked == true;
        }
        JsonElement saved = await _engine.PutAsync("v1/settings", settings, ct);
        ApplyAsrOptions(saved);
        if (settings.ContainsKey("gemini_api_key")) { GeminiKey.Clear(); LocalizeText(KeyStatus, "API 키를 로컬 엔진에 전달했습니다."); }
    }

    private string[] ReadAsrHints() => AsrHintsBox.Text.Split(new[] { '\r', '\n' },
        StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    private void ApplyAsrOptions(JsonElement settings)
    {
        string profile = Text(settings, "asr_profile");
        AsrProfileBox.SelectedIndex = 0;
        foreach (ComboBoxItem item in AsrProfileBox.Items)
            if ((string?)item.Tag == profile) { AsrProfileBox.SelectedItem = item; break; }
        AsrHintsBox.Text = settings.TryGetProperty("asr_hints", out var hints) && hints.ValueKind == JsonValueKind.Array
            ? string.Join(Environment.NewLine, hints.EnumerateArray().Where(item => item.ValueKind == JsonValueKind.String).Select(item => item.GetString())) : "";
        BoundaryRecheckBox.IsChecked = settings.TryGetProperty("qwen_boundary_recheck", out var recheck) && recheck.ValueKind == JsonValueKind.True;
    }

    private async void SaveAsrOptions_Click(object sender, RoutedEventArgs e)
    {
        if (_busy || _running || _exiting || !_connected || _engine == null) return;
        _busy = true; UpdateControls();
        try
        {
            ValidateAsrProfile();
            var options = new Dictionary<string, object> {
                ["asr_profile"] = AsrProfile, ["asr_hints"] = ReadAsrHints(),
                ["qwen_boundary_recheck"] = BoundaryRecheckBox.IsChecked == true
            };
            // Keep the selected model and its compatible profile together;
            // leave mode, language, translation model and API credentials alone.
            if (AsrModels.SelectedItem is ModelChoice asr) options["asr_model"] = asr.Path;
            JsonElement saved = await _engine.PutAsync("v1/settings", options, _lifetime.Token);
            ApplyAsrOptions(saved);
            SetStatus("음성 인식 옵션 저장됨", "다음 로컬 음성 인식 시작부터 적용됩니다.", "#3BD8AD");
            AddLog(UiText.F($"음성 인식 옵션 저장 · {(AsrProfileBox.SelectedItem as ComboBoxItem)?.Content} · 힌트 {ReadAsrHints().Length}개"));
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) { ReportError("음성 인식 옵션 저장 실패", ex); }
        finally { _busy = false; UpdateControls(); }
    }

    private void AsrModel_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_initialized) UpdateQwenControls();
    }

    private void AsrProfile_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_initialized) UpdateQwenControls();
    }

    private void ValidateAsrProfile()
    {
        if (LocalMode.IsChecked == true && !AsrProfileCompatible)
            throw new InvalidOperationException("Whisper AlignAtt는 Whisper 모델 전용입니다. Qwen에서는 ‘기본’ 또는 ‘반복확인(느림)’을 선택하세요.");
    }

    private void UpdateQwenControls()
    {
        bool configurable = !_busy && !_running && !_exiting && LocalMode.IsChecked == true;
        bool qwen = (AsrModels.SelectedItem as ModelChoice)?.Backend == "qwen3_asr";
        bool whisper = (AsrModels.SelectedItem as ModelChoice)?.Backend == "whisper";
        AsrProfileBox.IsEnabled = configurable;
        AlignAttProfileItem.IsEnabled = whisper;
        LocalizeText(AsrProfileHint, !AsrProfileCompatible
            ? "Whisper AlignAtt는 Whisper 전용입니다. 시작 전에 ‘기본’ 또는 ‘반복확인(느림)’을 선택하세요."
            : AsrProfile switch {
                "legacy" => "기본 인식·표시 방식을 사용합니다. 문장 경계 재확인을 켜면 추가 대기가 생길 수 있습니다.",
                "alignatt" => "실험 방식입니다. 음성과 텍스트의 정렬로 확정하며, 영상에 따라 인식 정확도가 낮아질 수 있습니다.",
                _ => "여러 차례의 인식 결과를 비교한 뒤 표시합니다. 기본보다 늦어질 수 있으며, 인식 오류가 남을 수 있습니다."
            });
        QwenOptionsPanel.IsEnabled = configurable && qwen;
        BoundaryRecheckBox.IsEnabled = configurable && qwen && AsrProfile == "legacy";
        SaveAsrOptionsButton.IsEnabled = configurable && _connected && AsrProfileCompatible;
    }

    private void ValidateModels()
    {
        if (TranslationModels.SelectedItem == null) throw new InvalidOperationException("번역 GGUF 모델을 먼저 가져오세요.");
        if (LocalMode.IsChecked == true && AsrModels.SelectedItem == null) throw new InvalidOperationException("로컬 음성 인식 모델을 선택하세요. 목록이 비어 있으면 models/asr 폴더에 지원 모델을 넣고 다시 연결하세요.");
        ValidateAsrProfile();
    }

    private async void Start_Click(object sender, RoutedEventArgs e)
    {
        if (_busy || _running || !_connected || _engine == null) return;
        long epoch = ++_epoch;
        _sessionCts = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
        var ct = _sessionCts.Token;
        _busy = true; UpdateControls();
        try
        {
            ValidateModels();
            if (AudioSources.SelectedItem is not AudioTarget target) throw new InvalidOperationException("브라우저, 마이크 또는 시스템 전체 소리 중 오디오 소스를 선택하세요.");
            if (target.ProcessId.HasValue && !WindowTargets.Exists(target.WindowHandle)) throw new InvalidOperationException("선택한 브라우저 창이 닫혔습니다. 창 목록을 새로고침하세요.");
            SetStatus("모델 준비 중", "모델을 불러오는 동안 잠시 기다려 주세요. 중지를 누르면 시작을 취소합니다.", "#E6B75C");
            if (!_liveSmokePid.HasValue)
            {
                await SaveEngineSettingsAsync(ct);
                await _engine.PostAsync("v1/prepare", new { }, ct);
            }
            ct.ThrowIfCancellationRequested();
            if (epoch != _epoch) return;
            var session = new SubtitleSession(); _session = session;
            session.Log += message => { if (epoch == Interlocked.Read(ref _epoch)) AddLog(message); };
            session.Error += message => Dispatch(() => { if (epoch == _epoch) _ = HandleSessionFailureAsync(message); });
            session.Event += message => Dispatch(() => { if (epoch == _epoch) HandleStreamEvent(message); });
            SetStatus("오디오 연결 중", "전사 세션이 준비되면 선택한 오디오 소스의 캡처를 시작합니다.", "#E6B75C");
            await session.StartAsync(_engine.Token, Mode, SourceLanguage, ct, targetLanguage: TargetLanguage);
            ct.ThrowIfCancellationRequested();
            if (epoch != _epoch) return;
            var capture = new AudioCapture(); _capture = capture;
            capture.PcmAvailable += pcm =>
            {
                if (epoch != Interlocked.Read(ref _epoch)) return;
                if (_liveSmokePid.HasValue) Interlocked.Increment(ref _diagnosticPcmChunks);
                session.QueueAudio(pcm);
            };
            capture.Error += message => Dispatch(() => { if (epoch == _epoch) _ = HandleSessionFailureAsync(message); });
            await Task.Run(() => capture.Start(target.ProcessId, target.CaptureDeviceId), ct);
            if (ct.IsCancellationRequested || epoch != _epoch) { await Task.Run(capture.Stop); return; }
            _running = true;
            SessionLabel.Text = $"● {SourceLanguageName} → {TargetLanguageName}";
            LocalizeText(PartialText, target.IsMicrophone ? "마이크 입력을 듣고 있습니다…" : "방송 음성을 듣고 있습니다…");
            SetRunningStatus();
            AddLog(UiText.T("자막 시작 · {0} · {1} · {2}", target.DisplayName, SourceLanguageSummary, TargetLanguageSummary));
        }
        catch (OperationCanceledException) { AddLog("자막 시작이 취소되었습니다."); }
        catch (Exception ex) { if (epoch == _epoch) { ReportError("자막 시작 실패", ex); await StopSessionAsync(preserveStatus: true); } }
        finally { if (epoch == _epoch) { _busy = false; UpdateControls(); } }
    }

    private async Task HandleSessionFailureAsync(string message)
    {
        if (_stopping || _exiting) return;
        AddLog(UiText.T("오류 · {0}", UiText.T(message))); SetStatus("자막 연결이 중단되었습니다", message, "#F39090");
        await StopSessionAsync(preserveStatus: true);
    }

    private void HandleStreamEvent(JsonElement message)
    {
        string type = Text(message, "type");
        string sessionId = Text(message, "session_id");
        if (sessionId.Length > 0 && _session?.SessionId is { Length: > 0 } current && sessionId != current) return;
        switch (type)
        {
            case "status":
                string status = Text(message, "message");
                if (status.Length > 0) { _showRunningStatus = false; LocalizeText(StatusDetail, status); AddLog(status); }
                break;
            case "warning":
                string warning = Text(message, "message");
                if (warning.Length > 0) AddLog(UiText.T("번역 경고 · {0}", UiText.T(warning)));
                break;
            case "transcript":
                string transcript = Text(message, "text");
                bool preview = !message.TryGetProperty("preview", out var previewFlag) || previewFlag.ValueKind != JsonValueKind.False;
                if (preview && transcript.Length > 0) PartialText.Text = transcript;
                break;
            case "caption":
                string translation = Text(message, "text"); string source = Text(message, "source_text");
                if (Text(message, "translation_status") == "failed") translation = UiText.T("[번역하지 못했습니다]");
                if (translation.Length == 0) break;
                string id = Text(message, "segment_id");
                string combined = sessionId + ":" + id;
                var caption = new CaptionRow(DateTime.Now.ToString("HH:mm:ss"), translation, source, combined);
                int existing = id.Length > 0 ? _captions.ToList().FindIndex(c => c.Id == combined) : -1;
                if (existing >= 0) _captions[existing] = caption;
                else _captions.Insert(0, caption);
                while (_captions.Count > 60) _captions.RemoveAt(_captions.Count - 1);
                try { _overlay?.SetCaption(translation, source, id.Length > 0 ? combined : null); }
                catch (CaptionQueueOverflowException ex)
                {
                    _ = HandleSessionFailureAsync(ex.Message);
                    return;
                }
                if (_liveSmokePid.HasValue && !_diagnosticFirstCaption && _smokeOutput != null)
                {
                    _diagnosticFirstCaption = true;
                    Dispatch(() =>
                    {
                        UpdateLayout(); _overlay?.UpdateLayout();
                        SaveVisual(this, Path.Combine(_smokeOutput, "live-first-caption-window.png"));
                        if (_overlay != null) SaveVisual(_overlay, Path.Combine(_smokeOutput, "live-first-caption-overlay.png"));
                        if (_overlay != null) File.WriteAllText(Path.Combine(_smokeOutput, "live-first-overlay-state.json"), JsonSerializer.Serialize(_overlay.DiagnosticState(), new JsonSerializerOptions { WriteIndented = true }));
                    });
                }
                break;
            case "caption_remove":
                string removedId = Text(message, "segment_id");
                if (removedId.Length > 0)
                {
                    string removedKey = sessionId + ":" + removedId;
                    int removedIndex = _captions.ToList().FindIndex(c => c.Id == removedKey);
                    if (removedIndex >= 0) _captions.RemoveAt(removedIndex);
                    _overlay?.RemoveCaption(removedKey);
                }
                break;
            case "stopped":
                if (!_stopping) _ = StopSessionAsync();
                break;
        }
    }

    private async Task StopSessionAsync(bool preserveStatus = false)
    {
        if (_stopping) return;
        _stopping = true; Interlocked.Increment(ref _epoch);
        _sessionCts?.Cancel();
        var capture = _capture; _capture = null;
        var session = _session; _session = null;
        _running = false; _busy = true; UpdateControls();
        _overlay?.Clear();
        try
        {
            if (capture != null)
            {
                try { await Task.Run(() => { capture.Stop(); capture.Dispose(); }); }
                catch (Exception ex) { AddLog(UiText.T("오디오 중지: {0}", ex.Message)); }
            }
            if (session != null) await session.DisposeAsync();
            LocalizeText(SessionLabel, "대기 중");
            LocalizeText(PartialText, "자막이 중지되었습니다. 최근 자막은 이 창에서 확인할 수 있습니다.");
            if (!preserveStatus && !_exiting) SetStatus("자막 중지됨", "모델과 방송을 선택해 다시 시작할 수 있습니다.", "#91A6C2");
            AddLog("오디오 캡처와 자막 세션을 중지했습니다.");
        }
        catch (Exception ex) { AddLog(UiText.T("중지 처리: {0}", ex.Message)); }
        finally { _sessionCts?.Dispose(); _sessionCts = null; _busy = false; _stopping = false; UpdateControls(); }
    }

    private async void Prepare_Click(object sender, RoutedEventArgs e)
    {
        if (_busy || _running || _engine == null) return;
        long epoch = ++_epoch;
        _sessionCts = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
        CancellationToken ct = _sessionCts.Token;
        _busy = true; UpdateControls();
        SetStatus("모델 준비 중", "전사·번역 모델을 메모리에 불러옵니다. 첫 실행은 오래 걸릴 수 있습니다.", "#E6B75C");
        try
        {
            ValidateModels(); await SaveEngineSettingsAsync(ct);
            await _engine.PostAsync("v1/prepare", new { }, ct);
            ct.ThrowIfCancellationRequested();
            if (epoch != _epoch) return;
            SetStatus("모델 준비 완료", "오디오 소스를 선택한 뒤 실시간 자막을 시작하세요.", "#3BD8AD"); AddLog("모델 준비가 완료되었습니다.");
        }
        catch (OperationCanceledException) { AddLog("모델 준비 요청 대기를 취소했습니다. 이미 시작된 엔진의 모델 로딩은 완료될 수 있습니다."); }
        catch (Exception ex) { if (epoch == _epoch) ReportError("모델 준비 실패", ex); }
        finally { if (epoch == _epoch) { _sessionCts?.Dispose(); _sessionCts = null; _busy = false; UpdateControls(); } }
    }

    private async void ImportModel_Click(object sender, RoutedEventArgs e)
    {
        if (_busy || _running || _engine == null) return;
        var picker = new Microsoft.Win32.OpenFileDialog { Title = UiText.T("번역 모델 추가(GGUF)"), Filter = UiText.T("GGUF 모델 (*.gguf)|*.gguf"), CheckFileExists = true, Multiselect = false };
        if (picker.ShowDialog(this) != true) return;
        _busy = true; UpdateControls(); SetStatus("번역 모델 추가 중", Path.GetFileName(picker.FileName), "#E6B75C", localizeDetail: false);
        try
        {
            JsonElement result = await _engine.PostAsync("v1/models/import", new { path = picker.FileName }, _lifetime.Token);
            await RefreshModelsAsync();
            string imported = Text(result, "path");
            if (imported.Length == 0 && result.TryGetProperty("model", out var nested)) imported = Text(nested, "path");
            if (imported.Length > 0) SelectModel(TranslationModels, imported);
            else foreach (ModelChoice model in TranslationModels.Items) if (Path.GetFileName(model.Path).Equals(Path.GetFileName(picker.FileName), StringComparison.OrdinalIgnoreCase)) { TranslationModels.SelectedItem = model; break; }
            SetStatus("번역 모델 추가 완료", "‘모델 준비’에서 선택한 모델을 불러올 수 있습니다.", "#3BD8AD");
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) { ReportError("번역 모델 추가 실패", ex); }
        finally { _busy = false; UpdateControls(); }
    }

    private async void Test_Click(object sender, RoutedEventArgs e)
    {
        if (_busy || _running || _engine == null || string.IsNullOrWhiteSpace(TestInput.Text)) return;
        long epoch = ++_epoch;
        _sessionCts = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
        CancellationToken ct = _sessionCts.Token;
        _busy = true; UpdateControls(); LocalizeText(TestResult, "선택한 모델을 준비하는 중… 중지 버튼으로 취소할 수 있습니다.");
        try
        {
            if (TranslationModels.SelectedItem == null) throw new InvalidOperationException("번역 GGUF 모델을 먼저 가져오세요.");
            await SaveEngineSettingsAsync(ct);
            await _engine.PostAsync("v1/prepare", new { }, ct);
            ct.ThrowIfCancellationRequested();
            if (epoch != _epoch) return;
            _translationTestPending = true;
            LocalizeText(TestResult, "준비된 로컬 모델로 {0} 번역 중…", TargetLanguageName);
            JsonElement result = await _engine.PostAsync("v1/translate", new { text = TestInput.Text.Trim(), source_language = SourceLanguage, target_language = TargetLanguage }, ct);
            ct.ThrowIfCancellationRequested();
            if (epoch != _epoch) return;
            _translationTestPending = false;
            TestResult.Text = Text(result, "translation");
            if (TestResult.Text.Length == 0) throw new InvalidOperationException("엔진에서 번역 결과를 받지 못했습니다.");
            _overlay?.SetCaption(TestResult.Text, TestInput.Text.Trim()); AddLog(UiText.T("수동 번역 테스트 완료 · {0}", TargetLanguageSummary));
        }
        catch (OperationCanceledException) { LocalizeText(TestResult, "번역 테스트를 취소했습니다."); AddLog("번역 테스트 대기를 취소했습니다. 이미 시작된 엔진의 모델 로딩은 완료될 수 있습니다."); }
        catch (Exception ex) { if (epoch == _epoch) { LocalizeText(TestResult, ex.Message); AddLog(UiText.T("번역 테스트 실패: {0}", UiText.T(ex.Message))); } }
        finally { _translationTestPending = false; if (epoch == _epoch) { _sessionCts?.Dispose(); _sessionCts = null; _busy = false; UpdateControls(); } }
    }

    private void RefreshSources()
    {
        var previous = AudioSources.SelectedItem as AudioTarget;
        var sources = WindowTargets.Enumerate(); AudioSources.ItemsSource = sources;
        AudioSources.SelectedItem = FindAudioSelection(sources, previous, selectDefault: !_audioSourcesInitialized);
        _audioSourcesInitialized = true;
        if (AudioSources.SelectedItem == null) LocalizeText(AudioSourceHint, "오디오 소스를 선택하세요. 브라우저나 마이크가 목록에 없다면 연결한 뒤 새로고침하세요.");
    }

    private static bool SameAudioTarget(AudioTarget first, AudioTarget second) =>
        first.WindowHandle == second.WindowHandle && first.ProcessId == second.ProcessId &&
        string.Equals(first.CaptureDeviceId, second.CaptureDeviceId, StringComparison.Ordinal);

    private static AudioTarget? FindAudioSelection(IEnumerable<AudioTarget> sources, AudioTarget? previous, bool selectDefault = true) =>
        previous != null
            ? sources.FirstOrDefault(item => SameAudioTarget(item, previous))
            : !selectDefault ? null
                : sources.FirstOrDefault(item => item.ProcessId.HasValue) ??
                  sources.FirstOrDefault(item => item.ProcessId == null && !item.IsMicrophone);

    private void AudioSources_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_initialized || AudioSourceHint == null) return;
        LocalizeText(AudioSourceHint, AudioSources.SelectedItem switch
        {
            AudioTarget { IsMicrophone: true } => "선택한 마이크의 소리를 인식합니다. 자막은 모니터 위에 표시됩니다.",
            AudioTarget { ProcessId: not null } => "선택한 브라우저 프로세스의 소리를 캡처합니다. 같은 브라우저의 다른 탭 소리도 포함될 수 있습니다.",
            AudioTarget => "시스템 전체 소리를 선택하면 알림과 다른 앱 소리도 함께 처리됩니다.",
            _ => "Chrome·Edge 창, 마이크 또는 시스템 전체 소리를 선택하세요."
        });
        ApplyOverlaySettings(); UpdateControls();
    }

    private void Mode_Changed(object sender, RoutedEventArgs e)
    {
        if (!_initialized) return;
        UpdateModeAppearance();
    }

    private void UpdateModeAppearance()
    {
        bool cloud = GeminiMode.IsChecked == true;
        GeminiPanel.Visibility = cloud ? Visibility.Visible : Visibility.Collapsed;
        PrivacyBanner.Background = Brush(cloud ? "#3B3022" : "#162C2A");
        PrivacyText.Foreground = Brush(cloud ? "#F3D299" : "#8CE4C2");
        LocalizeText(TranslationBadge, "{0} 번역 · 항상 로컬", TargetLanguageName);
        if (cloud) LocalizeText(PrivacyText, "입력 음성이 Google로 전송됩니다. {0} 번역은 이 PC에서 처리됩니다.", TargetLanguageName);
        else LocalizeText(PrivacyText, "음성과 텍스트가 이 PC에서 처리됩니다.");
        AsrModels.IsEnabled = !cloud && !_busy && !_running;
        UpdateQwenControls();
    }

    private void LoadUiSettings()
    {
        bool previous = _initialized; _initialized = false;
        SelectLanguage(UiLanguageBox, _ui.UiLanguage);
        UiText.SetLanguage((UiLanguageBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "ko");
        _ui.UiLanguage = UiText.Language;
        ShowOverlayBox.IsChecked = _ui.ShowOverlay; BilingualBox.IsChecked = _ui.Bilingual; FollowWindowBox.IsChecked = _ui.FollowWindow;
        FontSizeSlider.Value = Math.Clamp(_ui.FontSize, 18, 52); OpacitySlider.Value = Math.Clamp(_ui.BackgroundOpacity * 100, 0, 95);
        _initialized = previous; RefreshLocalizedUi();
    }

    private void ApplyOverlaySettings()
    {
        if (!_initialized || FontSizeSlider == null || OpacitySlider == null) return;
        _ui.ShowOverlay = ShowOverlayBox.IsChecked == true; _ui.Bilingual = BilingualBox.IsChecked == true;
        _ui.FollowWindow = FollowWindowBox.IsChecked == true; _ui.FontSize = FontSizeSlider.Value; _ui.BackgroundOpacity = OpacitySlider.Value / 100;
        _overlay?.Apply(_ui, EditOverlayBox.IsChecked == true, (AudioSources.SelectedItem as AudioTarget)?.WindowHandle ?? IntPtr.Zero);
        if (_overlay != null) SaveUiSettings();
    }

    private void SaveUiSettings()
    {
        if (_smokeOutput != null) return;
        try { _ui.Save(_engine?.Root ?? _uiSettingsRoot); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { AddLog(UiText.T("자막 위치·설정 저장 실패: {0}", ex.Message)); }
    }

    private void OverlaySettings_Changed(object sender, RoutedEventArgs e) => ApplyOverlaySettings();
    private void PlaceOnMonitor_Click(object sender, RoutedEventArgs e)
    {
        if (_overlay == null) return;
        _overlay.PlaceOnMonitor();
        AddLog("자막 기본 위치·크기 복원: 모니터 가로 중앙 · 하단 95%");
    }
    private void PlaceInBrowser_Click(object sender, RoutedEventArgs e)
    {
        if (_overlay == null) return;
        _overlay.PlaceInBrowser();
        AddLog("자막 기본 위치·크기 복원: 브라우저 하단 중앙 · 브라우저를 표시할 수 없으면 모니터");
    }
    private void OverlaySlider_Changed(object sender, RoutedPropertyChangedEventArgs<double> e) => ApplyOverlaySettings();
    private void RefreshSources_Click(object sender, RoutedEventArgs e) { if (!_busy && !_running) RefreshSources(); }
    private async void Reconnect_Click(object sender, RoutedEventArgs e) => await ConnectAsync();
    private async void Stop_Click(object sender, RoutedEventArgs e) => await StopSessionAsync();

    private async void ClearLogs_Click(object sender, RoutedEventArgs e)
    {
        if (!_initialized || !_connected || _engine == null || _clearingLogs || _exiting) return;
        _clearingLogs = true; UiText.Bind(ClearLogsButton, ContentControl.ContentProperty, "삭제 중…"); UpdateControls();
        try
        {
            JsonElement response = await _engine.PostAsync("v1/logs/clear", new { }, _lifetime.Token);
            if (_exiting) return;
            var result = ReadLogClearResult(response);
            if (result.Complete)
            {
                _logs.Clear(); _captions.Clear(); _overlay?.Clear();
                LocalizeText(PartialText, _running ? "다음 음성을 기다리고 있습니다…"
                    : "번역 기록이 없습니다. 자막을 시작하면 원문과 번역이 여기에 표시됩니다.");
                TestResult.Text = "";
                AddLog(UiText.T("번역 기록 삭제 완료 · 로그 파일 {0}개 삭제", result.DeletedCount) +
                    (_running ? UiText.T(" · 실시간 자막은 계속 실행됩니다.") : ""));
            }
            else
            {
                string failed = result.FailedFiles.Length > 0
                    ? UiText.T("삭제하지 못한 파일 {0}개: {1}", result.FailedFiles.Length, string.Join(", ", result.FailedFiles))
                    : UiText.T("전체 삭제 완료를 확인하지 못했습니다.");
                AddLog(UiText.T("번역 기록 삭제 미완료 · 로그 파일 {0}개 삭제 · {1}", result.DeletedCount, failed));
            }
        }
        catch (OperationCanceledException) { if (!_exiting) AddLog("번역 기록 삭제 요청이 취소되었습니다. 삭제 결과를 확인하지 못했습니다."); }
        catch (Exception ex) { if (!_exiting) AddLog(UiText.T("번역 기록 삭제 실패 · {0}", ex.Message)); }
        finally { _clearingLogs = false; UiText.Bind(ClearLogsButton, ContentControl.ContentProperty, "번역 기록 삭제"); UpdateControls(); }
    }

    internal static (bool Complete, int DeletedCount, string[] FailedFiles) ReadLogClearResult(JsonElement response)
    {
        if (response.ValueKind != JsonValueKind.Object ||
            !response.TryGetProperty("ok", out var ok) || ok.ValueKind is not (JsonValueKind.True or JsonValueKind.False) ||
            !response.TryGetProperty("deleted_count", out var count) || !count.TryGetInt32(out int deletedCount) || deletedCount < 0 ||
            !response.TryGetProperty("failed_files", out var failed) || failed.ValueKind != JsonValueKind.Array ||
            failed.EnumerateArray().Any(item => item.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(item.GetString())))
            throw new InvalidOperationException("엔진의 기록 삭제 결과를 확인할 수 없습니다.");
        string[] files = failed.EnumerateArray().Select(item => item.GetString()!).ToArray();
        // A partial failure must never be presented as a complete deletion,
        // even if a server version reports ok=true alongside failed files.
        return (ok.ValueKind == JsonValueKind.True && files.Length == 0, deletedCount, files);
    }

    private void UpdateControls()
    {
        if (!_initialized) return;
        bool configurable = !_busy && !_running && !_exiting;
        StartButton.IsEnabled = configurable && _connected && AudioSources.SelectedItem != null;
        StopButton.IsEnabled = !_exiting && !_stopping && (_running || _sessionCts != null);
        ReconnectButton.IsEnabled = configurable; PrepareButton.IsEnabled = configurable && _connected;
        ClearLogsButton.IsEnabled = _connected && _engine != null && !_clearingLogs && !_exiting;
        ImportButton.IsEnabled = configurable && _connected; TestButton.IsEnabled = configurable && _connected;
        AudioSources.IsEnabled = configurable; LocalMode.IsEnabled = GeminiMode.IsEnabled = configurable;
        LanguageBox.IsEnabled = TargetLanguageBox.IsEnabled = TranslationModels.IsEnabled = configurable;
        AsrModels.IsEnabled = configurable && LocalMode.IsChecked == true;
        GeminiKey.IsEnabled = SaveGeminiKey.IsEnabled = configurable;
        UiLanguageBox.IsEnabled = !_exiting;
        UpdateQwenControls();
        BrowserPlacementButton.IsEnabled = MonitorPlacementButton.IsEnabled = _overlay != null && !_exiting;
    }

    private void SetStatus(string status, string detail, string color, bool localizeDetail = true)
    {
        _showRunningStatus = false;
        LocalizeText(StatusText, status);
        if (localizeDetail) LocalizeText(StatusDetail, detail); else StatusDetail.Text = detail;
        StatusDot.Fill = Brush(color);
        RefreshTrayText();
    }

    private void ReportError(string title, Exception ex) { SetStatus(title, ex.Message, "#F39090"); AddLog(UiText.T(title) + " · " + UiText.T(ex.Message)); }
    private static SolidColorBrush Brush(string color) => (SolidColorBrush)new BrushConverter().ConvertFromString(color)!;
    private static string Text(JsonElement obj, string key) => obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty(key, out var value) ? value.ToString() : "";
    private void Dispatch(Action action) { if (!Dispatcher.HasShutdownStarted) Dispatcher.BeginInvoke(action); }
    private void AddLog(string message)
    {
        Dispatch(() =>
        {
            if (_exiting) return;
            _logs.Add($"{DateTime.Now:HH:mm:ss}  {UiText.T(message)}");
            while (_logs.Count > 160) _logs.RemoveAt(0);
            if (_logs.Count > 0) LogList.ScrollIntoView(_logs[^1]);
        });
    }

    private void SetupTray()
    {
        _tray = new Forms.NotifyIcon { Icon = System.Drawing.SystemIcons.Information, Text = "LiveSubtitle", Visible = true };
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("LiveSubtitle 열기", null, (_, _) => Dispatch(ShowFromTray));
        menu.Items.Add("자막 중지", null, (_, _) => Dispatch(() => _ = StopSessionAsync()));
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("완전히 종료", null, (_, _) => Dispatch(() => _ = QuitAsync()));
        _tray.ContextMenuStrip = menu;
        RefreshTrayText();
        _tray.DoubleClick += (_, _) => Dispatch(ShowFromTray);
    }

    private void ShowFromTray() { Show(); WindowState = WindowState.Normal; Activate(); }
    private void Window_StateChanged(object? sender, EventArgs e) { if (WindowState == WindowState.Minimized && _tray != null) HideToTray(); }
    private void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (_exiting) return;
        e.Cancel = true; HideToTray();
    }
    private void HideToTray()
    {
        Hide();
        if (_tray != null && !_trayNoticeShown)
        {
            _trayNoticeShown = true;
            _tray.ShowBalloonTip(2500, "LiveSubtitle", UiText.T("트레이에서 계속 실행됩니다. 트레이 아이콘을 더블클릭하면 다시 열립니다."), Forms.ToolTipIcon.Info);
        }
    }
    private async void Quit_Click(object sender, RoutedEventArgs e) => await QuitAsync();
    private async Task QuitAsync()
    {
        if (_exiting) return;
        _exiting = true; _lifetime.Cancel(); UpdateControls();
        await StopSessionAsync();
        try { SaveUiSettings(); } catch (Exception) { }
        _overlay?.Close(); _tray?.Dispose(); _engine?.Dispose();
        System.Windows.Application.Current.Shutdown();
    }

    internal void VerifyLanguageSelections()
    {
        // Offline assertions only: no engine, audio, API or screen capture.
        int previousSource = LanguageBox.SelectedIndex, previousTarget = TargetLanguageBox.SelectedIndex;
        bool previousBusy = _busy, previousRunning = _running, previousInitialized = _initialized;
        try
        {
            _initialized = true;
            string[] sources = { "auto", "en", "zh", "ja", "ko" };
            string[] targets = { "ko", "en", "zh", "ja" };
            if (!LanguageBox.Items.Cast<ComboBoxItem>().Select(item => item.Tag?.ToString()).SequenceEqual(sources) ||
                !TargetLanguageBox.Items.Cast<ComboBoxItem>().Select(item => item.Tag?.ToString()).SequenceEqual(targets))
                throw new InvalidOperationException("언어 선택 목록 또는 기존 입력 언어 인덱스가 잘못되었습니다.");
            if (SourceLanguage != "auto" || TargetLanguage != "ko")
                throw new InvalidOperationException("초기 언어는 자동 감지 → 한국어여야 합니다.");
            foreach (string source in sources)
            foreach (string target in targets)
            {
                ApplyLanguageSettings(JsonSerializer.SerializeToElement(new { language = source, target_language = target }));
                UpdateLanguageHint(); UpdateModeAppearance();
                if (SourceLanguage != source || TargetLanguage != target ||
                    TranslationBadge.Text != UiText.T("{0} 번역 · 항상 로컬", TargetLanguageName))
                    throw new InvalidOperationException("언어 설정 복원 또는 선택 언어 안내가 맞지 않습니다.");
                if (source == "ko" && SourceLanguageName != UiText.T("한국어"))
                    throw new InvalidOperationException("한국어 입력이 자동 감지로 표시되었습니다.");
            }
            ApplyLanguageSettings(JsonSerializer.SerializeToElement(new { language = "zh" }));
            if (SourceLanguage != "zh" || TargetLanguage != "ko")
                throw new InvalidOperationException("기존 설정에는 한국어 번역 기본값을 적용해야 합니다.");
            ApplyLanguageSettings(JsonSerializer.SerializeToElement(new { language = "unsupported", target_language = "unsupported" }));
            if (SourceLanguage != "auto" || TargetLanguage != "ko")
                throw new InvalidOperationException("알 수 없는 언어 설정의 기본값이 맞지 않습니다.");
            foreach ((bool busy, bool running) in new[] { (true, false), (false, true) })
            {
                _busy = busy; _running = running; UpdateControls();
                if (LanguageBox.IsEnabled || TargetLanguageBox.IsEnabled)
                    throw new InvalidOperationException("준비·실행 중 언어 선택이 잠기지 않았습니다.");
            }
            _busy = false; _running = false; UpdateControls();
            if (!LanguageBox.IsEnabled || !TargetLanguageBox.IsEnabled)
                throw new InvalidOperationException("중지 후 언어 선택이 복원되지 않았습니다.");
            var system = new AudioTarget("system", null, IntPtr.Zero);
            var micOne = new AudioTarget("mic", null, IntPtr.Zero, "test-mic-1");
            var micTwo = new AudioTarget("mic", null, IntPtr.Zero, "test-mic-2");
            if (SameAudioTarget(system, micOne) || SameAudioTarget(micOne, micTwo) ||
                !SameAudioTarget(micOne, micOne with { DisplayName = "renamed mic" }))
                throw new InvalidOperationException("마이크와 시스템 입력의 선택 유지 식별자가 충돌합니다.");
            var renamedMic = micOne with { DisplayName = "renamed mic" };
            var browser = new AudioTarget("browser", 1234, new IntPtr(5678));
            var refreshed = new[] { micTwo, system, renamedMic };
            if (!ReferenceEquals(FindAudioSelection(refreshed, micOne), renamedMic) ||
                !ReferenceEquals(FindAudioSelection(refreshed, system), system) ||
                !ReferenceEquals(FindAudioSelection(refreshed, micTwo), micTwo) ||
                FindAudioSelection(new[] { micTwo, system, browser }, micOne) != null ||
                FindAudioSelection(new[] { micTwo, system }, browser) != null ||
                !ReferenceEquals(FindAudioSelection(new[] { micTwo, system, browser }, null), browser) ||
                !ReferenceEquals(FindAudioSelection(new[] { micTwo, system }, null), system) ||
                FindAudioSelection(new[] { micTwo }, null) != null ||
                FindAudioSelection(new[] { micTwo, system, browser }, null, selectDefault: false) != null)
                throw new InvalidOperationException("새로고침 후 마이크 ID·시스템 선택 유지 또는 제거된 장치 처리가 잘못되었습니다.");
        }
        finally
        {
            LanguageBox.SelectedIndex = previousSource; TargetLanguageBox.SelectedIndex = previousTarget;
            _busy = previousBusy; _running = previousRunning;
            UpdateLanguageHint(); UpdateModeAppearance(); UpdateControls();
            _initialized = previousInitialized;
        }
    }

    internal void VerifyClearLogsControls()
    {
        // Inspect UI wiring and synthetic replies only; never call the endpoint.
        bool previousInitialized = _initialized, previousClearing = _clearingLogs;
        bool previousRunning = _running, previousExiting = _exiting;
        try
        {
            _initialized = true;
            if (ClearLogsButton.Content?.ToString() != UiText.T("번역 기록 삭제") ||
                ClearLogsButton.Parent is not StackPanel parent)
                throw new InvalidOperationException("번역 기록 삭제 버튼을 찾지 못했습니다.");
            int position = parent.Children.IndexOf(ClearLogsButton);
            if (parent.Orientation != Orientation.Horizontal || position < 0 ||
                position + 1 >= parent.Children.Count ||
                !ReferenceEquals(parent.Children[position + 1], QuitButton) ||
                QuitButton.Content?.ToString() != UiText.T("완전히 종료"))
                throw new InvalidOperationException("번역 기록 삭제 버튼은 종료 버튼 바로 왼쪽에 있어야 합니다.");
            foreach (bool running in new[] { false, true })
            {
                _running = running; _clearingLogs = false; _exiting = false; UpdateControls();
                if (ClearLogsButton.IsEnabled != (_connected && _engine != null))
                    throw new InvalidOperationException("기록 삭제 버튼 연결·실행 상태가 맞지 않습니다.");
                _clearingLogs = true; UpdateControls();
                if (ClearLogsButton.IsEnabled || (running && !StopButton.IsEnabled))
                    throw new InvalidOperationException("삭제 중 중복 클릭 차단 또는 자막 중지 버튼이 잘못 잠겼습니다.");
            }
            _clearingLogs = false; _exiting = true; UpdateControls();
            if (ClearLogsButton.IsEnabled) throw new InvalidOperationException("종료 중 기록 삭제 버튼이 활성화되었습니다.");
            var success = ReadLogClearResult(JsonSerializer.SerializeToElement(new { ok = true, deleted_count = 3, failed_files = Array.Empty<string>() }));
            var partial = ReadLogClearResult(JsonSerializer.SerializeToElement(new { ok = true, deleted_count = 2, failed_files = new[] { "busy.log" } }));
            var failure = ReadLogClearResult(JsonSerializer.SerializeToElement(new { ok = false, deleted_count = 0, failed_files = new[] { "busy.log" } }));
            if (!success.Complete || success.DeletedCount != 3 || partial.Complete || failure.Complete ||
                partial.FailedFiles.Single() != "busy.log")
                throw new InvalidOperationException("기록 삭제의 성공·부분 실패 표시가 올바르지 않습니다.");
        }
        finally
        {
            _clearingLogs = previousClearing; _running = previousRunning; _exiting = previousExiting;
            UpdateControls(); _initialized = previousInitialized;
        }
    }

    internal void VerifyTranslationTestDefaults()
    {
        // Model selection checks only: no model loading, audio or engine calls.
        var previousItems = TranslationModels.ItemsSource;
        object? previousSelection = TranslationModels.SelectedItem;
        string previousText = TestInput.Text;
        bool previousInitialized = _initialized;
        int previousSource = LanguageBox.SelectedIndex, previousTarget = TargetLanguageBox.SelectedIndex;
        string japaneseModelHint = UiText.T("모델 준비에는 시간이 걸릴 수 있습니다. 시작 전에 모델을 메모리에 불러옵니다.\n일본어→한국어 전용입니다. 입력: 일본어/자동 · 번역: 한국어");
        string generalModelHint = UiText.T("모델 준비에는 시간이 걸릴 수 있습니다. 시작 전에 모델을 메모리에 불러옵니다.\n번역 결과만 출력합니다. Qwen 모델은 사고 모드를 자동으로 해제합니다.");
        try
        {
            _initialized = true;
            TranslationModels.ItemsSource = new[] {
                new ModelChoice("HY-MT2", "models/translation/HY-MT2-7B-Q6_K.gguf"),
                new ModelChoice("JA-KO-VN", "models/translation/ja-ko-vn-12b-v2-Q4_K_M.gguf"),
                new ModelChoice("Other", "models/translation/ja-ko-vn-12b-v2/HY-MT2-7B-Q6_K.gguf")
            };
            SelectLanguage(LanguageBox, "zh"); SelectLanguage(TargetLanguageBox, "en");
            TestInput.Text = DefaultTranslationTest;
            TranslationModels.SelectedIndex = 1;
            if (TestInput.Text != JapaneseTranslationTest || ModelPreparationHint.Text != japaneseModelHint)
                throw new InvalidOperationException("일본어 전용 모델의 기본 테스트 문구 또는 안내가 맞지 않습니다.");
            TranslationModels.SelectedIndex = 0;
            if (TestInput.Text != DefaultTranslationTest || ModelPreparationHint.Text != generalModelHint)
                throw new InvalidOperationException("다른 모델을 선택한 뒤 기본 테스트 문구가 복원되지 않았습니다.");
            TestInput.Text = "직접 입력한 테스트 문구";
            TranslationModels.SelectedIndex = 1; TranslationModels.SelectedIndex = 0;
            if (TestInput.Text != "직접 입력한 테스트 문구")
                throw new InvalidOperationException("모델 변경으로 직접 입력한 테스트 문구가 덮어써졌습니다.");
            TestInput.Text = ""; TranslationModels.SelectedIndex = 1;
            if (TestInput.Text.Length != 0) throw new InvalidOperationException("사용자가 비운 테스트 입력이 변경되었습니다.");
            TestInput.Text = JapaneseTranslationTest; TranslationModels.SelectedIndex = 2;
            if (TestInput.Text != DefaultTranslationTest)
                throw new InvalidOperationException("상위 폴더 이름만으로 일본어 전용 모델을 판별했습니다.");
            ApplyEngineSettings(JsonSerializer.SerializeToElement(new {
                mode = Mode, language = "zh", target_language = "en",
                translation_model = "models/translation/ja-ko-vn-12b-v2-Q4_K_M.gguf",
                asr_model = (AsrModels.SelectedItem as ModelChoice)?.Path ?? "",
                asr_profile = AsrProfile, asr_hints = ReadAsrHints(),
                qwen_boundary_recheck = BoundaryRecheckBox.IsChecked == true
            }));
            if (TestInput.Text != JapaneseTranslationTest || SourceLanguage != "zh" || TargetLanguage != "en")
                throw new InvalidOperationException("설정 복원에서 테스트 문구를 놓치거나 언어 선택을 자동 변경했습니다.");
        }
        finally
        {
            _initialized = false;
            TranslationModels.ItemsSource = previousItems; TranslationModels.SelectedItem = previousSelection;
            LanguageBox.SelectedIndex = previousSource; TargetLanguageBox.SelectedIndex = previousTarget;
            UpdateTranslationModelHint(); TestInput.Text = previousText;
            UpdateLanguageHint(); UpdateModeAppearance(); _initialized = previousInitialized;
        }
    }

    private async Task SmokeTestAsync(string output)
    {
        try
        {
            Directory.CreateDirectory(output);
            AudioSources.ItemsSource = new[] { new AudioTarget("Edge · Live music / 라이브 방송", 1234, IntPtr.Zero) }; AudioSources.SelectedIndex = 0;
            TranslationModels.ItemsSource = new[] { new ModelChoice("Qwen3.5-4B-Q4_K_M.gguf", "models/translation/Qwen3.5-4B-Q4_K_M.gguf") }; TranslationModels.SelectedIndex = 0;
            AsrModels.ItemsSource = new[] { new ModelChoice("Qwen3-ASR-1.7B", "models/asr/qwen3-asr-1.7b", "qwen3_asr"), new ModelChoice("Whisper large-v3-turbo", "models/asr/whisper-large-v3-turbo", "whisper") }; AsrModels.SelectedIndex = 0;
            _connected = true;
            VerifyLanguageSelections();
            VerifyClearLogsControls();
            VerifyTranslationTestDefaults();
            File.WriteAllText(Path.Combine(output, "translation-test-defaults-check.json"), JsonSerializer.Serialize(new
            {
                success = true,
                japanese_model_stock_example_and_hint_checked = true,
                other_model_stock_example_restored = true,
                custom_and_empty_input_preserved = true,
                model_filename_not_parent_folder_checked = true,
                settings_restore_checked = true,
                language_choices_not_changed = true,
                uses_models_audio_or_api = false
            }, new JsonSerializerOptions { WriteIndented = true }));
            File.WriteAllText(Path.Combine(output, "clear-logs-ui-check.json"), JsonSerializer.Serialize(new
            {
                success = true,
                button_text = ClearLogsButton.Content?.ToString(),
                immediately_before_quit = true,
                connection_running_duplicate_and_exit_states_checked = true,
                partial_failure_handling_checked = true,
                deletion_endpoint_called = false
            }, new JsonSerializerOptions { WriteIndented = true }));
            File.WriteAllText(Path.Combine(output, "language-selection-check.json"), JsonSerializer.Serialize(new
            {
                success = true,
                source_languages = LanguageBox.Items.Cast<ComboBoxItem>().Select(item => item.Tag?.ToString()).ToArray(),
                target_languages = TargetLanguageBox.Items.Cast<ComboBoxItem>().Select(item => item.Tag?.ToString()).ToArray(),
                default_target_language = TargetLanguage,
                settings_roundtrip_combinations = 20,
                missing_target_defaults_to_korean = true,
                preparation_and_running_lock_checked = true,
                microphone_identity_and_refresh_checked = true,
                uses_models_audio_or_api = false
            }, new JsonSerializerOptions { WriteIndented = true }));
            SetStatus("연결됨 · 준비 대기", "로컬 음성 인식과 번역 모델이 준비되었습니다.", "#3BD8AD");
            _captions.Add(new CaptionRow("21:04:18", "오늘 방송에 와 주셔서 감사합니다. 잠시 후 새로운 곡을 들려드릴게요.", "Thank you for joining today's stream. I'll play a new song in a moment.", "1"));
            _captions.Add(new CaptionRow("21:04:12", "소리가 잘 들리시나요? 채팅으로 알려 주세요.", "Can you hear me clearly? Let me know in the chat.", "2"));
            LocalizeText(PartialText, "다음 발화를 기다리고 있습니다…");
            _logs.Add("21:04:10  로컬 엔진 준비 완료 · 127.0.0.1:17865");
            _logs.Add("21:04:12  모델 준비가 완료되었습니다.");
            TestResult.Text = "안녕하세요! 오늘 라이브 방송을 시청해 주셔서 감사합니다.";
            _ui.FollowWindow = false;
            _overlay = new OverlayWindow(_ui); _overlay.SetCaption("오늘 방송에 와 주셔서 감사합니다.", "Thank you for joining today's stream.");
            UpdateControls(); await Task.Delay(700); UpdateLayout();
            SaveVisual(this, Path.Combine(output, "main-window.png"));
            _overlay.UpdateLayout(); SaveVisual(_overlay, Path.Combine(output, "overlay-window.png"));
            LanguageBox.SelectedIndex = 2;
            UpdateLayout();
            SaveVisual(this, Path.Combine(output, "input-language-chinese.png"));
            AsrProfileBox.BringIntoView(); UpdateLayout();
            SaveVisual(this, Path.Combine(output, "asr-profile.png"));
            AsrHintsBox.BringIntoView(); UpdateLayout();
            SaveVisual(this, Path.Combine(output, "asr-options.png"));
        }
        catch (Exception ex) { File.WriteAllText(Path.Combine(output, "smoke-error.txt"), ex.ToString()); }
        finally { _exiting = true; _overlay?.Close(); System.Windows.Application.Current.Shutdown(); }
    }

    private async Task LiveSmokeTestAsync(string output, int processId)
    {
        JsonElement? previousSettings = null;
        try
        {
            Directory.CreateDirectory(output);
            RefreshSources();
            await ConnectAsync();
            if (!_connected || _engine == null) throw new InvalidOperationException("실제 UI 진단에서 로컬 엔진에 연결하지 못했습니다: " + StatusDetail.Text);
            previousSettings = await _engine.GetAsync("v1/settings", _lifetime.Token);
            var target = AudioSources.Items.Cast<AudioTarget>().FirstOrDefault(item => item.ProcessId == processId);
            if (target == null) throw new InvalidOperationException($"PID {processId}의 Chrome/Edge 창을 찾지 못했습니다.");
            AudioSources.SelectedItem = target;
            LocalMode.IsChecked = true; GeminiMode.IsChecked = false; LanguageBox.SelectedIndex = 0;
            ShowOverlayBox.IsChecked = true; BilingualBox.IsChecked = true; EditOverlayBox.IsChecked = false;
            // Keep the user's browser state untouched; the selected window may be minimized.
            // This diagnostic changes only the in-memory overlay view, never persisted settings.
            FollowWindowBox.IsChecked = false;
            ApplyOverlaySettings();
            File.AppendAllText(Path.Combine(output, "diagnostic-startup.log"), $"{DateTime.UtcNow:O} Calling production Start_Click; PID={processId}; settings are not persisted\n");
            Start_Click(this, new RoutedEventArgs());
            DateTime deadline = DateTime.UtcNow.AddMinutes(5);
            while (!_running && _busy && DateTime.UtcNow < deadline) await Task.Delay(100);
            if (!_running) throw new InvalidOperationException("실제 UI 진단이 오디오 캡처를 시작하지 못했습니다: " + StatusDetail.Text);
            DateTime started = DateTime.UtcNow;
            while (_running && DateTime.UtcNow - started < TimeSpan.FromSeconds(20)) await Task.Delay(200);
            UpdateLayout(); _overlay?.UpdateLayout();
            SaveVisual(this, Path.Combine(output, "live-main-window.png"));
            if (_overlay != null) SaveVisual(_overlay, Path.Combine(output, "live-overlay-window.png"));
            object? finalOverlayState = _overlay?.DiagnosticState();
            var captions = _captions.Select(c => new { time = c.Time, source_text = c.Source, text = c.Translation, id = c.Id }).ToArray();
            await StopSessionAsync();
            JsonElement current = await _engine.GetAsync("v1/settings", _lifetime.Token);
            string[] checkKeys = { "mode", "language", "target_language", "translation_model", "asr_model", "asr_profile", "asr_hints", "qwen_boundary_recheck", "gemini_key_set", "save_gemini_key" };
            bool unchanged = checkKeys.All(key => Text(previousSettings.Value, key) == Text(current, key));
            File.WriteAllText(Path.Combine(output, "live-result.json"), JsonSerializer.Serialize(new
            {
                success = captions.Length > 0 && unchanged,
                process_id = processId,
                captured_chunks = _diagnosticPcmChunks,
                caption_count = captions.Length,
                duration_seconds = (DateTime.UtcNow - started).TotalSeconds,
                settings_unchanged = unchanged,
                overlay = finalOverlayState,
                captions,
                logs = _logs.ToArray()
            }, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex) { File.WriteAllText(Path.Combine(output, "live-error.txt"), ex.ToString()); }
        finally
        {
            File.AppendAllText(Path.Combine(output, "diagnostic-startup.log"), $"{DateTime.UtcNow:O} Live diagnostic ending through QuitAsync\n");
            await QuitAsync();
        }
    }

    private static void SaveVisual(FrameworkElement element, string path)
    {
        var bitmap = new RenderTargetBitmap((int)Math.Ceiling(element.ActualWidth), (int)Math.Ceiling(element.ActualHeight), 96, 96, PixelFormats.Pbgra32);
        bitmap.Render(element);
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
        using var file = File.Create(path); encoder.Save(file);
    }
}
