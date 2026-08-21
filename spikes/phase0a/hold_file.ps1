param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$ReadyPath,
    [int]$HoldMilliseconds = 5000
)

$stream = $null
try {
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    [System.IO.File]::WriteAllText($ReadyPath, "ready")
    Start-Sleep -Milliseconds $HoldMilliseconds
}
finally {
    if ($null -ne $stream) {
        $stream.Dispose()
    }
}
