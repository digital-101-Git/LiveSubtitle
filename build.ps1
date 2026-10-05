param([string]$Dotnet = 'dotnet')
$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$project = Join-Path $appRoot 'src\LiveSubtitle.App\LiveSubtitle.App.csproj'
$stagingRoot = Join-Path $appRoot '.publish-stage'
$sdkVersion = & $Dotnet --version
if ($LASTEXITCODE -ne 0 -or [int]($sdkVersion.Split('.')[0]) -lt 10) {
    throw '.NET 10 SDK is required. Pass -Dotnet with the path to dotnet.exe.'
}
# PublishDir must not be an ancestor of the project. The SDK excludes PublishDir
# from source globbing; publishing directly to appRoot silently omits app sources.
& $Dotnet publish $project -c Release -r win-x64 --self-contained true -o $stagingRoot -p:PublishSingleFile=false
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Get-ChildItem -LiteralPath $stagingRoot | Copy-Item -Destination $appRoot -Recurse -Force
$resolvedApp = [System.IO.Path]::GetFullPath($appRoot).TrimEnd('\') + '\'
$resolvedStaging = [System.IO.Path]::GetFullPath($stagingRoot)
if (-not $resolvedStaging.StartsWith($resolvedApp, [System.StringComparison]::OrdinalIgnoreCase) -or
    [System.IO.Path]::GetFileName($resolvedStaging) -ne '.publish-stage') {
    throw 'Refusing to clean a publish staging directory outside the application folder.'
}
Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
Write-Output ('Built: ' + (Join-Path $appRoot 'LiveSubtitle.exe'))
