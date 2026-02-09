Option Explicit

Dim showConsole: showConsole = 1

Dim sh, scriptDir, pyCmd, installBat, cfg, ocrScript, filerScript
Set sh = CreateObject("WScript.Shell")

' Start Ollama server (non-blocking)
sh.Run "ollama pull llama3.1:8b", 1, False
sh.Run "ollama serve", 0, False
WScript.Sleep 1500

' Folder where this VBS lives (put it next to your .bat + scripts)
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

' Paths (edit if your filenames differ)
installBat   = Chr(34) & scriptDir & "install_dependencies.bat" & Chr(34)
cfg          = Chr(34) & scriptDir & "config.json" & Chr(34)

' Python command to use based on whether the console should be hidden or not.
' You can also hardcode a full path if needed.
pyCmd = "python"
if showConsole = 0 then
	' Use pythonw.exe (no console).
	pyCmd = "pythonw"
end if

ocrScript    = Chr(34) & scriptDir & "ai-scan-filer-ocr.py" & Chr(34)
filerScript  = Chr(34) & scriptDir & "ai-scan-filer.py" & Chr(34)

' 1) Run dependency installer BAT and WAIT for it to finish
'    0 = hidden window, True = wait
sh.Run installBat, 0, True

' 2) Start OCR watcher (do NOT wait)
'    Set working directory so relative config.json works
sh.CurrentDirectory = scriptDir
sh.Run pyCmd & " " & ocrScript & " --config " & cfg, showConsole, False

' 3) Start AI filer watcher simultaneously (do NOT wait)
sh.Run pyCmd & " " & filerScript & " --config " & cfg, showConsole, False
