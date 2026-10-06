using System;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using LiveSubtitle.App.Services;

namespace LiveSubtitle.App;

public sealed class OverlayWindow : Window
{
    private readonly Border _surface;
    private readonly OutlinedCaptionText _previousTranslation;
    private readonly OutlinedCaptionText _translation;
    private readonly OutlinedCaptionText _source;
    private readonly TextBlock _editHint;
    private readonly DispatcherTimer _timer;
    private readonly DispatcherTimer _captionTimer;
    private bool _closed;
    private UiSettings _settings;
    private bool _editing;
    private IntPtr _followHandle;
    private IntPtr _handle;
    private IntPtr _anchorMonitor;
    private HwndSource? _windowSource;
    private HorizontalAnchor _anchor;
    private HorizontalAnchor? _effectiveAnchor;
    private PixelBounds? _browserPlacementBeforeFallback;
    private bool _anchorInitialized;
    private bool _constructing = true;
    private bool _hasExternalApply;
    private bool _inUserMove;
    private PixelBounds _moveStartBounds;
    private bool _movingProgrammatically;
    private int _preferredPixelWidth, _preferredPixelHeight;
    private DateTime _lastCaption = DateTime.MinValue;
    private string _lastTranslation = "";
    private string _lastSource = "";
    private readonly CaptionDisplayQueue _captions = new();

    // Commit completed placements immediately, independently of application exit.
    public event EventHandler? PlacementCommitted;

