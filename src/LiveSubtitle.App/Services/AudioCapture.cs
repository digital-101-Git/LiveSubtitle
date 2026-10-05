using System;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;

namespace LiveSubtitle.App.Services;

/// <summary>
/// WASAPI capture. A PID captures that process and its descendants; a capture
/// endpoint ID captures that microphone. Both null explicitly request the default
/// render endpoint's system mix. Never substitutes a different source on failure.
/// Events run on the capture thread.
/// </summary>
public sealed class AudioCapture : IDisposable
{
    public event Action<byte[]>? PcmAvailable;
    public event Action<string>? Error;

    private readonly object _gate = new();
    private Thread? _thread;
    private CancellationTokenSource? _cancellation;
    private bool _disposed;

    public void Start(int? processId) => Start(processId, null);

    public void Start(int? processId, string? captureDeviceId)
    {
        if (processId is <= 0)
            throw new ArgumentOutOfRangeException(nameof(processId));
        if (captureDeviceId is not null && string.IsNullOrWhiteSpace(captureDeviceId))
            throw new ArgumentException(UiText.T("마이크 장치 ID가 비어 있습니다."), nameof(captureDeviceId));
        if (processId.HasValue && captureDeviceId is not null)
            throw new ArgumentException(UiText.T("브라우저 프로세스와 마이크를 동시에 선택할 수 없습니다."), nameof(captureDeviceId));

        var ready = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        CancellationTokenSource cancellation;
        lock (_gate)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (_thread is not null)
                throw new InvalidOperationException(UiText.T("오디오 캡처가 이미 실행 중입니다."));
            cancellation = new CancellationTokenSource();
            _cancellation = cancellation;
            _thread = new Thread(() => CaptureLoop(processId, captureDeviceId, cancellation, ready))
            {
                IsBackground = true,
                Name = "LiveSubtitle WASAPI capture"
            };
            _thread.SetApartmentState(ApartmentState.MTA);
            _thread.Start();
        }

