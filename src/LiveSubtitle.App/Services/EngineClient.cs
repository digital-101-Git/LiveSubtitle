using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace LiveSubtitle.App.Services;

public sealed class EngineClient : IDisposable
{
    public const int Port = 17865;
    private readonly HttpClient _http = new() { BaseAddress = new Uri($"http://127.0.0.1:{Port}/"), Timeout = TimeSpan.FromMinutes(10) };
    private readonly SemaphoreSlim _startup = new(1, 1);
    private Process? _ownedProcess;
    private string _token = "";
    public string Root { get; }
    public string Token => _token;
    public event Action<string>? Log;

    public EngineClient() => Root = FindRoot();

    private static string FindRoot()
    {
        foreach (string candidate in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            DirectoryInfo? directory = new(candidate);
            while (directory != null)
            {
                if (File.Exists(Path.Combine(directory.FullName, "engine", "server.py")) && Directory.Exists(Path.Combine(directory.FullName, "models")))
                    return directory.FullName;
                directory = directory.Parent;
            }
        }
        throw new DirectoryNotFoundException(UiText.T("engine\\server.py와 models 폴더를 찾을 수 없습니다. 배포 폴더 전체를 같은 위치에 두세요."));
    }

    public async Task EnsureReadyAsync(CancellationToken ct)
    {
        await _startup.WaitAsync(ct);
        try
        {
            if (await IsHealthyAsync(ct))
            {
                await ReadTokenAsync(ct);
                Log?.Invoke(UiText.T("실행 중인 로컬 엔진에 연결했습니다."));
                return;
            }
            if (_ownedProcess is { HasExited: false })
                Log?.Invoke(UiText.T("앱이 시작한 엔진의 준비를 기다립니다."));
            else
                await StartEngineAsync(ct);

            for (int i = 0; i < 120; i++)
            {
                ct.ThrowIfCancellationRequested();
                if (await IsHealthyAsync(ct))
                {
                    await ReadTokenAsync(ct);
                    Log?.Invoke(UiText.T("로컬 엔진 준비 완료 · 127.0.0.1:17865"));
                    return;
                }
                if (_ownedProcess is { HasExited: true })
                    throw new InvalidOperationException(UiText.F($"엔진 프로세스가 종료되었습니다 (코드 {_ownedProcess.ExitCode}). 진행 기록을 확인하세요."));
                await Task.Delay(500, ct);
            }
            throw new TimeoutException(UiText.T("로컬 엔진이 60초 안에 응답하지 않았습니다. 진행 기록을 확인한 뒤 다시 연결하세요."));
        }
        finally { _startup.Release(); }
    }

    private async Task StartEngineAsync(CancellationToken ct)
    {
        string embedded = Path.Combine(Root, "runtime", "python", "python.exe");
        var candidates = new List<(string Exe, string[] Prefix)>();
        if (File.Exists(embedded)) candidates.Add((embedded, Array.Empty<string>()));
        else
        {
            candidates.Add(("py", new[] { "-3.14" }));
            candidates.Add(("python", Array.Empty<string>()));
        }
        Exception? last = null;
        foreach (var (exe, prefix) in candidates)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                var start = new ProcessStartInfo(exe)
                {
                    WorkingDirectory = Root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };
                foreach (string argument in prefix) start.ArgumentList.Add(argument);
                start.ArgumentList.Add(Path.Combine(Root, "engine", "server.py"));
                start.ArgumentList.Add("--root"); start.ArgumentList.Add(Root);
                start.ArgumentList.Add("--port"); start.ArgumentList.Add(Port.ToString());
                start.Environment["PYTHONUTF8"] = "1";
                start.Environment["PYTHONUNBUFFERED"] = "1";
                var process = new Process { StartInfo = start, EnableRaisingEvents = true };
                process.OutputDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) Log?.Invoke(e.Data); };
                process.ErrorDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) Log?.Invoke(e.Data); };
                process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine();
                _ownedProcess?.Dispose(); _ownedProcess = process;
                Log?.Invoke(UiText.T("로컬 Python 엔진을 시작했습니다."));
                await Task.Delay(700, ct);
                if (!process.HasExited) return;
                last = new InvalidOperationException(UiText.F($"Python 종료 코드: {process.ExitCode}"));
            }
            catch (Exception ex) when (ex is not OperationCanceledException) { last = ex; }
        }
        throw new InvalidOperationException(UiText.T("Python 엔진을 실행할 수 없습니다. 배포 runtime 폴더와 엔진 파일을 확인하세요."), last);
    }

    private async Task<bool> IsHealthyAsync(CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(2));
        try
        {
            using var response = await _http.GetAsync("health", timeout.Token);
            if (!response.IsSuccessStatusCode) return false;
            using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token));
            if (json.RootElement.TryGetProperty("service", out var service) && service.GetString() == "LiveSubtitle") return true;
            throw new InvalidOperationException(UiText.T("17865 포트를 다른 서비스가 사용하고 있습니다. 해당 서비스를 종료하거나 포트 충돌을 해결하세요."));
        }
        catch (HttpRequestException) { return false; }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested) { return false; }
    }

    private async Task ReadTokenAsync(CancellationToken ct)
    {
        string path = Path.Combine(Root, "config", "engine-token.txt");
        for (int attempt = 0; attempt < 20; attempt++)
        {
            if (File.Exists(path))
            {
                _token = (await File.ReadAllTextAsync(path, ct)).Trim();
                if (_token.Length > 0) return;
            }
            await Task.Delay(150, ct);
        }
        throw new InvalidOperationException(UiText.T("엔진 인증 토큰이 없습니다. 이 앱과 엔진의 배포 폴더가 같은지 확인하세요."));
    }

    public async Task<JsonElement> GetAsync(string path, CancellationToken ct = default) => await RequestAsync(HttpMethod.Get, path, null, ct);
    public async Task<JsonElement> PutAsync(string path, object body, CancellationToken ct = default) => await RequestAsync(HttpMethod.Put, path, body, ct);
    public async Task<JsonElement> PostAsync(string path, object? body = null, CancellationToken ct = default) => await RequestAsync(HttpMethod.Post, path, body, ct);

    private async Task<JsonElement> RequestAsync(HttpMethod method, string path, object? body, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(method, path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _token);
        if (body != null) request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        using var response = await _http.SendAsync(request, ct);
        string content = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
        {
            string message = content;
            try
            {
                using var error = JsonDocument.Parse(content);
                foreach (string key in new[] { "message", "detail", "error" })
                    if (error.RootElement.TryGetProperty(key, out var value)) { message = value.ToString(); break; }
            }
            catch (JsonException) { }
            throw new InvalidOperationException(UiText.F($"엔진 요청 실패 ({(int)response.StatusCode}): {UiText.T(message)}"));
        }
        using var doc = JsonDocument.Parse(string.IsNullOrWhiteSpace(content) ? "{}" : content);
        return doc.RootElement.Clone();
    }

    public void Dispose()
    {
        if (_ownedProcess != null)
        {
            try { if (!_ownedProcess.HasExited) _ownedProcess.Kill(entireProcessTree: true); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
            _ownedProcess.Dispose(); _ownedProcess = null;
        }
        _http.Dispose();
    }
}
