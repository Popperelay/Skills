$ErrorActionPreference = 'Stop'
$cache = Join-Path $env:LOCALAPPDATA 'YouTubeZhDub'
# Reuse the fully extracted runtime from the previous offline release.
$runtime = Join-Path $cache 'rt-fa67111eb1cbb0b6'
if (!(Test-Path -LiteralPath $runtime -PathType Container)) { $runtime = Join-Path $cache 'runtime-v1' }
$python = Join-Path $runtime 'python\python.exe'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $cache 'bytecode'
Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue
$env:PATH = (Join-Path $runtime 'bin') + ';' + $env:PATH
$lock = [Threading.Mutex]::new($false, 'Local\YouTubeZhDub-setup-v1')
try {
    try { [void]$lock.WaitOne() } catch [Threading.AbandonedMutexException] { }
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        [IO.Directory]::CreateDirectory($runtime) | Out-Null
        $stage = Join-Path $runtime 'python-setup'
        [IO.Directory]::CreateDirectory($stage) | Out-Null
        $archive = Join-Path $stage 'python.nupkg'
        $expected = '0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8'
        if (!(Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive).Hash -ne $expected) {
            Write-Host 'Downloading Python 3.12.10 x64...'
            $client = New-Object System.Net.WebClient
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $client.DownloadFile('https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg', $archive)
            } finally { $client.Dispose() }
        }
        if ((Get-FileHash -LiteralPath $archive).Hash -ne $expected) { throw 'Python download checksum mismatch.' }
        & "$env:SystemRoot\System32\tar.exe" -xf $archive -C $stage tools
        if ($LASTEXITCODE -ne 0) { throw 'Could not extract Python.' }
        $destination = Join-Path $runtime 'python'
        if ([IO.Directory]::Exists($destination)) {
            # Preserve packages when repairing a missing interpreter in an existing installation.
            Copy-Item -Path (Join-Path $stage 'tools\*') -Destination $destination -Recurse -Force
        } else { [IO.Directory]::Move((Join-Path $stage 'tools'), $destination) }
        Remove-Item -LiteralPath $archive
    }
    if ($args.Count -gt 0 -and $args[0] -in 'doctor','run') {
        & $python (Join-Path $PSScriptRoot 'tool\prepare.py')
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
} finally {
    $lock.ReleaseMutex()
    $lock.Dispose()
}
$env:HF_HUB_CACHE = Join-Path $runtime 'models\hub'
$env:HF_HUB_OFFLINE = '1'
& $python (Join-Path $PSScriptRoot 'tool\dub\__main__.py') @args
exit $LASTEXITCODE
