using System;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace LiveSubtitle.App;

/// <summary>Caption contours with a one-device-pixel black outline.</summary>
public sealed class OutlinedCaptionText : Control
{
    public static readonly DependencyProperty TextProperty = DependencyProperty.Register(
        nameof(Text), typeof(string), typeof(OutlinedCaptionText),
        new FrameworkPropertyMetadata("", FrameworkPropertyMetadataOptions.AffectsMeasure |
            FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty FitToBoundsProperty = DependencyProperty.Register(
        nameof(FitToBounds), typeof(bool), typeof(OutlinedCaptionText),
        new FrameworkPropertyMetadata(true, FrameworkPropertyMetadataOptions.AffectsMeasure |
            FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty TextWrappingProperty = DependencyProperty.Register(
        nameof(TextWrapping), typeof(TextWrapping), typeof(OutlinedCaptionText),
        new FrameworkPropertyMetadata(TextWrapping.NoWrap, FrameworkPropertyMetadataOptions.AffectsMeasure |
            FrameworkPropertyMetadataOptions.AffectsRender));

    private Geometry? _geometry;
    private Rect _textBounds;
    private double _layoutWidth = double.NaN;

    public string Text { get => (string)GetValue(TextProperty); set => SetValue(TextProperty, value); }
    public bool FitToBounds { get => (bool)GetValue(FitToBoundsProperty); set => SetValue(FitToBoundsProperty, value); }
    public TextWrapping TextWrapping { get => (TextWrapping)GetValue(TextWrappingProperty); set => SetValue(TextWrappingProperty, value); }

    public OutlinedCaptionText()
    {
        Focusable = false;
        IsTabStop = false;
    }

    protected override void OnPropertyChanged(DependencyPropertyChangedEventArgs e)
    {
        base.OnPropertyChanged(e);
        if (e.Property == TextProperty || e.Property == FitToBoundsProperty || e.Property == TextWrappingProperty ||
            e.Property == FontFamilyProperty || e.Property == FontSizeProperty || e.Property == FontWeightProperty ||
            e.Property == FontStyleProperty || e.Property == FontStretchProperty ||
            e.Property == LanguageProperty || e.Property == FlowDirectionProperty)
        {
            InvalidateText();
        }
    }

    protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
    {
        base.OnDpiChanged(oldDpi, newDpi);
        InvalidateText();
    }

    private void InvalidateText()
    {
        _geometry = null;
        InvalidateMeasure();
        InvalidateVisual();
    }

    private void EnsureText(double availableWidth)
    {
        DpiScale dpi = VisualTreeHelper.GetDpi(this);
        double width = !FitToBounds && TextWrapping != TextWrapping.NoWrap && double.IsFinite(availableWidth)
            ? Math.Max(1, availableWidth - 2 / dpi.DpiScaleX)
            : double.PositiveInfinity;
        if (_geometry != null && _layoutWidth == width) return;
        _layoutWidth = width;
        if (string.IsNullOrWhiteSpace(Text))
        {
            _geometry = Geometry.Empty;
            _textBounds = Rect.Empty;
            return;
        }

        CultureInfo culture;
        try { culture = Language.GetSpecificCulture(); }
        catch (InvalidOperationException) { culture = CultureInfo.CurrentUICulture; }
        var formatted = new FormattedText(Text, culture, FlowDirection,
            new Typeface(FontFamily, FontStyle, FontWeight, FontStretch), FontSize,
            Brushes.White, dpi.PixelsPerDip);
        if (double.IsFinite(width))
        {
            formatted.MaxTextWidth = width;
            formatted.TextAlignment = TextAlignment.Center;
        }
        _geometry = formatted.BuildGeometry(new Point());
        if (_geometry.CanFreeze) _geometry.Freeze();

        // Keep line metrics as well as ink bounds: accents and overhang must not
        // clip, and different sentences must not stretch to different heights.
        _textBounds = new Rect(0, 0, double.IsFinite(width) ? width : formatted.WidthIncludingTrailingWhitespace,
            formatted.Height);
        _textBounds.Union(_geometry.Bounds);
    }

    protected override Size MeasureOverride(Size constraint)
    {
        EnsureText(constraint.Width);
        if (_textBounds.IsEmpty) return new Size();
        DpiScale dpi = VisualTreeHelper.GetDpi(this);
        var desired = new Size(_textBounds.Width + 2 / dpi.DpiScaleX, _textBounds.Height + 2 / dpi.DpiScaleY);
        // Report the fitted size to WPF as well; an oversized desired size can
        // otherwise make Arrange give this control a wider, clipped render area.
        return FitToBounds
            ? new Size(Math.Min(desired.Width, constraint.Width), Math.Min(desired.Height, constraint.Height))
            : desired;
    }

    protected override void OnRender(DrawingContext drawingContext)
    {
        base.OnRender(drawingContext);
        EnsureText(RenderSize.Width);
        if (_textBounds.IsEmpty || _geometry == null || _geometry.IsEmpty()) return;
        DpiScale dpi = VisualTreeHelper.GetDpi(this);
        double availableWidth = RenderSize.Width - 2 / dpi.DpiScaleX;
        double availableHeight = RenderSize.Height - 2 / dpi.DpiScaleY;
        if (availableWidth <= 0 || availableHeight <= 0) return;
        double scale = FitToBounds
            ? Math.Min(1, Math.Min(availableWidth / _textBounds.Width, availableHeight / _textBounds.Height))
            : 1;

        // Transform only the glyph contours, never the pen. A Viewbox around
        // this control would also scale the outline and is intentionally absent.
        double x = (RenderSize.Width - _textBounds.Width * scale) / 2 - _textBounds.X * scale;
        double y = (RenderSize.Height - _textBounds.Height * scale) / 2 - _textBounds.Y * scale;
        var geometry = new GeometryGroup();
        geometry.Children.Add(_geometry);
        geometry.Transform = new MatrixTransform(scale * dpi.DpiScaleX, 0, 0, scale * dpi.DpiScaleY,
            x * dpi.DpiScaleX, y * dpi.DpiScaleY);
        geometry.Freeze();

        // Draw in device pixels, including on different-DPI monitors. The
        // centered 2px stroke followed by the fill leaves a 1px outside border.
        var pen = new Pen(Brushes.Black, 2) { LineJoin = PenLineJoin.Round };
        pen.Freeze();
        drawingContext.PushTransform(new ScaleTransform(1 / dpi.DpiScaleX, 1 / dpi.DpiScaleY));
        drawingContext.DrawGeometry(null, pen, geometry);
        drawingContext.DrawGeometry(Foreground, null, geometry);
        drawingContext.Pop();
    }
}
