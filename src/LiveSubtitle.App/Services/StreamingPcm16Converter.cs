using System;
using System.Buffers.Binary;
using System.Runtime.InteropServices;

namespace LiveSubtitle.App.Services;

internal readonly record struct CaptureWaveFormat(int SampleRate, int Channels, int BitsPerSample,
    int BlockAlign, bool IsFloat, uint ChannelMask = 0)
{
    internal static CaptureWaveFormat Read(IntPtr pointer)
    {
        if (pointer == IntPtr.Zero) throw new ArgumentNullException(nameof(pointer));
        var wave = Marshal.PtrToStructure<WaveFormatEx>(pointer);
        bool floating = wave.FormatTag == 3;
        uint mask = 0;
        if (wave.FormatTag == 0xfffe)
        {
            if (wave.ExtraSize < 22) throw new NotSupportedException(UiText.T("WAVEFORMATEXTENSIBLE 형식이 잘못되었습니다."));
            var subformat = Marshal.PtrToStructure<Guid>(IntPtr.Add(pointer, 24));
            if (subformat == new Guid("00000003-0000-0010-8000-00aa00389b71")) floating = true;
            else if (subformat != new Guid("00000001-0000-0010-8000-00aa00389b71"))
                throw new NotSupportedException(UiText.F($"지원하지 않는 오디오 하위 형식: {subformat}"));
            mask = unchecked((uint)Marshal.ReadInt32(pointer, 20));
        }
        else if (wave.FormatTag is not (1 or 3))
            throw new NotSupportedException(UiText.F($"지원하지 않는 오디오 형식: {wave.FormatTag}"));
        var result = new CaptureWaveFormat(checked((int)wave.SampleRate), wave.Channels,
            wave.BitsPerSample, wave.BlockAlign, floating, mask);
        result.Validate();
        return result;
    }

    internal void Validate()
    {
        if (SampleRate is < 1000 or > 768000 || Channels is < 1 or > 32)
            throw new NotSupportedException(UiText.F($"지원하지 않는 오디오 속도/채널: {SampleRate} Hz, {Channels} ch"));
        if (IsFloat ? BitsPerSample is not (32 or 64) : BitsPerSample is not (8 or 16 or 24 or 32))
            throw new NotSupportedException(UiText.F($"지원하지 않는 오디오 샘플: {(IsFloat ? "Float" : "PCM")} {BitsPerSample} bit"));
        if (BlockAlign < Channels * (BitsPerSample / 8))
            throw new NotSupportedException(UiText.T("오디오 프레임 크기가 잘못되었습니다."));
    }
}

/// <summary>
/// Packet-boundary-independent downmix and band-limited resampling.
/// Produces signed little-endian 16 kHz mono PCM in 100 ms chunks. Rational phase
/// arithmetic prevents long-run drift at rates such as 44.1 kHz. A symmetric
/// Blackman-windowed sinc removes aliases before downsampling, with about 2 ms
/// lookahead at input rates >= 16 kHz. Same-rate input bypasses the filter.
/// </summary>
internal sealed class StreamingPcm16Converter
{
    internal const int OutputRate = 16000;
    private const int ChunkBytes = OutputRate / 10 * 2;
    private readonly CaptureWaveFormat _format;
    private readonly Action<byte[]> _emit;
    private readonly double[] _weights;
    private byte[] _chunk = new byte[ChunkBytes];
    private int _chunkUsed;
    private readonly StreamingSincResampler? _resampler;

    internal double FilterLookaheadMilliseconds => _resampler?.LookaheadMilliseconds ?? 0;

