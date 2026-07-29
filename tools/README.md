# tools

로컬 PC(Windows)에서 쓰는 보조 스크립트.

## find-cmd-popup.ps1 — 주기적으로 뜨는 검은 cmd 창 범인 찾기

검은 `cmd.EXE` 창이 몇 분/몇 시간마다 떴다가 사라질 때, 무엇이 띄우는지 찾아낸다.

```powershell
# 저장소 폴더에서 (관리자 PowerShell 권장)
powershell -ExecutionPolicy Bypass -File tools\find-cmd-popup.ps1

# 창이 뜨는 주기가 길면 감시 시간을 늘린다 (기본 10분)
powershell -ExecutionPolicy Bypass -File tools\find-cmd-popup.ps1 -WatchMinutes 30
```

하는 일

1. **작업 스케줄러** 중 `cmd`/`powershell`/`wscript`/`.bat`/`.vbs` 를 실행하는 작업을
   반복 주기·마지막 실행 시각과 함께 나열한다.
2. **시작프로그램**(레지스트리 `Run` 키 + 시작 폴더) 중 콘솔을 띄우는 항목을 나열한다.
3. 지정한 시간 동안 **실시간 감시** — 새로 뜨는 콘솔 프로세스를 잡아 명령줄, 부모
   프로세스, 같은 시각에 실행된 예약 작업을 기록한다.

결과는 `tools\cmd-popup-report-<날짜>.txt` 로 저장된다.

범인을 찾았으면 끄기 (되돌릴 수 있음):

```powershell
Disable-ScheduledTask -TaskPath "\작업경로\" -TaskName "작업이름"
Enable-ScheduledTask  -TaskPath "\작업경로\" -TaskName "작업이름"   # 되돌리기
```

> 참고: 이 저장소의 모니터링(`check_booking.py`, `presale_monitor.py`, `auto_book*.py`)은
> 전부 GitHub Actions 러너에서 돌기 때문에 PC에 창을 띄우지 않는다. 팝업이 보인다면
> 저장소 밖(다른 프로그램의 업데이트 작업 등)에서 오는 것이다.