        try
        {
            ready.Task.WaitAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult();
        }
        catch
        {
            // Worker owns disposal, including when startup failed before this wait.
            lock (_gate)
                if (ReferenceEquals(_cancellation, cancellation)) cancellation.Cancel();
            throw;
        }
    }

    public void Stop()
    {
        Thread? thread;
        lock (_gate)
        {
            thread = _thread;
            _cancellation?.Cancel();
        }
        if (thread is not null && thread != Thread.CurrentThread && !thread.Join(6000))
            ReportError(UiText.T("오디오 캡처 종료를 기다리는 중입니다. 새 캡처는 종료 후 시작할 수 있습니다."));
    }

    public void Dispose()
    {
        lock (_gate) _disposed = true;
        Stop();
        GC.SuppressFinalize(this);
    }

    private void CaptureLoop(int? processId, string? captureDeviceId, CancellationTokenSource cancellation,
        TaskCompletionSource ready)
    {
        IAudioClient? client = null;
        IAudioCaptureClient? capture = null;
        IntPtr formatPointer = IntPtr.Zero;
        bool comInitialized = false;
        bool started = false;
        StreamingPcm16Converter? converter = null;
        using var sampleReady = new AutoResetEvent(false);
        try
        {
            NativeAudio.Check(NativeAudio.CoInitializeEx(IntPtr.Zero, 0), UiText.T("COM 초기화"));
            comInitialized = true;
            cancellation.Token.ThrowIfCancellationRequested();

            uint flags = NativeAudio.EventCallback;
            if (captureDeviceId is not null)
            {
                client = AudioInputDevices.Activate(captureDeviceId);
                NativeAudio.Check(client.GetMixFormat(out formatPointer), UiText.T("마이크 장치 형식 읽기"));
            }
            else if (processId.HasValue)
            {
                flags |= NativeAudio.Loopback;
                if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 20348))
                    throw new PlatformNotSupportedException(UiText.T("프로세스 오디오 캡처에는 Windows 빌드 20348 이상이 필요합니다."));
                client = ActivateProcess(processId.Value, cancellation.Token);
                // The virtual process device can return E_NOTIMPL for GetMixFormat.
                // Request a concrete shared-mode format, as in Microsoft's sample.
                var requested = new WaveFormatEx
                {
                    FormatTag = 1, Channels = 2, SampleRate = 48000,
                    AverageBytesPerSecond = 192000, BlockAlign = 4, BitsPerSample = 16,
                    ExtraSize = 0
                };
                formatPointer = Marshal.AllocCoTaskMem(Marshal.SizeOf<WaveFormatEx>());
                Marshal.StructureToPtr(requested, formatPointer, false);
                flags |= NativeAudio.AutoConvertPcm | NativeAudio.SrcDefaultQuality;
            }
            else
            {
                flags |= NativeAudio.Loopback;
                client = ActivateDefaultRenderDevice();
                NativeAudio.Check(client.GetMixFormat(out formatPointer), UiText.T("오디오 장치 형식 읽기"));
            }

            var format = CaptureWaveFormat.Read(formatPointer);
            converter = new StreamingPcm16Converter(format, bytes => PcmAvailable?.Invoke(bytes));
            NativeAudio.Check(client.Initialize(0, flags, 0, 0, formatPointer, IntPtr.Zero), UiText.T("오디오 캡처 초기화"));
            NativeAudio.Check(client.SetEventHandle(sampleReady.SafeWaitHandle.DangerousGetHandle()), UiText.T("오디오 이벤트 설정"));
            var captureId = typeof(IAudioCaptureClient).GUID;
            NativeAudio.Check(client.GetService(ref captureId, out var captureObject), UiText.T("오디오 캡처 서비스 열기"));
            capture = (IAudioCaptureClient)captureObject;
            cancellation.Token.ThrowIfCancellationRequested();
            NativeAudio.Check(client.Start(), UiText.T("오디오 캡처 시작"));
            started = true;
            ready.TrySetResult();

            WaitHandle[] waits = [cancellation.Token.WaitHandle, sampleReady];
            byte[] packet = Array.Empty<byte>();
            while (!cancellation.IsCancellationRequested)
            {
                // Timeout also drains packets if a driver coalesces/losses events.
                if (WaitHandle.WaitAny(waits, 50) == 0) break;
                NativeAudio.Check(capture.GetNextPacketSize(out var available), UiText.T("오디오 패킷 확인"));
                while (available != 0 && !cancellation.IsCancellationRequested)
                {
                    NativeAudio.Check(capture.GetBuffer(out var data, out var frames, out var bufferFlags,
                        out _, out _), UiText.T("오디오 패킷 읽기"));
                    try
                    {
                        int frameCount = checked((int)frames);
                        int byteCount = checked(frameCount * format.BlockAlign);
                        bool silent = (bufferFlags & NativeAudio.Silent) != 0;
                        if ((bufferFlags & NativeAudio.DataDiscontinuity) != 0)
                            converter.ResetInterpolation();
                        if (!silent)
                        {
                            if (data == IntPtr.Zero) throw new InvalidOperationException(UiText.T("오디오 패킷 주소가 비어 있습니다."));
                            if (packet.Length < byteCount) packet = new byte[byteCount];
                            Marshal.Copy(data, packet, 0, byteCount);
                        }
                        converter.Append(packet.AsSpan(0, silent ? 0 : byteCount), frameCount, silent);
                    }
                    finally
                    {
                        NativeAudio.Check(capture.ReleaseBuffer(frames), UiText.T("오디오 패킷 반환"));
                    }
                    NativeAudio.Check(capture.GetNextPacketSize(out available), UiText.T("다음 오디오 패킷 확인"));
                }
            }
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            ready.TrySetCanceled(cancellation.Token);
        }
        catch (Exception ex)
        {
            Exception failure = captureDeviceId is not null
                ? new InvalidOperationException(UiText.F($"선택한 마이크에서 소리를 가져올 수 없습니다. 연결 상태와 Windows 마이크 접근 권한을 확인하세요. {UiText.T(ex.Message)}"), ex)
                : ex;
            // Start reports initialization failures to its caller; Error is for
            // failures after Start has returned successfully, avoiding duplicates.
            if (!ready.TrySetException(failure))
                ReportError(UiText.F($"오디오 캡처가 중단되었습니다: {UiText.T(failure.Message)}"));
        }
        finally
        {
            if (started && client is not null) _ = client.Stop();
            NativeAudio.Release(capture);
            NativeAudio.Release(client);
            if (formatPointer != IntPtr.Zero) Marshal.FreeCoTaskMem(formatPointer);
            if (comInitialized) NativeAudio.CoUninitialize();
            // A stop is a boundary; do not send a late partial chunk to the next session.
            lock (_gate)
            {
                if (ReferenceEquals(_cancellation, cancellation))
                {
                    _thread = null;
                    _cancellation = null;
                }
                cancellation.Dispose();
            }
        }
    }

    private static IAudioClient ActivateDefaultRenderDevice()
    {
        IMMDeviceEnumerator? enumerator = null;
        IMMDevice? device = null;
        try
        {
            enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            NativeAudio.Check(enumerator.GetDefaultAudioEndpoint(0, 1, out device), UiText.T("기본 출력 장치 찾기"));
            var id = typeof(IAudioClient).GUID;
            NativeAudio.Check(device.Activate(ref id, 23, IntPtr.Zero, out var audio), UiText.T("출력 장치 캡처 열기"));
            return (IAudioClient)audio;
        }
        finally
        {
            NativeAudio.Release(device);
            NativeAudio.Release(enumerator);
        }
    }

    private static IAudioClient ActivateProcess(int processId, CancellationToken cancellation)
    {
        var callback = new AudioActivationCompletion(processId);
        IntPtr operation = IntPtr.Zero;
        bool initiated = false;
        try
        {
            var clientId = typeof(IAudioClient).GUID;
            NativeAudio.Check(NativeAudio.ActivateAudioInterfaceAsync("VAD\\Process_Loopback",
                ref clientId, callback.Parameters, callback, out operation), UiText.T("프로세스 오디오 캡처 열기"));
            initiated = true;
            callback.Wait(cancellation);
            IntPtr audio = callback.TakeResult();
            try { return (IAudioClient)Marshal.GetObjectForIUnknown(audio); }
            finally { Marshal.Release(audio); }
        }
        finally
        {
            // Windows retains the callback until completion. On cancellation its
            // unmanaged activation parameters remain owned by the callback.
            callback.Abandon(initiated);
            if (operation != IntPtr.Zero) Marshal.Release(operation);
            GC.KeepAlive(callback);
        }
    }

    private void ReportError(string message)
    {
        try { Error?.Invoke(message); }
        catch { /* An error subscriber must not crash a native capture thread. */ }
    }
}

