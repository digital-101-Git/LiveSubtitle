using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Data;

namespace LiveSubtitle.App.Services;

/// <summary>UI-only localization. Never sends display language to the inference engine.</summary>
public static class UiText
{
    private sealed class Entry
    {
        public string Source { get; set; } = "";
        public string En { get; set; } = "";
        public string Zh { get; set; } = "";
        public string Ja { get; set; } = "";
        public string For(string language) => language switch
        {
            "en" when En.Length > 0 => En,
            "zh" when Zh.Length > 0 => Zh,
            "ja" when Ja.Length > 0 => Ja,
            _ => Source
        };
    }

    private sealed class LanguageSource : INotifyPropertyChanged
    {
        public int Revision { get; private set; }
        public event PropertyChangedEventHandler? PropertyChanged;
        public void Notify()
        {
            Revision++;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Revision)));
        }
    }

    private sealed class LocalizedValue(string source, object?[] arguments) : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture) => T(source, arguments);
        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) => Binding.DoNothing;
    }

    private static readonly IReadOnlyDictionary<string, Entry> Entries = LoadEntries();
    private static readonly LanguageSource Source = new();
    private static string _language = "ko";
    public static string Language => _language;
    public static event EventHandler? LanguageChanged;

    public static string Normalize(string? language) => language is "en" or "zh" or "ja" ? language : "ko";

    public static void SetLanguage(string language)
    {
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher != null && !dispatcher.CheckAccess())
        {
            dispatcher.Invoke(() => SetLanguage(language));
            return;
        }
        language = Normalize(language);
        bool changed = language != _language;
        _language = language;
        // Populate even for Korean on startup, before XAML DynamicResources resolve.
        if (Application.Current is { } app)
            foreach (var entry in Entries.Values)
                app.Resources[ResourceKey(entry.Source)] = entry.For(language);
        if (!changed) return;
        Source.Notify();
        LanguageChanged?.Invoke(null, EventArgs.Empty);
    }

    public static string T(string source, params object?[] arguments)
    {
        string language = _language;
        string format = Entries.TryGetValue(source, out var entry) ? entry.For(language) : source;
        if (arguments.Length == 0) return format;
        return string.Format(CultureInfo.InvariantCulture, format, arguments);
    }

    public static string F(FormattableString value) => T(value.Format, value.GetArguments());

    /// <summary>Keep generated UI labels live; assigning a real caption's Text replaces this binding.</summary>
    public static void Bind(DependencyObject target, DependencyProperty property, string source, params object?[] arguments)
    {
        // Services may already have localized a fixed exception message. Retain
        // its canonical key so the displayed error follows later UI switches.
        // This applies only to UI bindings, never captured/translated speech.
        if (!Entries.ContainsKey(source))
        {
            string? canonical = null;
            foreach (Entry entry in Entries.Values)
            {
                if (source != entry.En && source != entry.Zh && source != entry.Ja) continue;
                if (canonical != null) { canonical = null; break; }
                canonical = entry.Source;
            }
            source = canonical ?? source;
        }
        BindingOperations.SetBinding(target, property, new Binding(nameof(LanguageSource.Revision))
        {
            Source = Source,
            Mode = BindingMode.OneWay,
            Converter = new LocalizedValue(source, arguments)
        });
    }

    public static string ResourceKey(string source) => "Ui." +
        System.Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(source)))[..16].ToLowerInvariant();

    private static IReadOnlyDictionary<string, Entry> LoadEntries()
    {
        var entries = new Dictionary<string, Entry>(StringComparer.Ordinal);
        var assembly = typeof(UiText).Assembly;
        var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
        foreach (string name in assembly.GetManifestResourceNames()
            .Where(name => name.Contains(".Localization.", StringComparison.Ordinal) && name.EndsWith(".json", StringComparison.Ordinal))
            .OrderBy(name => name, StringComparer.Ordinal))
        {
            using Stream stream = assembly.GetManifestResourceStream(name)!;
            foreach (Entry entry in JsonSerializer.Deserialize<Entry[]>(stream, options) ?? Array.Empty<Entry>())
                if (!string.IsNullOrEmpty(entry.Source)) entries[entry.Source] = entry;
        }
        return entries;
    }
}
