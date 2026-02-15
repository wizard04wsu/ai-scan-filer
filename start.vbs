Option Explicit

Dim i, args, sh, fso, arg, scriptDir, showConsole, pyCmd, installBat, cfg, ocrScript, filerScript, pauseOnError
set args = WScript.Arguments
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
showConsole = 1
cfg = ""

For i=1 To args.Count
	arg = args(i)
	if arg = "--config" and args.Count >= i+2 Then
		i = i+1
		arg = args(i)
		if fso.FileExists(arg) Then
			' Path to the config file specified in the arguments.
			cfg = fso.GetAbsolutePathName(arg)
		Else
			WScript.Echo "Config file not found: " & arg
			WScript.Quit(1)
		end If
	elseif arg = "--hidden" Then
		showConsole = 0
	end if
Next

set fso = Nothing

' Start Ollama server (non-blocking).
sh.Run "ollama pull llama3.1:8b", 1, False
sh.Run "ollama serve", 0, False
WScript.Sleep 1500

' Folder where this VBS lives (put it next to your .bat + scripts).
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

' Path to the dependencies installation script.
installBat = scriptDir & "install_dependencies.bat"

if cfg = "" Then
	' Path to the default config file if it wasn't specified in the arguments.
	cfg = scriptDir & "config.json"
end if

ocrScript    = scriptDir & "asf_ocr.py"
filerScript  = scriptDir & "ai-scan-filer.py"

pyCmd = "python"
pauseOnError = ""
if showConsole = 0 then
	' Use pythonw (no console).
	pyCmd = "pythonw"
else
	'Pause the cmd window before closing if the error level is not 0 (success) and not 15 (killed process).
	pauseOnError = " & echo ErrorLevel:!ERRORLEVEL! & if !ERRORLEVEL! neq 0 if !ERRORLEVEL! neq 15 pause"
end If

' 1) Run dependency installer BAT and WAIT for it to finish.
'    0: hidden, True: wait
sh.Run """" & installBat & """", 0, True

' 2) Start OCR watcher (do NOT wait).
sh.Run "cmd /v:on /c " & pyCmd & " """ & ocrScript & """ --config """ & cfg & """" & pauseOnError, showConsole, False

' 3) Start AI filer watcher simultaneously (do NOT wait).
sh.Run "cmd /v:on /c " & pyCmd & " """ & filerScript & """ --config """ & cfg & """" & pauseOnError, showConsole, False