/// <summary>COM callback is agile: Windows invokes it on an MTA worker.</summary>
[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public sealed class AudioActivationCompletion : IAudioActivationCompletionHandler, IAudioAgileObject
{
    private readonly object _gate = new();
    private readonly TaskCompletionSource _done = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private IntPtr _blob;
    private IntPtr _variant;
    private IntPtr _audio;
    private int _result;
    private bool _completed;
    private bool _abandoned;
    internal IntPtr Parameters => _variant;

    internal AudioActivationCompletion(int processId)
    {
        // AUDIOCLIENT_ACTIVATION_PARAMS: enum (4), DWORD PID (4), mode (4).
        _blob = Marshal.AllocCoTaskMem(12);
        Marshal.WriteInt32(_blob, 0, 1); // PROCESS_LOOPBACK
        Marshal.WriteInt32(_blob, 4, processId);
        Marshal.WriteInt32(_blob, 8, 0); // INCLUDE_TARGET_PROCESS_TREE
        // PROPVARIANT union is at offset 8; BLOB pointer is aligned to pointer size.
        int size = IntPtr.Size == 8 ? 24 : 16;
        _variant = Marshal.AllocCoTaskMem(size);
        Marshal.Copy(new byte[size], 0, _variant, size);
        Marshal.WriteInt16(_variant, 0, 65); // VT_BLOB
        Marshal.WriteInt32(_variant, 8, 12);
        Marshal.WriteIntPtr(_variant, IntPtr.Size == 8 ? 16 : 12, _blob);
    }

    public int ActivateCompleted(IAudioActivationOperation operation)
    {
        IntPtr audio = IntPtr.Zero;
        int result;
        try
        {
            int callResult = operation.GetActivateResult(out result, out audio);
            if (callResult < 0) result = callResult;
        }
        catch (Exception ex) { result = ex.HResult; }
        lock (_gate)
        {
            _result = result;
            _completed = true;
            if (_abandoned || result < 0)
            {
                if (audio != IntPtr.Zero) Marshal.Release(audio);
            }
            else _audio = audio;
            FreeParameters();
        }
        _done.TrySetResult();
        return 0;
    }

    internal void Wait(CancellationToken cancellation) =>
        _done.Task.WaitAsync(TimeSpan.FromSeconds(4), cancellation).GetAwaiter().GetResult();

    internal IntPtr TakeResult()
    {
        lock (_gate)
        {
            NativeAudio.Check(_result, UiText.T("프로세스 오디오 활성화"));
            if (_audio == IntPtr.Zero) throw new InvalidOperationException(UiText.T("프로세스 오디오 인터페이스가 없습니다."));
            var result = _audio;
            _audio = IntPtr.Zero;
            return result;
        }
    }

    internal void Abandon(bool initiated)
    {
        lock (_gate)
        {
            _abandoned = true;
            if (_audio != IntPtr.Zero) { Marshal.Release(_audio); _audio = IntPtr.Zero; }
            if (_completed || !initiated) FreeParameters();
        }
    }

    private void FreeParameters()
    {
        if (_variant != IntPtr.Zero) { Marshal.FreeCoTaskMem(_variant); _variant = IntPtr.Zero; }
        if (_blob != IntPtr.Zero) { Marshal.FreeCoTaskMem(_blob); _blob = IntPtr.Zero; }
    }
}

internal static class NativeAudio
{
    internal const uint Loopback = 0x00020000, EventCallback = 0x00040000;
    internal const uint AutoConvertPcm = 0x80000000, SrcDefaultQuality = 0x08000000;
    internal const uint DataDiscontinuity = 1, Silent = 2;

    [DllImport("ole32.dll", ExactSpelling = true)]
    internal static extern int CoInitializeEx(IntPtr reserved, uint apartment);
    [DllImport("ole32.dll", ExactSpelling = true)]
    internal static extern void CoUninitialize();
    [DllImport("Mmdevapi.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    internal static extern int ActivateAudioInterfaceAsync(string deviceInterfacePath,
        ref Guid interfaceId, IntPtr activationParams,
        IAudioActivationCompletionHandler completionHandler, out IntPtr activationOperation);

    internal static void Check(int hr, string operation)
    {
        if (hr < 0) throw new COMException(UiText.F($"{operation} 실패 (0x{hr:X8}): {Marshal.GetExceptionForHR(hr)?.Message}"), hr);
    }

    internal static void Release(object? value)
    {
        if (value is not null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value);
    }
}

[StructLayout(LayoutKind.Sequential, Pack = 2)]
internal struct WaveFormatEx
{
    public ushort FormatTag, Channels;
    public uint SampleRate, AverageBytesPerSecond;
    public ushort BlockAlign, BitsPerSample, ExtraSize;
}

[ComVisible(true), Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioActivationCompletionHandler
{
    [PreserveSig] int ActivateCompleted(IAudioActivationOperation operation);
}

[ComVisible(true), Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioAgileObject { }

[ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioActivationOperation
{
    [PreserveSig] int GetActivateResult(out int activateResult, out IntPtr activatedInterface);
}

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), ClassInterface(ClassInterfaceType.None)]
internal class MMDeviceEnumerator { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceEnumerator
{
    [PreserveSig] int EnumAudioEndpoints(int flow, uint mask, out IMMDeviceCollection devices);
    [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice endpoint);
    [PreserveSig] int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device);
    [PreserveSig] int RegisterEndpointNotificationCallback(IntPtr client);
    [PreserveSig] int UnregisterEndpointNotificationCallback(IntPtr client);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDevice
{
    [PreserveSig] int Activate(ref Guid id, uint context, IntPtr parameters,
        [MarshalAs(UnmanagedType.IUnknown)] out object instance);
    [PreserveSig] int OpenPropertyStore(uint mode, out IAudioPropertyStore properties);
    [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    [PreserveSig] int GetState(out uint state);
}

[ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioClient
{
    [PreserveSig] int Initialize(int shareMode, uint flags, long duration, long periodicity, IntPtr format, IntPtr sessionId);
    [PreserveSig] int GetBufferSize(out uint frames);
    [PreserveSig] int GetStreamLatency(out long latency);
    [PreserveSig] int GetCurrentPadding(out uint frames);
    [PreserveSig] int IsFormatSupported(int mode, IntPtr format, out IntPtr closest);
    [PreserveSig] int GetMixFormat(out IntPtr format);
    [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr handle);
    [PreserveSig] int GetService(ref Guid id, [MarshalAs(UnmanagedType.IUnknown)] out object service);
}

[ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioCaptureClient
{
    [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
    [PreserveSig] int ReleaseBuffer(uint frames);
    [PreserveSig] int GetNextPacketSize(out uint frames);
}
