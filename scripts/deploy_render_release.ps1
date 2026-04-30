$ErrorActionPreference = "Stop"

$repo = "Kratos561/aisak-bot"
$serviceId = "srv-d7l8q65f420s739l77e0"
$files = @(
    "README.md",
    "config.py",
    "main.py",
    "requirements.txt",
    "cogs/controls.py",
    "cogs/help.py",
    "cogs/music.py",
    "scripts/simulate_bot_playback.py",
    "utils/audio_effects.py",
    "utils/audio_handler.py",
    "utils/formatters.py",
    "utils/models.py",
    "utils/music_manager.py",
    "utils/player_controls.py",
    "utils/validators.py"
)

if (-not $env:GITHUB_PAT) {
    throw "Falta GITHUB_PAT en el entorno."
}

if (-not $env:RENDER_API_KEY) {
    throw "Falta RENDER_API_KEY en el entorno."
}

$githubHeaders = @{
    Accept = "application/vnd.github+json"
    Authorization = "Bearer $env:GITHUB_PAT"
    "X-GitHub-Api-Version" = "2022-11-28"
}

$renderHeaders = @{
    Authorization = "Bearer $env:RENDER_API_KEY"
    Accept = "application/json"
}

function Update-GitHubFile {
    param(
        [string]$Path
    )

    $uri = "https://api.github.com/repos/$repo/contents/$($Path -replace '\\','/')"
    $currentSha = $null
    try {
        $current = Invoke-RestMethod -Uri $uri -Headers $githubHeaders
        $currentSha = $current.sha
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 404) {
            throw
        }
    }
    $content = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes((Join-Path (Get-Location) $Path)))
    $body = @{
        message = "fix: stabilize playback and queue responses"
        content = $content
        branch = "main"
    }
    if ($currentSha) {
        $body.sha = $currentSha
    }
    $jsonBody = $body | ConvertTo-Json -Compress

    Invoke-RestMethod -Method Put -Uri $uri -Headers $githubHeaders -Body $jsonBody -ContentType "application/json" | Out-Null
    Write-Host "updated $Path"
}

function Set-RepoVisibility {
    param(
        [bool]$Private
    )

    $body = @{ private = $Private } | ConvertTo-Json -Compress
    $result = Invoke-RestMethod -Method Patch -Uri "https://api.github.com/repos/$repo" -Headers $githubHeaders -Body $body -ContentType "application/json"
    Write-Host ("repo visibility: " + $result.visibility)
}

function Start-RenderDeploy {
    $deploy = Invoke-RestMethod -Method Post -Uri "https://api.render.com/v1/services/$serviceId/deploys" -Headers $renderHeaders
    Write-Host ("deploy started: " + $deploy.id + " commit=" + $deploy.commit.id)
    return $deploy.id
}

function Wait-RenderDeploy {
    param(
        [string]$DeployId
    )

    for ($i = 0; $i -lt 30; $i++) {
        $deploy = Invoke-RestMethod -Uri "https://api.render.com/v1/services/$serviceId/deploys/$DeployId" -Headers $renderHeaders
        Write-Host ("deploy status: " + $deploy.status)
        if ($deploy.status -in @("live", "build_failed", "update_failed", "canceled")) {
            return $deploy
        }
        Start-Sleep -Seconds 10
    }

    throw "Timeout esperando el deploy de Render."
}

foreach ($file in $files) {
    Update-GitHubFile -Path $file
}

Set-RepoVisibility -Private $false
$deployId = Start-RenderDeploy
$deploy = Wait-RenderDeploy -DeployId $deployId
Set-RepoVisibility -Private $true

if ($deploy.status -ne "live") {
    throw ("Render no quedo live. Estado final: " + $deploy.status)
}

$health = Invoke-WebRequest -Uri "https://aisak-bot.onrender.com/health" -UseBasicParsing -TimeoutSec 60
Write-Host ("health status: " + $health.StatusCode)
Write-Host $health.Content
