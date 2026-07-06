Set WshShell = CreateObject("WScript.Shell")
' 0 hides the command prompt window, False returns execution immediately to the caller
WshShell.Run "pythonw main.py", 0, False
