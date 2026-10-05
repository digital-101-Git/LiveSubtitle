using System;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Channels;
using System.Threading.Tasks;

namespace LiveSubtitle.App.Services;

public sealed class SubtitleSession : IAsyncDisposable
{
    private readonly ClientWebSocket _socket = new();
    private readonly SemaphoreSlim _writer = new(1, 1);
    private readonly CancellationTokenSource _stop = new();
    private readonly Channel<byte[]> _audio = Channel.CreateBounded<byte[]>(new BoundedChannelOptions(60) { SingleReader = true, FullMode = BoundedChannelFullMode.Wait });
    private readonly TaskCompletionSource<string> _ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private Task? _receiveTask;
    private Task? _sendTask;
    private int _audioOverrun;
    private int _disposed;
    public string SessionId { get; private set; } = "";
    public event Action<JsonElement>? Event;
    public event Action<string>? Error;
    public event Action<string>? Log;

    public async Task StartAsync(string token, string mode, string language, CancellationToken ct, string targetLanguage = "ko")
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, _stop.Token);
        _socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(15);
        await _socket.ConnectAsync(new Uri($"ws://127.0.0.1:{EngineClient.Port}/v1/stream"), linked.Token);
        _receiveTask = ReceiveAsync(_stop.Token);
        await SendJsonAsync(new { type = "auth", token }, linked.Token);
        await SendJsonAsync(new { type = "start", mode, language, target_language = targetLanguage }, linked.Token);
        SessionId = await _ready.Task.WaitAsync(TimeSpan.FromMinutes(5), linked.Token);
        _sendTask = SendAudioAsync(_stop.Token);
    }

    public void QueueAudio(byte[] pcm)
    {
        if (Volatile.Read(ref _disposed) != 0 || _stop.IsCancellationRequested || string.IsNullOrEmpty(SessionId)) return;
        if (_audio.Writer.TryWrite(pcm)) return;
        if (Interlocked.Exchange(ref _audioOverrun, 1) != 0) return;
        _audio.Writer.TryComplete();
        _stop.Cancel();
        Log?.Invoke(UiText.T("오디오 전송 대기열이 가득 차 자막 세션을 중지합니다."));
        Error?.Invoke(UiText.T("오디오 전송 대기열이 6초를 초과해 중지했습니다. 음성을 건너뛰지 않도록 연결과 시스템 부하를 확인한 뒤 다시 시작하세요."));
    }

    private async Task SendAudioAsync(CancellationToken ct)
    {
        try
        {
            await foreach (byte[] pcm in _audio.Reader.ReadAllAsync(ct))
            {
                await _writer.WaitAsync(ct);
                try { await _socket.SendAsync(new ArraySegment<byte>(pcm), WebSocketMessageType.Binary, true, ct); }
                finally { _writer.Release(); }
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) { if (!ct.IsCancellationRequested) Error?.Invoke(UiText.T("오디오 전송 실패: {0}", UiText.T(ex.Message))); }
    }

    private async Task SendJsonAsync(object value, CancellationToken ct)
    {
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(value);
        await _writer.WaitAsync(ct);
        try { await _socket.SendAsync(new ArraySegment<byte>(payload), WebSocketMessageType.Text, true, ct); }
        finally { _writer.Release(); }
    }

    private async Task ReceiveAsync(CancellationToken ct)
    {
        byte[] buffer = new byte[8192];
        try
        {
            while (!ct.IsCancellationRequested && _socket.State == WebSocketState.Open)
            {
                using var message = new MemoryStream();
                WebSocketReceiveResult result;
                do
                {
                    result = await _socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        if (!ct.IsCancellationRequested) throw new IOException(UiText.T("엔진이 자막 연결을 종료했습니다."));
                        return;
                    }
                    message.Write(buffer, 0, result.Count);
                    if (message.Length > 1024 * 1024) throw new IOException(UiText.T("엔진 메시지가 허용 크기를 초과했습니다."));
                } while (!result.EndOfMessage);
                if (result.MessageType != WebSocketMessageType.Text) continue;
                using var document = JsonDocument.Parse(message.ToArray());
                JsonElement root = document.RootElement;
                string type = Get(root, "type");
                if (type == "ready")
                {
                    string session = Get(root, "session_id");
                    _ready.TrySetResult(session.Length > 0 ? session : Guid.NewGuid().ToString("N"));
                }
                else if (type == "error")
                {
                    string error = UiText.T(Get(root, "message"));
                    _ready.TrySetException(new InvalidOperationException(error));
                    Error?.Invoke(error);
                }
                Event?.Invoke(root.Clone());
            }
        }
        catch (OperationCanceledException) { _ready.TrySetCanceled(); }
        catch (Exception ex)
        {
            _ready.TrySetException(ex);
            if (!ct.IsCancellationRequested) Error?.Invoke(UiText.T(ex.Message));
        }
    }

    private static string Get(JsonElement value, string key) => value.TryGetProperty(key, out var item) ? item.ToString() : "";

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        _audio.Writer.TryComplete();
        _stop.Cancel();
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try
        {
            if (_socket.State == WebSocketState.Open)
            {
                await SendJsonAsync(new { type = "stop" }, deadline.Token);
                await _writer.WaitAsync(deadline.Token);
                try { await _socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "user_stop", deadline.Token); }
                finally { _writer.Release(); }
            }
        }
        catch (Exception ex) when (ex is OperationCanceledException or WebSocketException or ObjectDisposedException) { }
        _socket.Abort();
        if (_sendTask != null) try { await _sendTask; } catch { }
        if (_receiveTask != null) try { await _receiveTask; } catch { }
        _socket.Dispose(); _stop.Dispose();
    }
}
