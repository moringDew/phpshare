param(
    [string]$Start = "2018-01-01",
    [string]$End = (Get-Date -Format "yyyy-MM-dd"),
    [string]$Output = "data/a_share_daily.csv"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TokenPath = Join-Path $ProjectRoot ".secrets\tushare_token.dpapi"

if (-not (Test-Path -LiteralPath $TokenPath)) {
    throw "未找到加密的 Tushare Token：$TokenPath"
}

$SecureToken = ConvertTo-SecureString (Get-Content -LiteralPath $TokenPath -Raw)
$TokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
try {
    $env:TUSHARE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPointer)
    $env:PYTHONPATH = Join-Path $ProjectRoot "backend"
    Push-Location $ProjectRoot
    try {
        python -m market_data.sync_cli --start $Start --end $End --output $Output
        if ($LASTEXITCODE -ne 0) {
            throw "正式行情同步失败，退出码：$LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPointer)
}
