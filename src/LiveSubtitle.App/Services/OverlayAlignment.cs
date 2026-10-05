using System;

namespace LiveSubtitle.App.Services;

/// <summary>Physical pixel coordinates in the Windows virtual desktop.</summary>
public readonly record struct PixelBounds(int X, int Y, int Width, int Height);

public enum HorizontalAnchor
{
    Browser,
    Monitor
}

/// <summary>Stateless geometry used when an overlay drag has finished.</summary>
public static class OverlayAlignment
{
    /// <summary>
    /// Chooses the browser only when the overlay's center is inside its valid
    /// client rectangle. Left/top edges are included; right/bottom are excluded.
    /// Call once before moving the overlay and retain the selected anchor.
    /// </summary>
    public static HorizontalAnchor DetermineAnchor(PixelBounds overlay, PixelBounds? browser)
    {
        if (overlay.Width <= 0 || overlay.Height <= 0 ||
            browser is not { Width: > 0, Height: > 0 } bounds)
            return HorizontalAnchor.Monitor;

        // Doubled coordinates preserve half-pixel centers, including negatives.
        // Convert before arithmetic so virtual-desktop coordinates cannot overflow.
        long centerX2 = 2L * overlay.X + overlay.Width;
        long centerY2 = 2L * overlay.Y + overlay.Height;
        bool inside = centerX2 >= 2L * bounds.X &&
                      centerX2 < 2L * ((long)bounds.X + bounds.Width) &&
                      centerY2 >= 2L * bounds.Y &&
                      centerY2 < 2L * ((long)bounds.Y + bounds.Height);
        return inside ? HorizontalAnchor.Browser : HorizontalAnchor.Monitor;
    }

    /// <summary>
    /// Returns a centered physical-pixel X coordinate without resizing the overlay.
    /// A half-pixel tie rounds left, including when the overlay is wider than its
    /// container. The caller supplies valid positive widths and retains Y itself.
    /// </summary>
    public static int CenteredLeft(PixelBounds overlay, PixelBounds container)
    {
        long difference = (long)container.Width - overlay.Width;
        long offset = difference >= 0 ? difference / 2 : (difference - 1) / 2;
        long left = (long)container.X + offset;
        return (int)Math.Clamp(left, int.MinValue, int.MaxValue);
    }

    public static bool FitsVertically(PixelBounds overlay, PixelBounds browser) =>
        overlay.Y >= browser.Y && (long)overlay.Y + overlay.Height <= (long)browser.Y + browser.Height;

    public static int BottomGap(PixelBounds overlay, PixelBounds browser) =>
        (int)Math.Clamp((long)browser.Y + browser.Height - overlay.Y - overlay.Height, 0, int.MaxValue);

    /// <summary>Keep the preferred size and bottom gap, limiting only what cannot fit.</summary>
    public static PixelBounds InsideBrowser(PixelBounds preferred, PixelBounds browser, int bottomGap)
    {
        int width = Math.Min(preferred.Width, browser.Width);
        int height = Math.Min(preferred.Height, browser.Height);
        int gap = Math.Clamp(bottomGap, 0, Math.Max(0, browser.Height - height));
        return new PixelBounds(CenteredLeft(preferred with { Width = width }, browser),
            (int)Math.Clamp((long)browser.Y + browser.Height - height - gap, int.MinValue, int.MaxValue), width, height);
    }

    public static PixelBounds OnMonitor(PixelBounds preferred, PixelBounds monitor)
    {
        int bottomOffset = (int)Math.Max(1, (long)monitor.Height * 95 / 100);
        int width = Math.Min(preferred.Width, monitor.Width);
        int height = Math.Min(preferred.Height, bottomOffset);
        return new PixelBounds(CenteredLeft(preferred with { Width = width }, monitor),
            (int)Math.Clamp((long)monitor.Y + bottomOffset - height, int.MinValue, int.MaxValue), width, height);
    }
}
