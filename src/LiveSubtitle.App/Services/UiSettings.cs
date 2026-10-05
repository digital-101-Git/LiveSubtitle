using System;
using System.IO;
using System.Text.Json;

namespace LiveSubtitle.App.Services;

public sealed class UiSettings
{
    public const double DefaultOverlayWidth = 1000;
    public const double DefaultOverlayHeight = 180;
    // UI language is independent of transcription and translation languages.
    public string UiLanguage { get; set; } = "ko";
    public bool ShowOverlay { get; set; } = true;
    public bool Bilingual { get; set; } = false;
    public bool FollowWindow { get; set; } = true;
    public double FontSize { get; set; } = 30;
    public double BackgroundOpacity { get; set; } = .74;
    public double OverlayLeft { get; set; } = 150;
    public double OverlayTop { get; set; } = 650;
    public double OverlayWidth { get; set; } = DefaultOverlayWidth;
    public double OverlayHeight { get; set; } = DefaultOverlayHeight;
    public string OverlayAnchorMode { get; set; } = "Browser";
    // A completed manual drag/resize owns the physical desktop position until
    // either placement preset is selected again.
    public bool OverlayManualPosition { get; set; }
    public int? OverlayPixelLeft { get; set; }
    public int? OverlayPixelTop { get; set; }
    public int? OverlayPixelWidth { get; set; }
    public int? OverlayPixelHeight { get; set; }
    public int? OverlayBrowserBottomGap { get; set; }
    // Preserve the user's size when a small browser temporarily limits the window.
    public int? OverlayPreferredPixelWidth { get; set; }
    public int? OverlayPreferredPixelHeight { get; set; }

    public static UiSettings Load(string root)
    {
        try
        {
            string path = Path.Combine(root, "config", "ui-settings.json");
            if (!File.Exists(path)) return new UiSettings();
            string json = File.ReadAllText(path);
            var settings = JsonSerializer.Deserialize<UiSettings>(json) ?? new UiSettings();
            using var document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind == JsonValueKind.Object &&
                !document.RootElement.TryGetProperty(nameof(OverlayManualPosition), out _))
            {
                // One-time migration from automatic centering to the requested
                // browser default. New explicit presets/manual placements persist.
                settings.OverlayAnchorMode = "Browser";
                settings.OverlayBrowserBottomGap = null;
            }
            return settings;
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException) { return new UiSettings(); }
    }

    public void Save(string root)
    {
        string directory = Path.Combine(root, "config"); Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, "ui-settings.json");
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(temporary, path, true);
    }
}
