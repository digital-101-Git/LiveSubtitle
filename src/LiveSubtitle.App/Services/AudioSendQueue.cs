using System;
using System.Threading;
using System.Threading.Channels;

namespace LiveSubtitle.App.Services;

internal readonly record struct AudioPacket(byte[] Pcm, long StartSample)
{
    public long EndSample => StartSample + Pcm.Length / 2;
    public long MissingSamplesBefore(long previousEnd) => Math.Max(0, StartSample - previousEnd);
}

/// <summary>Keep recent PCM, retaining capture positions even when queued packets are dropped.</summary>
internal sealed class AudioSendQueue
{
    private readonly Channel<AudioPacket> _channel;
    private readonly object _captureGate = new();
    private long _capturedSamples;

    public AudioSendQueue(int capacity = 60)
    {
        _channel = Channel.CreateBounded<AudioPacket>(new BoundedChannelOptions(capacity)
        {
            SingleReader = true,
            FullMode = BoundedChannelFullMode.DropOldest
        });
    }

    public ChannelReader<AudioPacket> Reader => _channel.Reader;

    // Called before capture is admitted for a newly started session.
    public void ResetSequence()
    {
        lock (_captureGate) Interlocked.Exchange(ref _capturedSamples, 0);
    }

    public bool TryWrite(byte[] pcm)
    {
        // Capture currently has one callback thread. Keep allocation and enqueue
        // ordered if a future capture source invokes callbacks concurrently.
        lock (_captureGate)
        {
            long count = pcm.Length / 2;
            long start = Interlocked.Add(ref _capturedSamples, count) - count;
            return _channel.Writer.TryWrite(new AudioPacket(pcm, start));
        }
    }

    public void Complete() => _channel.Writer.TryComplete();
}
