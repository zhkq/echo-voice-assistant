# =====================================================================
# echo-launch-lib.ps1 - start the ECHO service process with NO console window.
#
# Dot-sourced by: scripts\start.ps1, scripts\startup.ps1, scripts\launch-desktop.ps1
#
# WHY THIS EXISTS (2026-09-23, reported by the user as an empty PowerShell window after startup)
#   The runtime is a uv-created venv, and its Scripts\pythonw.exe is a TRAMPOLINE: it
#   re-execs the base interpreter as `python.exe`, which is a CONSOLE-subsystem image.
#   If the trampoline itself has no console, that child allocates a NEW console - and on
#   a machine where Windows Terminal is the default terminal application, that console
#   shows up as a visible (empty) terminal window titled with the pythonw path, and it
#   stays for as long as ECHO runs. Measured: `pythonw.exe -m app.main` and
#   `WindowsTerminal.exe -Embedding` start in the SAME SECOND.
#
#   Start-Process has no CreateNoWindow switch, and -WindowStyle Hidden does not help:
#   it sets SW_HIDE on the FIRST process only, while the visible console is allocated by
#   the grandchild. So we go through .NET with CreateNoWindow = $true (== CREATE_NO_WINDOW):
#   the trampoline gets a console that has no window, and every descendant inherits it.
#
# WHY cmd.exe INSTEAD OF RedirectStandardOutput
#   .NET can only redirect to PIPES, and a pipe needs a live reader. `start.ps1 -Background`
#   exits right after launching, so that reader would die and ECHO would eventually block
#   on a full pipe. cmd /c redirects straight to files, byte for byte - exactly what
#   Start-Process -RedirectStandardOutput used to do.
#
# ASCII-ONLY on purpose: Windows PowerShell 5.1 parses a BOM-less .ps1 as ANSI, so a
# non-ASCII literal can silently break a script. tests/test_script_encoding.py enforces
# "ASCII or BOM" for this directory.
# =====================================================================

function Start-EchoProcess {
    # Runs <Pythonw> <Arguments> in $WorkDir with stdout/stderr redirected to the log
    # files, without ever creating a console window. Returns the cmd.exe wrapper Process.
    param(
        [Parameter(Mandatory = $true)][string]$Pythonw,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [string]$Arguments = '-m app.main',
        [string]$OutLog = '',
        [string]$ErrLog = ''
    )
    if (-not (Test-Path -LiteralPath $Pythonw)) {
        throw "pythonw not found: $Pythonw"
    }
    $cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
    # cmd /c ""<exe>" <args> > "<out>" 2> "<err>""
    # The doubled leading quote is cmd's rule for a command that starts with a quoted path.
    $line = '""' + $Pythonw + '" ' + $Arguments
    if ($OutLog) { $line += ' > "' + $OutLog + '"' }
    if ($ErrLog) { $line += ' 2> "' + $ErrLog + '"' }
    $line += '"'

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $cmdExe
    $psi.Arguments = '/c ' + $line
    $psi.WorkingDirectory = $WorkDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    return [System.Diagnostics.Process]::Start($psi)
}
