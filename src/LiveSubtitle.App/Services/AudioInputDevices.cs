using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace LiveSubtitle.App.Services;

internal sealed record AudioInputDevice(string Id, string FriendlyName);

/// <summary>WASAPI recording endpoints. IDs are stable across display-name changes.</summary>
internal static class AudioInputDevices
{
    private const int CaptureFlow = 1;
    private const uint ActiveState = 1;
    private const int ChangedComApartment = unchecked((int)0x80010106);
    private static readonly AudioPropertyKey FriendlyNameKey =
        new(new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14);

    internal static List<AudioInputDevice> Enumerate()
    {
        var result = new List<AudioInputDevice>();
        IMMDeviceEnumerator? enumerator = null;
        IMMDeviceCollection? devices = null;
        bool ownsCom = false;
        try
        {
            // WindowTargets runs on WPF's STA; tests/background refresh can use MTA.
            // RPC_E_CHANGED_MODE means COM already belongs to the existing STA,
            // so use that apartment and do not balance the failed initialization.
            int initialized = NativeAudio.CoInitializeEx(IntPtr.Zero, 0);
            if (initialized != ChangedComApartment) NativeAudio.Check(initialized, UiText.T("마이크 목록 COM 초기화"));
            ownsCom = initialized >= 0;
            enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            NativeAudio.Check(enumerator.EnumAudioEndpoints(CaptureFlow, ActiveState, out devices), UiText.T("마이크 목록 읽기"));
            NativeAudio.Check(devices.GetCount(out uint count), UiText.T("마이크 수 확인"));
            for (uint index = 0; index < count; index++)
            {
                IMMDevice? device = null;
                try
                {
                    NativeAudio.Check(devices.Item(index, out device), UiText.T("마이크 장치 읽기"));
                    NativeAudio.Check(device.GetState(out uint state), UiText.T("마이크 연결 상태 확인"));
                    if ((state & ActiveState) == 0) continue; // Unplugged during enumeration.
                    NativeAudio.Check(device.GetId(out string id), UiText.T("마이크 ID 읽기"));
                    if (string.IsNullOrWhiteSpace(id)) continue;
                    result.Add(new AudioInputDevice(id, ReadFriendlyName(device) ?? UiText.F($"오디오 입력 장치 ({id})")));
                }
                catch (COMException ex)
                {
                    // A device can disappear while the source list is being built.
                    Debug.WriteLine(UiText.F($"마이크 목록의 장치를 읽지 못했습니다: {UiText.T(ex.Message)}"));
                }
                finally { NativeAudio.Release(device); }
            }
        }
        catch (COMException ex)
        {
            // A stopped Windows audio service must not hide the browser choices.
            // Starting a selected microphone still reports an explicit failure.
            Debug.WriteLine(UiText.F($"마이크 목록을 읽지 못했습니다: {UiText.T(ex.Message)}"));
        }
        finally
        {
            NativeAudio.Release(devices);
            NativeAudio.Release(enumerator);
            if (ownsCom) NativeAudio.CoUninitialize();
        }
        result.Sort((a, b) =>
        {
            int name = string.Compare(a.FriendlyName, b.FriendlyName, StringComparison.CurrentCulture);
            return name != 0 ? name : string.Compare(a.Id, b.Id, StringComparison.Ordinal);
        });
        return result;
    }

    // Caller owns COM initialization and releases the returned IAudioClient.
    internal static IAudioClient Activate(string deviceId)
    {
        IMMDeviceEnumerator? enumerator = null;
        IMMDevice? device = null;
        try
        {
            enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            NativeAudio.Check(enumerator.GetDevice(deviceId, out device), UiText.T("선택한 마이크 찾기"));
            NativeAudio.Check(device.GetState(out uint state), UiText.T("선택한 마이크 연결 상태 확인"));
            if ((state & ActiveState) == 0)
                throw new InvalidOperationException(UiText.T("선택한 마이크가 연결되어 있지 않거나 사용 중지되었습니다. 오디오 입력 목록을 새로 고친 뒤 다시 선택하세요."));
            // Never accept a render endpoint as a microphone, even for a stale or
            // incorrectly persisted ID. QueryInterface shares the device's RCW.
            NativeAudio.Check(((IMMEndpoint)device).GetDataFlow(out int flow), UiText.T("마이크 입력 방향 확인"));
            if (flow != CaptureFlow)
                throw new InvalidOperationException(UiText.T("선택한 장치는 마이크 입력 장치가 아닙니다."));
            var clientId = typeof(IAudioClient).GUID;
            NativeAudio.Check(device.Activate(ref clientId, 23, IntPtr.Zero, out var audio), UiText.T("선택한 마이크 캡처 열기"));
            return (IAudioClient)audio;
        }
        finally
        {
            NativeAudio.Release(device);
            NativeAudio.Release(enumerator);
        }
    }

    private static string? ReadFriendlyName(IMMDevice device)
    {
        IAudioPropertyStore? properties = null;
        IntPtr value = IntPtr.Zero;
        try
        {
            NativeAudio.Check(device.OpenPropertyStore(0, out properties), UiText.T("마이크 이름 속성 열기")); // STGM_READ
            int size = IntPtr.Size == 8 ? 24 : 16;
            value = Marshal.AllocCoTaskMem(size);
            Marshal.Copy(new byte[size], 0, value, size);
            var key = FriendlyNameKey;
            NativeAudio.Check(properties.GetValue(ref key, value), UiText.T("마이크 이름 읽기"));
            // PKEY_Device_FriendlyName is VT_LPWSTR; the PROPVARIANT union starts at 8.
            if (Marshal.ReadInt16(value) != 31) return null;
            string? name = Marshal.PtrToStringUni(Marshal.ReadIntPtr(value, 8));
            return string.IsNullOrWhiteSpace(name) ? null : name;
        }
        catch (COMException) { return null; }
        finally
        {
            if (value != IntPtr.Zero)
            {
                _ = PropVariantClear(value);
                Marshal.FreeCoTaskMem(value);
            }
            NativeAudio.Release(properties);
        }
    }

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern int PropVariantClear(IntPtr value);
}

[StructLayout(LayoutKind.Sequential)]
internal struct AudioPropertyKey
{
    public Guid FormatId;
    public uint PropertyId;
    internal AudioPropertyKey(Guid formatId, uint propertyId) => (FormatId, PropertyId) = (formatId, propertyId);
}

[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceCollection
{
    [PreserveSig] int GetCount(out uint count);
    [PreserveSig] int Item(uint index, out IMMDevice device);
}

[ComImport, Guid("1BE09788-6894-4089-8586-9A2A6C265AC5"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMEndpoint
{
    [PreserveSig] int GetDataFlow(out int flow);
}

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioPropertyStore
{
    [PreserveSig] int GetCount(out uint count);
    [PreserveSig] int GetAt(uint index, out AudioPropertyKey key);
    [PreserveSig] int GetValue(ref AudioPropertyKey key, IntPtr value);
    [PreserveSig] int SetValue(ref AudioPropertyKey key, IntPtr value);
    [PreserveSig] int Commit();
}