    public OverlayWindow(UiSettings settings)
    {
        _settings = settings;
        Title = UiText.T("LiveSubtitle 자막");
        AllowsTransparency = true; WindowStyle = WindowStyle.None; Background = Brushes.Transparent;
        ShowInTaskbar = false; ShowActivated = false; Topmost = true;
        Width = Math.Clamp(settings.OverlayWidth, 320, 3000); Height = Math.Clamp(settings.OverlayHeight, 90, 700);
        MinWidth = 320; MinHeight = 90; ResizeMode = ResizeMode.NoResize;
        Left = settings.OverlayLeft; Top = settings.OverlayTop;
        _previousTranslation = CreateTranslationText();
        _translation = CreateTranslationText();
        _previousTranslation.Margin = new Thickness(0, 0, 0, 6);
        _source = new OutlinedCaptionText { Foreground = new SolidColorBrush(Color.FromRgb(207, 221, 239)), FontWeight = FontWeights.Bold, FitToBounds = false, Margin = new Thickness(0, 6, 0, 0), TextWrapping = TextWrapping.Wrap };
        _editHint = new TextBlock { Text = UiText.T("드래그해서 이동 · 모서리에서 크기 조절 · 놓은 위치 자동 저장"), Foreground = new SolidColorBrush(Color.FromRgb(111, 237, 197)), FontSize = 12, HorizontalAlignment = HorizontalAlignment.Center, TextAlignment = TextAlignment.Center, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 8), Visibility = Visibility.Collapsed };
        var stack = new StackPanel(); stack.Children.Add(_editHint); stack.Children.Add(_previousTranslation); stack.Children.Add(_translation); stack.Children.Add(_source);
        _surface = new Border { Padding = new Thickness(18, 12, 18, 14), CornerRadius = new CornerRadius(10), Child = stack, VerticalAlignment = VerticalAlignment.Bottom, BorderThickness = new Thickness(1) };
        var layout = new Grid { Background = Brushes.Transparent, ClipToBounds = true }; layout.Children.Add(_surface); Content = layout;
        MouseLeftButtonDown += (_, e) =>
        {
            if (!_editing || e.ButtonState != MouseButtonState.Pressed) return;
            BeginUserPlacement();
            try { DragMove(); }
            catch (InvalidOperationException) { }
            finally { FinishUserPlacement(false); }
        };
        SourceInitialized += (_, _) =>
        {
            _handle = new WindowInteropHelper(this).Handle;
            _windowSource = HwndSource.FromHwnd(_handle);
            _windowSource?.AddHook(WindowMessage);
            RestorePhysicalBounds();
            ApplyNativeStyle();
            InitializeAnchor();
        };
        LocationChanged += (_, _) => SaveBounds();
        SizeChanged += (_, _) => { SaveBounds(); UpdateLineHeights(); };
        _timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(250) }; _timer.Tick += Timer_Tick; _timer.Start();
        _captionTimer = new DispatcherTimer(DispatcherPriority.Normal);
        _captionTimer.Tick += CaptionTimer_Tick;
        UiText.LanguageChanged += UiLanguageChanged;
        Closed += (_, _) => { _closed = true; UiText.LanguageChanged -= UiLanguageChanged; _timer.Stop(); _captionTimer.Stop(); _windowSource?.RemoveHook(WindowMessage); };
        Apply(settings, false, IntPtr.Zero);
        _constructing = false;
    }

    public void Apply(UiSettings settings, bool editing, IntPtr followHandle)
    {
        bool finishedEditing = _editing && !editing;
        _settings = settings; _editing = editing; _followHandle = followHandle;
        if (!_constructing) _hasExternalApply = true;
        _translation.FontSize = Math.Clamp(settings.FontSize, 18, 52);
        _previousTranslation.FontSize = _translation.FontSize;
        _previousTranslation.Height = _translation.Height = _translation.FontSize * 1.4;
        _source.FontSize = Math.Max(14, settings.FontSize * .6);
        _surface.Background = new SolidColorBrush(Color.FromArgb((byte)(255 * Math.Clamp(settings.BackgroundOpacity, 0, .95)), 8, 13, 22));
        _surface.BorderBrush = editing ? new SolidColorBrush(Color.FromRgb(74, 225, 176)) : Brushes.Transparent;
        _editHint.Visibility = editing ? Visibility.Visible : Visibility.Collapsed;
        ResizeMode = editing ? ResizeMode.CanResizeWithGrip : ResizeMode.NoResize;
        ApplyNativeStyle(); UpdateText();
        if (settings.ShowOverlay || editing) { if (!IsVisible) Show(); }
        else Hide();
        SynchronizeCaptionQueue();
        Timer_Tick(null, EventArgs.Empty);
        if (finishedEditing) CommitPlacement();
    }

    public void SetCaption(string translation, string source, string? captionId = null)
    {
        // After a quiet interval, a fresh utterance starts with one row. An
        // authoritative correction of either still-known row keeps its place.
        if ((DateTime.UtcNow - _lastCaption).TotalSeconds > 8 &&
            (string.IsNullOrEmpty(captionId) || !_captions.VisibleCaptions.Any(item => item.Id == captionId)))
        {
            _captions.ClearVisibleHistory();
            ClearCaptionText();
        }
        DisplayCaption? visible = _captions.Enqueue(translation, source, captionId, Environment.TickCount64);
        if (visible != null) PresentCaptions();
        SynchronizeCaptionQueue();
        Timer_Tick(null, EventArgs.Empty);
    }

    public void RemoveCaption(string captionId)
    {
        if (_captions.Remove(captionId, Environment.TickCount64))
        {
            if (_captions.Current != null) PresentCaptions();
            else ClearCaptionText();
        }
        SynchronizeCaptionQueue();
        Timer_Tick(null, EventArgs.Empty);
    }

    private void PresentCaptions()
    {
        _lastTranslation = string.Join("\n", _captions.VisibleCaptions.Select(item => SingleLine(item.Translation)));
        _lastSource = string.Join("\n", _captions.VisibleCaptions.Select(item => SingleLine(item.Source)));
        _lastCaption = DateTime.UtcNow; UpdateText();
        if (_settings.ShowOverlay || _editing)
        {
            if (!IsVisible) Show();
            // Popup players may also be topmost. Present each new caption without
            // activating it or changing the user's keyboard focus.
            if (_handle != IntPtr.Zero)
                SetWindowPos(_handle, new IntPtr(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010);
        }
    }

    public void PlaceOnMonitor()
    {
        if (_handle == IntPtr.Zero) new WindowInteropHelper(this).EnsureHandle();
        _hasExternalApply = true;
        ResetPresetSize(_handle);
        SelectAnchor(HorizontalAnchor.Monitor, ReadBounds());
        AlignToAnchor(); CommitPlacement(); Timer_Tick(null, EventArgs.Empty);
    }

    public void PlaceInBrowser()
    {
        if (_handle == IntPtr.Zero) new WindowInteropHelper(this).EnsureHandle();
        _hasExternalApply = true;
        ResetPresetSize(WindowTargets.Exists(_followHandle) ? _followHandle : _handle);
        _settings.OverlayBrowserBottomGap = BrowserBounds() != null ? DefaultBrowserBottomGap() : null;
        SelectAnchor(HorizontalAnchor.Browser, ReadBounds());
        AlignToAnchor(); CommitPlacement(); Timer_Tick(null, EventArgs.Empty);
    }

    private int DefaultBrowserBottomGap() => (int)Math.Round(24 *
        Math.Max(96, GetDpiForWindow(WindowTargets.Exists(_followHandle) ? _followHandle : _handle)) / 96.0);

    private void ResetPresetSize(IntPtr target)
    {
        _settings.OverlayManualPosition = false;
        double scale = Math.Max(96, GetDpiForWindow(target)) / 96.0;
        _settings.OverlayWidth = UiSettings.DefaultOverlayWidth;
        _settings.OverlayHeight = UiSettings.DefaultOverlayHeight;
        _preferredPixelWidth = (int)Math.Round(UiSettings.DefaultOverlayWidth * scale);
        _preferredPixelHeight = (int)Math.Round(UiSettings.DefaultOverlayHeight * scale);
        _settings.OverlayPreferredPixelWidth = _preferredPixelWidth;
        _settings.OverlayPreferredPixelHeight = _preferredPixelHeight;
    }

    public void Clear()
    {
        _captions.Clear(); ClearCaptionText(); SynchronizeCaptionQueue(); Timer_Tick(null, EventArgs.Empty);
    }

    private void ClearCaptionText()
    { _lastCaption = DateTime.MinValue; _lastTranslation = _lastSource = ""; UpdateText(); }

    internal object DiagnosticState() => new
    {
        windowVisible = IsVisible,
        surfaceVisible = _surface.Visibility.ToString(),
        width = ActualWidth,
        height = ActualHeight,
        surfaceWidth = _surface.ActualWidth,
        surfaceHeight = _surface.ActualHeight,
        translation = _lastTranslation,
        visibleCaptions = _captions.VisibleCaptions,
        translationLineCount = _captions.VisibleCaptions.Count,
        captionId = _captions.Current?.Id,
        pendingCaptions = _captions.PendingCount,
        captionAgeSeconds = (DateTime.UtcNow - _lastCaption).TotalSeconds,
        showOverlay = _settings.ShowOverlay,
        manualPosition = _settings.OverlayManualPosition,
        followWindow = _settings.FollowWindow,
        browserBottomGap = _settings.OverlayBrowserBottomGap,
        preferredPixelWidth = _preferredPixelWidth,
        preferredPixelHeight = _preferredPixelHeight,
        anchor = DiagnosticAnchor?.ToString(),
        effectiveAnchor = _effectiveAnchor?.ToString(),
        pixelBounds = DiagnosticPixelBounds,
        target = WindowTargets.DiagnosticState(_followHandle)
    };

    internal PixelBounds DiagnosticPixelBounds => ReadBounds();
    internal HorizontalAnchor? DiagnosticAnchor => _anchorInitialized ? _anchor : null;
    internal HorizontalAnchor? DiagnosticEffectiveAnchor => _effectiveAnchor;
    internal bool DiagnosticCaptionVisible => _surface.IsVisible;
    internal void DiagnosticFinishPlacement() => FinishUserPlacement(true);
    internal void DiagnosticSynchronize() { SynchronizeCaptionQueue(); Timer_Tick(null, EventArgs.Empty); }

    private void UpdateText()
    {
        bool preview = _editing && (DateTime.UtcNow - _lastCaption).TotalSeconds > 8;
        _previousTranslation.Text = preview ? "" : SingleLine(_captions.Previous?.Translation ?? "");
        _translation.Text = preview ? UiText.T("실시간 번역 자막이 여기에 표시됩니다.") : SingleLine(_captions.Current?.Translation ?? "");
        _previousTranslation.Visibility = !preview && _captions.Previous != null ? Visibility.Visible : Visibility.Collapsed;
        _translation.Visibility = preview || _captions.Current != null ? Visibility.Visible : Visibility.Collapsed;
        _source.Text = preview ? UiText.T("Live captions appear here.") : _lastSource;
        _source.Visibility = _settings.Bilingual ? Visibility.Visible : Visibility.Collapsed;
        UpdateLineHeights();
    }

    private void UiLanguageChanged(object? sender, EventArgs e)
    {
        if (_closed) return;
        Title = UiText.T("LiveSubtitle 자막");
        _editHint.Text = UiText.T("드래그해서 이동 · 모서리에서 크기 조절 · 놓은 위치 자동 저장");
        // Only editor samples are localized. Captured and translated speech stays intact.
        UpdateText();
    }

    private void UpdateLineHeights()
    {
        // Keep both rows inside a manually shortened overlay as well as fitting
        // long sentences horizontally. This changes text scale, not placement.
        int rows = _previousTranslation.Visibility == Visibility.Visible ? 2 : 1;
        double width = Math.Max(1, (ActualWidth > 0 ? ActualWidth : Width) - 38);
        double available = (ActualHeight > 0 ? ActualHeight : Height) - 28 - (rows == 2 ? 6 : 0);
        foreach (FrameworkElement extra in new FrameworkElement[] { _editHint, _source })
        {
            if (extra.Visibility != Visibility.Visible) continue;
            extra.Measure(new Size(width, double.PositiveInfinity));
            available -= extra.DesiredSize.Height;
        }
        _previousTranslation.Height = _translation.Height = Math.Max(1,
            Math.Min(_translation.FontSize * 1.4, available / rows));
    }

    private void Timer_Tick(object? sender, EventArgs e)
    {
        InitializeAnchor();
        if ((_anchorInitialized || _hasExternalApply) && !_inUserMove)
        {
            // Keep the chosen Browser preset while no target is available. The
            // monitor fallback is temporary, including startup before selection.
            if (!_anchorInitialized || _anchor == HorizontalAnchor.Monitor || _settings.FollowWindow ||
                BrowserBounds() == null || _effectiveAnchor == HorizontalAnchor.Monitor) AlignToAnchor();
        }
        UpdateCaptionVisibility();
    }

    private void CaptionTimer_Tick(object? sender, EventArgs e)
    {
        SynchronizeCaptionQueue();
        if (!_closed) UpdateCaptionVisibility();
    }

    private void SynchronizeCaptionQueue()
    {
        _captionTimer.Stop();
        if (_closed) return;
        // Advance at most one caption, even when the dispatcher was delayed.
        // The queue keeps each row's original reading clock and FIFO order.
        if (_captions.Advance(Environment.TickCount64) != null) PresentCaptions();
        if (_captions.PendingCount == 0) return;
        _captionTimer.Interval = TimeSpan.FromMilliseconds(Math.Max(1,
            _captions.MillisecondsUntilAdvance(Environment.TickCount64)));
        _captionTimer.Start();
    }

    private void UpdateCaptionVisibility()
    {
        // Browser availability determines placement, never whether a new caption exists.
        bool contentVisible = (_settings.ShowOverlay || _editing) &&
            (_editing || (DateTime.UtcNow - _lastCaption).TotalSeconds <= 8);
        _surface.Visibility = contentVisible ? Visibility.Visible : Visibility.Hidden;
        if (_editing) UpdateText();
    }

    private static string SingleLine(string text) => Regex.Replace(text, @"\s+", " ").Trim();

    private static OutlinedCaptionText CreateTranslationText() => new()
    {
        Foreground = Brushes.White, FontWeight = FontWeights.Bold,
        HorizontalAlignment = HorizontalAlignment.Stretch
    };

    private void InitializeAnchor()
    {
        // The constructor shows the HWND before the real target is supplied. Do not
        // snap that temporary Apply(..., IntPtr.Zero), which would change the drop test.
        if (_anchorInitialized || !_hasExternalApply || _handle == IntPtr.Zero) return;
        PixelBounds bounds = ReadBounds();
        if (bounds.Width <= 0 || bounds.Height <= 0) return;
        if (_settings.OverlayManualPosition)
        {
            SelectAnchor(HorizontalAnchor.Monitor, bounds);
            _effectiveAnchor = HorizontalAnchor.Monitor;
            CommitPlacement();
            return;
        }
        // Older versions followed the browser bottom but saved only manual DIP
        // coordinates. Preserve that initial placement once when no physical
        // position has ever been saved; subsequent starts always use saved Y.
        if (string.IsNullOrEmpty(_settings.OverlayAnchorMode) &&
            _settings.OverlayPixelTop == null && _settings.OverlayPixelLeft == null &&
            _settings.FollowWindow && WindowTargets.Exists(_followHandle))
        {
            // Do not replace the old placement with a default Y just because its
            // browser is minimized during startup. A manual drop can still commit.
            if (BrowserBounds() is not { } legacyBrowser) return;
            double dpi = GetDpiForWindow(_followHandle) / 96.0;
            int width = Math.Min((int)Math.Round(_settings.OverlayWidth * dpi), Math.Max(300, legacyBrowser.Width - 36));
            int height = Math.Min((int)Math.Round(_settings.OverlayHeight * dpi), Math.Max(100, legacyBrowser.Height - 20));
            bounds = new PixelBounds(legacyBrowser.X + (legacyBrowser.Width - width) / 2,
                legacyBrowser.Y + legacyBrowser.Height - height - (int)(24 * dpi), width, height);
            PlaceBounds(bounds, true);
            bounds = ReadBounds();
        }
        HorizontalAnchor anchor = _settings.OverlayAnchorMode switch
        {
            "Browser" => HorizontalAnchor.Browser,
            "Monitor" => HorizontalAnchor.Monitor,
            _ => OverlayAlignment.DetermineAnchor(bounds, BrowserBounds())
        };
        if (anchor == HorizontalAnchor.Browser && _settings.OverlayBrowserBottomGap == null && BrowserBounds() != null)
            _settings.OverlayBrowserBottomGap = DefaultBrowserBottomGap();
        SelectAnchor(anchor, bounds);
        AlignToAnchor();
        CommitPlacement();
    }

    private IntPtr WindowMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (message == 0x0231 && _editing) BeginUserPlacement(); // WM_ENTERSIZEMOVE
        else if (message == 0x0232) FinishUserPlacement(false); // WM_EXITSIZEMOVE
        return IntPtr.Zero;
    }

    private void BeginUserPlacement()
    {
        if (_inUserMove) return;
        _moveStartBounds = ReadBounds();
        _inUserMove = true;
    }

    private void FinishUserPlacement(bool force)
    {
        if ((!force && !_inUserMove) || _handle == IntPtr.Zero) return;
        // WM_EXITSIZEMOVE and DragMove's finally can both arrive. Only the first
        // completes the gesture; a second classification would inspect the snapped X.
        _inUserMove = false;
        PixelBounds dropped = ReadBounds();
        if (!force && dropped == _moveStartBounds) return;
        bool resized = force || dropped.Width != _moveStartBounds.Width || dropped.Height != _moveStartBounds.Height;
        _settings.OverlayManualPosition = true;
        SelectAnchor(HorizontalAnchor.Monitor, dropped, true, resized);
        AlignToAnchor();
        CommitPlacement();
        Timer_Tick(null, EventArgs.Empty);
    }

    private void SelectAnchor(HorizontalAnchor anchor, PixelBounds bounds, bool userPlacement = false, bool userResized = false)
    {
        _anchor = anchor;
        _anchorInitialized = true;
        _anchorMonitor = MonitorAtCenter(bounds);
        if (anchor == HorizontalAnchor.Monitor) _browserPlacementBeforeFallback = null;
        _settings.OverlayAnchorMode = anchor.ToString();
        if (userResized || _preferredPixelWidth <= 0 || _preferredPixelHeight <= 0)
        {
            _preferredPixelWidth = !userResized && _settings.OverlayPreferredPixelWidth is > 0
                ? _settings.OverlayPreferredPixelWidth.Value : bounds.Width;
            _preferredPixelHeight = !userResized && _settings.OverlayPreferredPixelHeight is > 0
                ? _settings.OverlayPreferredPixelHeight.Value : bounds.Height;
        }
        if (anchor == HorizontalAnchor.Browser && (userPlacement || _settings.OverlayBrowserBottomGap == null))
            CaptureBrowserBottomGap(bounds);
    }

    private void CaptureBrowserBottomGap(PixelBounds bounds)
    {
        // Do not infer an offset from minimized/unavailable browser coordinates.
        if (BrowserBounds() is not { } browser) return;
        _settings.OverlayBrowserBottomGap = OverlayAlignment.FitsVertically(bounds, browser)
            ? OverlayAlignment.BottomGap(bounds, browser)
            : (int)Math.Round(24 * GetDpiForWindow(_followHandle) / 96.0);
    }

    private PixelBounds? BrowserBounds()
    {
        return WindowTargets.TryGetClientBounds(_followHandle, out int x, out int y, out int width, out int height)
            ? new PixelBounds(x, y, width, height) : null;
    }

    private void AlignToAnchor()
    {
        if (_inUserMove || _handle == IntPtr.Zero) return;
        if (_settings.OverlayManualPosition)
        {
            _effectiveAnchor = HorizontalAnchor.Monitor;
            return;
        }
        PixelBounds bounds = ReadBounds();
        if (bounds.Width <= 0 || bounds.Height <= 0) return;
        PixelBounds? browser = BrowserBounds();
        HorizontalAnchor effective = _anchorInitialized && _anchor == HorizontalAnchor.Browser && browser != null
            ? HorizontalAnchor.Browser : HorizontalAnchor.Monitor;
        if (effective == HorizontalAnchor.Monitor && _effectiveAnchor != HorizontalAnchor.Monitor)
        {
            if (!_anchorInitialized || _anchor == HorizontalAnchor.Browser)
            {
                _anchorMonitor = MonitorAtCenter(bounds);
                _browserPlacementBeforeFallback ??= bounds;
            }
        }
        _effectiveAnchor = effective;
        PixelBounds? container = effective == HorizontalAnchor.Browser ? browser : MonitorBounds(bounds);
        if (container is not { } valid || bounds.Width <= 0 || bounds.Height <= 0) return;
        bool acquiredGap = false;
        if (effective == HorizontalAnchor.Browser && _settings.OverlayBrowserBottomGap == null)
        {
            // A Browser preset chosen before its target existed must resolve
            // size and gap using that browser's DPI, not the fallback monitor's.
            ResetPresetSize(_followHandle);
            _settings.OverlayBrowserBottomGap = DefaultBrowserBottomGap();
            acquiredGap = true;
        }
        PixelBounds preferred = bounds with
        {
            Width = _preferredPixelWidth > 0 ? _preferredPixelWidth : bounds.Width,
            Height = _preferredPixelHeight > 0 ? _preferredPixelHeight : bounds.Height
        };
        PixelBounds target;
        if (effective == HorizontalAnchor.Browser)
        {
            target = OverlayAlignment.InsideBrowser(preferred, valid, _settings.OverlayBrowserBottomGap ?? 0);
            _browserPlacementBeforeFallback = null;
        }
        else target = OverlayAlignment.OnMonitor(preferred, valid);
        if (target == bounds)
        {
            if (acquiredGap) CommitPlacement();
            return;
        }
        PlaceBounds(target, target.Width != bounds.Width || target.Height != bounds.Height);
        if (acquiredGap) CommitPlacement();
        else SaveBounds();
    }

    private PixelBounds? MonitorBounds(PixelBounds overlay)
    {
        var info = new MonitorInfo { Size = Marshal.SizeOf<MonitorInfo>() };
        if (_anchorMonitor == IntPtr.Zero || !GetMonitorInfo(_anchorMonitor, ref info))
        {
            _anchorMonitor = MonitorAtCenter(overlay);
            if (_anchorMonitor == IntPtr.Zero || !GetMonitorInfo(_anchorMonitor, ref info)) return null;
        }
        return new PixelBounds(info.Monitor.Left, info.Monitor.Top,
            info.Monitor.Right - info.Monitor.Left, info.Monitor.Bottom - info.Monitor.Top);
    }

    private static IntPtr MonitorAtCenter(PixelBounds bounds)
    {
        var center = new NativePoint
        {
            X = (int)Math.Clamp((long)bounds.X + bounds.Width / 2L, int.MinValue, int.MaxValue),
            Y = (int)Math.Clamp((long)bounds.Y + bounds.Height / 2L, int.MinValue, int.MaxValue)
        };
        return MonitorFromPoint(center, 2); // MONITOR_DEFAULTTONEAREST; rcMonitor includes taskbar area.
    }

    private PixelBounds ReadBounds()
    {
        return _handle != IntPtr.Zero && GetWindowRect(_handle, out NativeRect rect)
            ? new PixelBounds(rect.Left, rect.Top, rect.Right - rect.Left, rect.Bottom - rect.Top)
            : default;
    }

    private void PlaceBounds(PixelBounds bounds, bool resize)
    {
        _movingProgrammatically = true;
        try
        {
            if (resize) AllowRequestedSize(bounds);
            SetWindowPos(_handle, IntPtr.Zero, bounds.X, bounds.Y, bounds.Width, bounds.Height,
                0x0004u | 0x0010u | (resize ? 0u : 0x0001u)); // NOZORDER | NOACTIVATE | optional NOSIZE
            // Per-monitor DPI changes may cause WPF to apply a suggested size. Keep
            // this operation's physical Y and size rather than adopting that resize.
            if (ReadBounds() != bounds)
            {
                AllowRequestedSize(bounds);
                SetWindowPos(_handle, IntPtr.Zero, bounds.X, bounds.Y, bounds.Width, bounds.Height, 0x0004 | 0x0010);
            }
        }
        finally { _movingProgrammatically = false; }
    }

    private void AllowRequestedSize(PixelBounds bounds)
    {
        double scale = Math.Max(1, GetDpiForWindow(_handle)) / 96.0;
        MinWidth = Math.Min(320, bounds.Width / scale);
        MinHeight = Math.Min(90, bounds.Height / scale);
    }

    private void RestorePhysicalBounds()
    {
        if (_settings.OverlayPixelLeft is int x && _settings.OverlayPixelTop is int y &&
            _settings.OverlayPixelWidth is int width && width > 0 && _settings.OverlayPixelHeight is int height && height > 0)
            PlaceBounds(new PixelBounds(x, y, width, height), true);
        // A disconnected monitor must not leave the editable overlay unreachable.
        // This startup-only recovery does not change an on-screen position or size.
        PixelBounds bounds = ReadBounds();
        var rect = new NativeRect { Left = bounds.X, Top = bounds.Y, Right = bounds.X + bounds.Width, Bottom = bounds.Y + bounds.Height };
        if (bounds.Width <= 0 || bounds.Height <= 0 || MonitorFromRect(ref rect, 0) != IntPtr.Zero) return;
        _anchorMonitor = MonitorAtCenter(bounds);
        if (MonitorBounds(bounds) is not { } monitor) return;
        int top = (int)Math.Clamp((long)bounds.Y, monitor.Y, Math.Max((long)monitor.Y, (long)monitor.Y + monitor.Height - bounds.Height));
        PlaceBounds(bounds with { X = OverlayAlignment.CenteredLeft(bounds, monitor), Y = top }, false);
    }

    private void SaveBounds()
    {
        if (!_anchorInitialized || _movingProgrammatically || _inUserMove || _handle == IntPtr.Zero) return;
        // A temporary monitor fallback must not overwrite the saved browser position.
        if (_anchor == HorizontalAnchor.Browser && _effectiveAnchor == HorizontalAnchor.Monitor) return;
        PixelBounds bounds = ReadBounds();
        if (bounds.Width <= 0 || bounds.Height <= 0) return;
        _settings.OverlayPixelLeft = bounds.X; _settings.OverlayPixelTop = bounds.Y;
        _settings.OverlayPixelWidth = bounds.Width; _settings.OverlayPixelHeight = bounds.Height;
        if (_preferredPixelWidth > 0) _settings.OverlayPreferredPixelWidth = _preferredPixelWidth;
        if (_preferredPixelHeight > 0) _settings.OverlayPreferredPixelHeight = _preferredPixelHeight;
        // Retain the old DIP fields so earlier settings files still load normally.
        _settings.OverlayLeft = Left; _settings.OverlayTop = Top;
        _settings.OverlayWidth = ActualWidth > 0 ? ActualWidth : Width;
        _settings.OverlayHeight = ActualHeight > 0 ? ActualHeight : Height;
    }

    private void CommitPlacement()
    {
        SaveBounds();
        if (_anchorInitialized && !_inUserMove && !_movingProgrammatically)
            PlacementCommitted?.Invoke(this, EventArgs.Empty);
    }

    private void ApplyNativeStyle()
    {
        if (_handle == IntPtr.Zero) return;
        long style = GetWindowLongPtr(_handle, -20).ToInt64();
        style |= 0x00000080L; // TOOLWINDOW
        if (_editing) style &= ~(0x00000020L | 0x08000000L);
        else style |= 0x00000020L | 0x08000000L; // TRANSPARENT | NOACTIVATE
        SetWindowLongPtr(_handle, -20, new IntPtr(style));
        SetWindowPos(_handle, new IntPtr(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020);
    }

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] private static extern IntPtr GetWindowLongPtr(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")] private static extern IntPtr SetWindowLongPtr(IntPtr hwnd, int index, IntPtr value);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hwnd, IntPtr insertAfter, int x, int y, int width, int height, uint flags);
    [StructLayout(LayoutKind.Sequential)] private struct NativePoint { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] private struct NativeRect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct MonitorInfo { public int Size; public NativeRect Monitor, Work; public uint Flags; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out NativeRect rect);
    [DllImport("user32.dll")] private static extern IntPtr MonitorFromPoint(NativePoint point, uint flags);
    [DllImport("user32.dll")] private static extern IntPtr MonitorFromRect(ref NativeRect rect, uint flags);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll", EntryPoint = "GetMonitorInfoW")] private static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);
}
