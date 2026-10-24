param([string]$Home_, [int]$Cycles = 20, [string]$Mode = "normal")
# 11-ui 7 "start-failure recovery" on the built Windows Controller (GAP-117): open and
# close it like a person (double-click, then the X), again and again. Each cycle: window
# up, service answers, X closes the window fast, the idle service ends, nothing is left.
# Run: powershell -File tools\checks\open_close_cycles.ps1 -Home_ <unzipped build folder>
#      -Mode normal|quickclose|double|killsvc|killall [-Cycles 20]. Modes:
#   normal      open, wait until ready, close
#   quickclose  close as soon as the window exists (service may still be starting)
#   double      two double-clicks at once, then close every window
#   killsvc     kill the service while the window is open, close, open again
#   killall     kill window and service (Task Manager), then open again
Add-Type @"
using System; using System.Runtime.InteropServices;
public class WM { [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l); }
"@
$exe = Join-Path $Home_ "ZeroLoss.exe"
function Ours { Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Home_) } }
function Answers { try { $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:10994/v1/controller/status"; $true } catch { $false } }
function Launch { $null = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "`"$exe`""; CurrentDirectory = $Home_ } }
function Windows { Get-Process -Name ZeroLoss -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Home_) -and $_.MainWindowHandle -ne [IntPtr]::Zero } }
function WaitFor([scriptblock]$cond, [double]$s) { $sw = [Diagnostics.Stopwatch]::StartNew(); while ($sw.Elapsed.TotalSeconds -lt $s) { if (& $cond) { return $sw.Elapsed.TotalSeconds }; Start-Sleep -Milliseconds 200 }; return -1 }
function CloseAll { foreach ($w in Windows) { [void][WM]::PostMessage($w.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) } }

$fails = 0
for ($i = 1; $i -le $Cycles; $i++) {
  $note = ""
  if (Ours) { $note += " leftover-before:" + ((Ours | % { $_.Name + "#" + $_.ProcessId }) -join ",") }
  Launch
  if ($Mode -eq "double") { Launch }
  $tw = WaitFor { (Windows | Measure-Object).Count -ge 1 } 40
  if ($Mode -eq "quickclose") { $ts = 0 } else { $ts = WaitFor { Answers } 40 }
  if ($Mode -eq "killsvc" -and $ts -ge 0) {
    Ours | Where-Object { $_.Name -eq "zeroloss.exe" } | % { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep 3
  }
  if ($Mode -eq "killall") {
    Start-Sleep 2
    Ours | % { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $tc = 0; $te = WaitFor { -not (Ours) } 10
  } else {
    Start-Sleep -Milliseconds 500
    $sw = [Diagnostics.Stopwatch]::StartNew(); CloseAll
    $tc = WaitFor { (Windows | Measure-Object).Count -eq 0 } 20
    $te = WaitFor { -not (Ours) } 20
  }
  $ok = ($tw -ge 0) -and ($Mode -eq "quickclose" -or $ts -ge 0) -and ($tc -ge 0) -and ($te -ge 0)
  if (-not $ok) { $fails++ }
  $left = (Ours | % { $_.Name + "#" + $_.ProcessId }) -join ","
  "{0,2} {1} window {2:N1}s service {3:N1}s closed {4:N1}s all-gone {5:N1}s {6}{7}" -f $i, ($(if ($ok) { "OK  " } else { "FAIL" })), $tw, $ts, $tc, $te, $(if ($left) { "LEFT " + $left } else { "" }), $note
  if ($left) { Ours | % { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 2 }
}
"mode ${Mode}: $fails of $Cycles cycles failed"