    internal StreamingPcm16Converter(CaptureWaveFormat format, Action<byte[]> emit)
    {
        format.Validate();
        _format = format;
        _emit = emit ?? throw new ArgumentNullException(nameof(emit));
        if (format.SampleRate != OutputRate)
            _resampler = new StreamingSincResampler(format.SampleRate, OutputRate, WriteSample);
        _weights = new double[format.Channels];
        // Equal-energy-safe average; omit a declared LFE channel from speech mix.
        int maskChannels = System.Numerics.BitOperations.PopCount(format.ChannelMask);
        uint mask = maskChannels == format.Channels ? format.ChannelMask : 0;
        int count = 0;
        for (int channel = 0; channel < format.Channels; channel++)
        {
            bool lfe = false;
            if (mask != 0)
            {
                uint speaker = mask & unchecked(0u - mask);
                lfe = speaker == 0x8;
                mask &= mask - 1;
            }
            _weights[channel] = lfe ? 0 : 1;
            if (!lfe) count++;
        }
        // Unusual LFE-only endpoint: retain it instead of dropping all sound.
        if (count == 0) { Array.Fill(_weights, 1.0); count = format.Channels; }
        for (int channel = 0; channel < format.Channels; channel++) _weights[channel] /= count;
    }

    internal void ResetInterpolation()
    {
        // A WASAPI discontinuity is a real boundary. Recover the preceding
        // filter's known tail, then keep its samples out of the new segment.
        // Keep the 100 ms output packet intact; do not invent a missing duration.
        _resampler?.FinishSegment();
    }

    internal void Append(ReadOnlySpan<byte> input, int frames, bool silent = false)
    {
        if (frames < 0 || (!silent && input.Length < checked(frames * _format.BlockAlign)))
            throw new ArgumentException(UiText.T("오디오 패킷 길이가 프레임 수와 일치하지 않습니다."));
        int sampleBytes = _format.BitsPerSample / 8;
        for (int frame = 0; frame < frames; frame++)
        {
            double mono = 0;
            if (!silent)
            {
                int offset = frame * _format.BlockAlign;
                for (int channel = 0; channel < _format.Channels; channel++)
                {
                    double sample = ReadSample(input.Slice(offset + channel * sampleBytes, sampleBytes));
                    if (double.IsFinite(sample)) mono += sample * _weights[channel];
                }
            }
            mono = Math.Clamp(mono, -1, 1);
            if (_resampler is null) WriteSample(mono);
            else _resampler.Append(mono);
        }
    }

    private double ReadSample(ReadOnlySpan<byte> sample)
    {
        if (_format.IsFloat)
            return _format.BitsPerSample == 32
                ? BitConverter.Int32BitsToSingle(BinaryPrimitives.ReadInt32LittleEndian(sample))
                : BitConverter.Int64BitsToDouble(BinaryPrimitives.ReadInt64LittleEndian(sample));
        return _format.BitsPerSample switch
        {
            8 => (sample[0] - 128) / 128.0,
            16 => BinaryPrimitives.ReadInt16LittleEndian(sample) / 32768.0,
            // Shift into a signed 32-bit container for correct 24-bit sign extension.
            24 => ((sample[0] << 8 | sample[1] << 16 | sample[2] << 24) >> 8) / 8388608.0,
            // WAVEFORMATEXTENSIBLE valid bits are left-aligned in the container.
            32 => BinaryPrimitives.ReadInt32LittleEndian(sample) / 2147483648.0,
            _ => throw new NotSupportedException()
        };
    }

    private void WriteSample(double sample)
    {
        int scaled = (int)Math.Round(Math.Clamp(sample, -1, 1) * 32768.0);
        short value = (short)Math.Clamp(scaled, short.MinValue, short.MaxValue);
        BinaryPrimitives.WriteInt16LittleEndian(_chunk.AsSpan(_chunkUsed, 2), value);
        _chunkUsed += 2;
        if (_chunkUsed == _chunk.Length)
        {
            var complete = _chunk;
            _chunk = new byte[ChunkBytes];
            _chunkUsed = 0;
            _emit(complete);
        }
    }

    // Complete a finite segment using edge extension, without adding padding
    // samples to its duration. Live capture does not call this on user stop.
    internal void Flush()
    {
        _resampler?.FinishSegment();
        if (_chunkUsed == 0) return;
        var tail = _chunk.AsSpan(0, _chunkUsed).ToArray();
        _chunk = new byte[ChunkBytes];
        _chunkUsed = 0;
        _emit(tail);
    }
}

