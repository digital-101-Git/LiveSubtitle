using System;
using System.Collections.Generic;
using System.Globalization;

namespace LiveSubtitle.App.Services;

public sealed record DisplayCaption(string? Id, string Translation, string Source);

public sealed class CaptionQueueOverflowException : InvalidOperationException
{
    public CaptionQueueOverflowException() : base(UiText.T("표시할 자막이 60개를 초과해 중지했습니다. 문장을 건너뛰지 않도록 자막을 다시 시작하세요.")) { }
}

// Used only on the UI thread. Time is monotonic milliseconds, supplied by the
// caller so delayed dispatcher ticks never skip unseen captions to catch up.
public sealed class CaptionDisplayQueue
{
    public const int MaximumPending = 60;
    public const long MinimumDisplayMilliseconds = 650;
    public const long MaximumDisplayMilliseconds = 3500;
    private readonly LinkedList<DisplayCaption> _pending = new();
    private readonly Dictionary<string, LinkedListNode<DisplayCaption>> _pendingById = new(StringComparer.Ordinal);
    private readonly HashSet<string> _seenIds = new(StringComparer.Ordinal);
    private long _shownSince;
    private long _baseDisplayMilliseconds;
    private long _previousShownSince;
    private long _previousBaseDisplayMilliseconds;

    public DisplayCaption? Current { get; private set; }
    public DisplayCaption? Previous { get; private set; }
    public IReadOnlyList<DisplayCaption> VisibleCaptions =>
        Current == null ? Array.Empty<DisplayCaption>() :
        Previous == null ? Array.AsReadOnly(new[] { Current }) :
        Array.AsReadOnly(new[] { Previous, Current });
    public int PendingCount => _pending.Count;
    public long CurrentDisplayMilliseconds => Current == null ? 0 :
        ApplyBacklogPressure(_baseDisplayMilliseconds, _pending.Count);
    public long PreviousDisplayMilliseconds => Previous == null ? 0 :
        ApplyBacklogPressure(_previousBaseDisplayMilliseconds, _pending.Count);

    // A second row is free space, so it needs no delay. Once both rows are
    // occupied, only the row about to leave must finish its reading time.
    // Its original first-appearance clock survives Current -> Previous.
    public long MillisecondsUntilAdvance(long now) => Previous == null ? 0 :
        Math.Max(0, PreviousDisplayMilliseconds - (now - _previousShownSince));

    // Count visible text elements rather than UTF-16 units: a combined accent
    // or emoji is one element, and whitespace does not add reading time.
    internal static long CalculateDisplayMilliseconds(string translation, int pendingCount = 0)
    {
        long duration = 500;
        var elements = StringInfo.GetTextElementEnumerator(translation);
        while (duration < MaximumDisplayMilliseconds && elements.MoveNext())
            if (!string.IsNullOrWhiteSpace(elements.GetTextElement())) duration += 75;
        return ApplyBacklogPressure(Math.Clamp(duration, MinimumDisplayMilliseconds,
            MaximumDisplayMilliseconds), pendingCount);
    }

    private static long ApplyBacklogPressure(long duration, int pendingCount)
    {
        // Speed up gently during a burst, never evict or fast-forward captions.
        int percent = pendingCount >= 6 ? 80 : pendingCount >= 3 ? 90 : 100;
        return Math.Max(MinimumDisplayMilliseconds, duration * percent / 100);
    }

    // A non-null return signals a visible change. Read VisibleCaptions for both
    // rows: a correction to Previous returns Current without changing row order.
    public DisplayCaption? Enqueue(string translation, string source, string? id, long now)
    {
        id = string.IsNullOrEmpty(id) ? null : id;
        var caption = new DisplayCaption(id, translation, source);
        if (id != null)
        {
            // A revision replaces the text without restarting its reading clock.
            if (Current?.Id == id) return Current = caption;
            if (Previous?.Id == id) { Previous = caption; return Current; }
            if (_pendingById.TryGetValue(id, out var node)) { node.Value = caption; return null; }
            if (_seenIds.Contains(id)) return null;
        }
        // A hidden-history reset keeps unseen queued captions. Promote the oldest
        // before accepting a fresh ID, so that reset cannot reverse their order.
        DisplayCaption? visible = Current == null && _pending.Count > 0 ? ShowNext(now) : null;
        if (Current == null || (_pending.Count == 0 && MillisecondsUntilAdvance(now) == 0))
        {
            if (id != null) _seenIds.Add(id);
            return Show(caption, now);
        }
        if (_pending.Count >= MaximumPending) throw new CaptionQueueOverflowException();
        var added = _pending.AddLast(caption);
        if (id != null) { _pendingById.Add(id, added); _seenIds.Add(id); }
        return visible;
    }

    public DisplayCaption? Advance(long now)
    {
        if (_pending.Count == 0 || MillisecondsUntilAdvance(now) > 0) return null;
        return ShowNext(now);
    }

    // Removing either row is immediate. Replacing the bottom row preserves the
    // top row; with no pending item, the top row becomes the sole visible caption.
    public bool Remove(string id, long now)
    {
        _seenIds.Add(id);
        if (_pendingById.Remove(id, out var node)) _pending.Remove(node);
        if (Previous?.Id == id)
        {
            Previous = null; _previousShownSince = 0; _previousBaseDisplayMilliseconds = 0;
            return true;
        }
        if (Current?.Id != id) return false;
        if (_pending.Count > 0) ShowNext(now, preservePrevious: true);
        else
        {
            Current = Previous; Previous = null;
            _shownSince = _previousShownSince;
            _baseDisplayMilliseconds = _previousBaseDisplayMilliseconds;
            _previousShownSince = 0; _previousBaseDisplayMilliseconds = 0;
        }
        return true;
    }

    private DisplayCaption ShowNext(long now, bool preservePrevious = false)
    {
        var node = _pending.First!;
        _pending.RemoveFirst();
        if (node.Value.Id != null) _pendingById.Remove(node.Value.Id);
        return Show(node.Value, now, preservePrevious);
    }

    private DisplayCaption Show(DisplayCaption caption, long now, bool preservePrevious = false)
    {
        if (!preservePrevious)
        {
            Previous = Current;
            _previousShownSince = _shownSince;
            _previousBaseDisplayMilliseconds = _baseDisplayMilliseconds;
        }
        _shownSince = now;
        _baseDisplayMilliseconds = CalculateDisplayMilliseconds(caption.Translation);
        return Current = caption;
    }

    public void Clear()
    {
        _pending.Clear(); _pendingById.Clear(); _seenIds.Clear();
        ClearVisibleHistory();
    }

    // Call before a fresh caption after auto-hide. Old IDs remain suppressed,
    // while already queued, not-yet-displayed captions retain their FIFO order.
    public void ClearVisibleHistory()
    {
        Current = null; Previous = null;
        _shownSince = 0; _baseDisplayMilliseconds = 0;
        _previousShownSince = 0; _previousBaseDisplayMilliseconds = 0;
    }
}
