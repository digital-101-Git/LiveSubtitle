using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace LiveSubtitle.App.Services;

public sealed record AudioTarget(string DisplayName, int? ProcessId, IntPtr WindowHandle,
    string? CaptureDeviceId = null) : INotifyPropertyChanged
{
    private string _displayName = DisplayName;
    internal string? DisplayNameSource { get; init; }
    internal object?[] DisplayNameArguments { get; init; } = Array.Empty<object?>();
    public string DisplayName
    {
        get => DisplayNameSource is { } source ? UiText.T(source, DisplayNameArguments) : _displayName;
        init => _displayName = value;
    }
    public event PropertyChangedEventHandler? PropertyChanged;
    public void RefreshDisplayName() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DisplayName)));
    public bool IsMicrophone => CaptureDeviceId is not null;
    public override string ToString() => DisplayName;
}

public static class WindowTargets
{
    public static List<AudioTarget> Enumerate()
    {
        var items = new List<AudioTarget>();
        EnumWindows((handle, _) =>
        {
            try
            {
                if (!IsWindowVisible(handle) || GetWindowTextLength(handle) == 0) return true;
                GetWindowThreadProcessId(handle, out uint pid);
                using var process = Process.GetProcessById((int)pid);
                string name = process.ProcessName;
                if (!name.Equals("chrome", StringComparison.OrdinalIgnoreCase) && !name.Equals("msedge", StringComparison.OrdinalIgnoreCase)) return true;
                var title = new StringBuilder(512); GetWindowText(handle, title, title.Capacity);
                items.Add(new AudioTarget($"{(name.Equals("msedge", StringComparison.OrdinalIgnoreCase) ? "Edge" : "Chrome")} · {title}", (int)pid, handle));
            }
            catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception) { }
            return true;
        }, IntPtr.Zero);
        items.Sort((a, b) => string.Compare(a.DisplayName, b.DisplayName, StringComparison.CurrentCulture));
        items.Add(new AudioTarget("시스템 전체 소리 · 다른 앱 소리도 포함", null, IntPtr.Zero)
        { DisplayNameSource = "시스템 전체 소리 · 다른 앱 소리도 포함" });
        foreach (var device in AudioInputDevices.Enumerate())
            items.Add(new AudioTarget(device.FriendlyName, null, IntPtr.Zero, device.Id)
            { DisplayNameSource = "마이크 · {0}", DisplayNameArguments = [device.FriendlyName] });
        return items;
    }

    public static bool TryGetClientBounds(IntPtr handle, out int x, out int y, out int width, out int height)
    {
        x = y = width = height = 0;
        if (handle == IntPtr.Zero || !IsWindow(handle) || IsIconic(handle) || !IsWindowVisible(handle)) return false;
        if (!GetClientRect(handle, out RECT bounds)) return false;
        POINT point = new() { X = bounds.Left, Y = bounds.Top };
        if (!ClientToScreen(handle, ref point)) return false;
        x = point.X; y = point.Y; width = bounds.Right - bounds.Left; height = bounds.Bottom - bounds.Top;
        return width > 0 && height > 0;
    }

    public static bool Exists(IntPtr handle) => handle != IntPtr.Zero && IsWindow(handle);
    public static object DiagnosticState(IntPtr handle)
    {
        bool boundsValid = TryGetClientBounds(handle, out int x, out int y, out int width, out int height);
        return new { handle = handle.ToInt64(), exists = IsWindow(handle), visible = IsWindowVisible(handle), minimized = IsIconic(handle), boundsValid, x, y, width, height };
    }
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int length);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll")] private static extern bool GetClientRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] private static extern bool ClientToScreen(IntPtr hWnd, ref POINT point);
    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct POINT { public int X, Y; }
}
