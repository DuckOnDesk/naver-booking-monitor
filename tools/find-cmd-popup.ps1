<#
.SYNOPSIS
  주기적으로 떴다가 사라지는 검은 cmd.exe 창의 정체를 찾아낸다.

.DESCRIPTION
  1) 작업 스케줄러에서 cmd/powershell/wscript 류를 실행하는 반복 작업 목록
  2) 시작프로그램(레지스트리 Run 키 + 시작 폴더) 중 콘솔을 띄우는 항목
  3) 실시간 감시 — 새로 뜨는 cmd/powershell 프로세스를 잡아 명령줄과 부모
     프로세스를 기록하고, 방금 실행된 예약 작업까지 대조해서 알려준다

  관리자 권한이 아니어도 동작하지만, 관리자 PowerShell로 실행하면 다른 계정이
  띄운 창의 명령줄까지 보여서 훨씬 정확하다.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\find-cmd-popup.ps1

.EXAMPLE
  # 창이 뜨는 주기가 길면 감시 시간을 늘린다 (기본 10분)
  powershell -ExecutionPolicy Bypass -File tools\find-cmd-popup.ps1 -WatchMinutes 30
#>
[CmdletBinding()]
param(
    # 실시간 감시 시간(분). 창이 뜨는 주기보다 넉넉히 잡는다.
    [int]$WatchMinutes = 10,
    # 프로세스 폴링 간격(ms). 창이 아주 짧게 떴다 사라지면 더 줄인다.
    [int]$PollMs = 150,
    # 목록 조사만 하고 실시간 감시는 건너뛴다.
    [switch]$NoWatch,
    # 결과 저장 경로 (기본: 이 스크립트 옆에 타임스탬프 파일)
    [string]$LogPath
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

if (-not $LogPath) {
    $LogPath = Join-Path $PSScriptRoot ('cmd-popup-report-{0:yyyyMMdd-HHmmss}.txt' -f (Get-Date))
}

$script:Lines = New-Object System.Collections.Generic.List[string]

function Out-Both {
    param([string]$Text = '', [string]$Color)
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
    $script:Lines.Add($Text)
}

function Out-Section {
    param([string]$Title)
    Out-Both ''
    Out-Both ('=' * 78)
    Out-Both "  $Title"
    Out-Both ('=' * 78)
}

# 콘솔 창을 띄울 수 있는 실행 파일들
$ConsoleExes = @('cmd.exe', 'powershell.exe', 'pwsh.exe', 'wscript.exe', 'cscript.exe')
$ConsolePattern = 'cmd\.exe|powershell|pwsh|wscript|cscript|\.bat\b|\.cmd\b|\.vbs\b'

$myIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($myIdentity)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

Out-Both "cmd 팝업 추적기 — $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Out-Both "PC: $env:COMPUTERNAME / 사용자: $env:USERNAME / 관리자권한: $isAdmin"
if (-not $isAdmin) {
    Out-Both '  ! 관리자 PowerShell로 실행하면 더 많은 정보를 볼 수 있습니다.' 'Yellow'
}

# ---------------------------------------------------------------- 1. 예약 작업
Out-Section '1. 작업 스케줄러 — 콘솔을 띄우는 작업 (반복 주기 포함)'

$taskHits = @()
try {
    foreach ($task in (Get-ScheduledTask -ErrorAction Stop)) {
        $actions = @($task.Actions | Where-Object { $_.Execute })
        $exec = ($actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; '
        if (-not $exec) { continue }
        if ($exec -notmatch $ConsolePattern) { continue }

        $info = $null
        try { $info = $task | Get-ScheduledTaskInfo -ErrorAction Stop } catch {}

        # 반복 주기(있으면)
        $repeat = ($task.Triggers | ForEach-Object {
            if ($_.Repetition -and $_.Repetition.Interval) { $_.Repetition.Interval }
        }) -join ','

        $taskHits += [pscustomobject]@{
            Task     = ($task.TaskPath + $task.TaskName)
            State    = $task.State
            Repeat   = $repeat
            LastRun  = $info.LastRunTime
            NextRun  = $info.NextRunTime
            Command  = $exec.Trim()
        }
    }
} catch {
    Out-Both "  (작업 스케줄러 조회 실패: $($_.Exception.Message))" 'Yellow'
}

if ($taskHits.Count -eq 0) {
    Out-Both '  해당 없음 — 예약 작업 중에 콘솔을 띄우는 건 없습니다.'
} else {
    # 반복 주기가 있거나 최근에 실행된 것부터 = 범인일 확률 높은 순
    foreach ($t in ($taskHits | Sort-Object -Property @{Expression={[bool]$_.Repeat}; Descending=$true},
                                             @{Expression='LastRun'; Descending=$true})) {
        Out-Both ''
        Out-Both "  [$($t.State)] $($t.Task)"
        if ($t.Repeat)  { Out-Both "      반복주기 : $($t.Repeat)" 'Cyan' }
        Out-Both     "      마지막실행: $($t.LastRun)    다음실행: $($t.NextRun)"
        Out-Both     "      명령      : $($t.Command)"
    }
    Out-Both ''
    Out-Both '  ↑ 반복주기가 있고 마지막 실행이 방금인 작업이 유력한 범인입니다.' 'Cyan'
}

# ------------------------------------------------------------ 2. 시작프로그램
Out-Section '2. 시작프로그램 — 콘솔을 띄우는 항목'

$startupHits = 0
$runKeys = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce'
)
foreach ($key in $runKeys) {
    if (-not (Test-Path $key)) { continue }
    $props = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
    if (-not $props) { continue }
    foreach ($p in $props.PSObject.Properties) {
        if ($p.Name -like 'PS*') { continue }
        if ("$($p.Value)" -match $ConsolePattern) {
            Out-Both "  $key"
            Out-Both "      $($p.Name) = $($p.Value)"
            $startupHits++
        }
    }
}

$startupDirs = @(
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Startup')
)
foreach ($dir in $startupDirs) {
    if (-not (Test-Path $dir)) { continue }
    foreach ($f in (Get-ChildItem $dir -File -ErrorAction SilentlyContinue)) {
        Out-Both "  시작폴더: $($f.FullName)"
        $startupHits++
    }
}

if ($startupHits -eq 0) { Out-Both '  해당 없음.' }

# -------------------------------------------------------------- 3. 실시간 감시
if (-not $NoWatch) {
    Out-Section "3. 실시간 감시 — $WatchMinutes 분간, 새로 뜨는 콘솔 프로세스를 잡습니다"
    Out-Both '  창이 뜨는 것을 기다리는 중... (Ctrl+C 로 중단하면 지금까지 결과가 저장됩니다)'
    Out-Both ''

    $filter = ($ConsoleExes | ForEach-Object { "Name='$_'" }) -join ' OR '
    $seen = @{}
    # 이미 떠 있는 것(이 스크립트 자신 포함)은 제외하고 시작
    foreach ($p in (Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue)) {
        $seen[$p.ProcessId] = $true
    }

    $deadline = (Get-Date).AddMinutes($WatchMinutes)
    $catchCount = 0

    try {
        while ((Get-Date) -lt $deadline) {
            $now = Get-Date
            foreach ($p in (Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue)) {
                if ($seen.ContainsKey($p.ProcessId)) { continue }
                $seen[$p.ProcessId] = $true
                $catchCount++

                $parentName = '(종료됨/조회불가)'
                $parentCmd  = ''
                try {
                    $par = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction Stop
                    if ($par) { $parentName = $par.Name; $parentCmd = $par.CommandLine }
                } catch {}

                Out-Both ''
                Out-Both ('-' * 78)
                Out-Both ("  [잡았다 #$catchCount] {0:HH:mm:ss}  {1} (PID $($p.ProcessId))" -f $now, $p.Name) 'Green'
                Out-Both "      명령줄  : $($p.CommandLine)"
                Out-Both "      부모    : $parentName (PID $($p.ParentProcessId))"
                if ($parentCmd) { Out-Both "      부모명령: $parentCmd" }

                if ($parentName -match 'svchost|taskeng|schedule') {
                    Out-Both '      → 부모가 서비스/스케줄러입니다. 작업 스케줄러가 띄운 창이 거의 확실합니다.' 'Cyan'
                }

                # 방금(10초 이내) 실행된 예약 작업 대조 — 처음 3번만 (느림)
                if ($catchCount -le 3) {
                    try {
                        $recent = Get-ScheduledTask -ErrorAction Stop | ForEach-Object {
                            $i = $null
                            try { $i = $_ | Get-ScheduledTaskInfo -ErrorAction Stop } catch {}
                            if ($i -and $i.LastRunTime -and
                                ($now - $i.LastRunTime).TotalSeconds -ge -5 -and
                                ($now - $i.LastRunTime).TotalSeconds -le 20) {
                                "$($_.TaskPath)$($_.TaskName)  (LastRun $($i.LastRunTime))"
                            }
                        }
                        if ($recent) {
                            Out-Both '      같은 시각에 실행된 예약 작업:' 'Cyan'
                            foreach ($r in $recent) { Out-Both "        - $r" 'Cyan' }
                        }
                    } catch {}
                }
            }
            Start-Sleep -Milliseconds $PollMs
        }
    } finally {
        Out-Both ''
        if ($catchCount -eq 0) {
            Out-Both "  감시 시간 동안 새로 뜬 콘솔 프로세스가 없습니다." 'Yellow'
            Out-Both "  → -WatchMinutes 를 창이 뜨는 주기보다 길게 잡고 다시 실행하세요." 'Yellow'
        } else {
            Out-Both "  총 $catchCount 건 포착."
        }
    }
}

# ------------------------------------------------------------------- 마무리
Out-Section '다음 단계'
Out-Both '  범인이 예약 작업으로 확인되면 아래로 끕니다 (경로/이름은 위 결과에서 복사):'
Out-Both ''
Out-Both '    Disable-ScheduledTask -TaskPath "\작업경로\" -TaskName "작업이름"'
Out-Both ''
Out-Both '  되돌릴 때는 Enable-ScheduledTask 에 같은 인자를 주면 됩니다.'
Out-Both '  삭제(Unregister-ScheduledTask)는 되돌릴 수 없으니 먼저 Disable 로 확인하세요.'
Out-Both ''

try {
    $script:Lines -join "`r`n" | Set-Content -Path $LogPath -Encoding UTF8
    Write-Host ""
    Write-Host "결과 저장: $LogPath" -ForegroundColor Green
    Write-Host "이 파일 내용을 그대로 보내주시면 어느 작업을 꺼야 하는지 짚어드립니다."
} catch {
    Write-Host "결과 파일 저장 실패: $($_.Exception.Message)" -ForegroundColor Red
}