/// <summary>
/// Small streaming polyphase FIR. State is independent of Append packet sizes.
/// Output position uses integer input-rate/output-rate arithmetic; coefficient
/// phases are exact for common rates (48k, 44.1k, 22.05k), capped at 1024 phases
/// for unusual rates. That cap approximates coefficients, never sample timing.
/// </summary>
internal sealed class StreamingSincResampler
{
    private readonly int _inputRate, _outputRate, _radius, _phaseCount, _mask;
    private readonly double[][] _coefficients;
    private readonly double[] _samples;
    private readonly Action<double> _emit;
    private long _inputIndex = -1, _nextOutputNumerator;
    private double _first, _last;

    internal double LookaheadMilliseconds => 1000.0 * _radius / _inputRate;

    internal StreamingSincResampler(int inputRate, int outputRate, Action<double> emit)
    {
        _inputRate = inputRate; _outputRate = outputRate; _emit = emit;
        // 32 output-periods per half-kernel: 193 taps / 2 ms at 48 -> 16 kHz.
        _radius = (int)Math.Ceiling(32.0 * Math.Max(1, (double)inputRate / outputRate));
        _phaseCount = Math.Min(1024, outputRate / Gcd(inputRate, outputRate));
        int capacity = 1;
        while (capacity < 2 * _radius + 2) capacity *= 2;
        _samples = new double[capacity]; _mask = capacity - 1;
        _coefficients = new double[_phaseCount][];
        double cutoff = .45 * Math.Min(1, (double)outputRate / inputRate);
        for (int phase = 0; phase < _phaseCount; phase++)
        {
            var taps = new double[2 * _radius + 1];
            double fraction = (double)phase / _phaseCount, sum = 0;
            for (int index = 0; index < taps.Length; index++)
            {
                double distance = index - _radius - fraction;
                if (Math.Abs(distance) > _radius) continue;
                double angle = Math.PI * distance / _radius;
                double window = .42 + .5 * Math.Cos(angle) + .08 * Math.Cos(2 * angle);
                double value = Math.Abs(distance) < 1e-12 ? 2 * cutoff :
                    Math.Sin(2 * Math.PI * cutoff * distance) / (Math.PI * distance);
                taps[index] = value * window; sum += taps[index];
            }
            for (int index = 0; index < taps.Length; index++) taps[index] /= sum;
            _coefficients[phase] = taps;
        }
    }

    internal void Append(double sample)
    {
        _inputIndex++;
        if (_inputIndex == 0) _first = sample;
        _last = sample;
        _samples[(int)(_inputIndex & _mask)] = sample;
        while (_nextOutputNumerator / _outputRate + _radius <= _inputIndex)
            EmitNext();
    }

    internal void FinishSegment()
    {
        // Match the established sample-grid convention, including upsampling:
        // floor((inputFrames - 1) * outputRate / inputRate) + 1 samples.
        // Only the FIR lookahead is extended; no trailing silent audio is added.
        if (_inputIndex >= 0)
            while (_nextOutputNumerator <= _inputIndex * _outputRate) EmitNext();
        _inputIndex = -1; _nextOutputNumerator = 0; _first = _last = 0;
        Array.Clear(_samples);
    }

    private void EmitNext()
    {
        long center = _nextOutputNumerator / _outputRate;
        int phase = (int)((_nextOutputNumerator % _outputRate) * _phaseCount / _outputRate);
        double[] taps = _coefficients[phase];
        long start = center - _radius;
        double value = 0;
        for (int index = 0; index < taps.Length; index++)
        {
            long position = start + index;
            double sample = position < 0 ? _first : position > _inputIndex ? _last :
                _samples[(int)(position & _mask)];
            value += sample * taps[index];
        }
        _emit(value);
        _nextOutputNumerator += _inputRate;
    }

    private static int Gcd(int a, int b)
    {
        while (b != 0) { int remainder = a % b; a = b; b = remainder; }
        return a;
    }
}
