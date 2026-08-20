Option Explicit

Dim shell, files, projectDir, venvDir, pythonExe, pythonwExe
Dim scriptPath, requirementsPath, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")

projectDir = files.GetParentFolderName(WScript.ScriptFullName)
venvDir = projectDir & "\.venv"
pythonExe = venvDir & "\Scripts\python.exe"
pythonwExe = venvDir & "\Scripts\pythonw.exe"
scriptPath = projectDir & "\nd2_roi_locator.py"
requirementsPath = projectDir & "\requirements.txt"
shell.CurrentDirectory = projectDir

' Create the project environment silently when it does not exist.
If Not files.FileExists(pythonExe) Then
    command = "py -3 -m venv " & Q(venvDir)
    exitCode = shell.Run(command, 0, True)
    If exitCode <> 0 Or Not files.FileExists(pythonExe) Then
        MsgBox "Unable to create the project Python environment." & vbCrLf & _
               "Please install Python 3 and try again.", vbCritical, "ND2 ROI Mapper"
        WScript.Quit 1
    End If
End If

' Require the Python syntax level used by the application.
command = Q(pythonExe) & " -c " & Q("import sys; raise SystemExit(sys.version_info < (3, 10))")
exitCode = shell.Run(command, 0, True)
If exitCode <> 0 Then
    MsgBox "ND2 ROI Mapper requires Python 3.10 or later.", vbCritical, "ND2 ROI Mapper"
    WScript.Quit 1
End If

' Install or update dependencies only when the import check fails.
command = Q(pythonExe) & " -c " & Q("import nd2, numpy, PIL, tkinterdnd2")
exitCode = shell.Run(command, 0, True)
If exitCode <> 0 Then
    command = Q(pythonExe) & " -m pip install -r " & Q(requirementsPath)
    exitCode = shell.Run(command, 0, True)
    If exitCode <> 0 Then
        MsgBox "Unable to install the required Python packages." & vbCrLf & _
               "Check the network connection and requirements.txt.", vbCritical, "ND2 ROI Mapper"
        WScript.Quit 1
    End If
End If

' Start the GUI with pythonw so no Command Prompt window is created.
command = Q(pythonwExe) & " " & Q(scriptPath)
exitCode = shell.Run(command, 0, False)
If exitCode <> 0 Then
    MsgBox "Unable to start ND2 ROI Mapper.", vbCritical, "ND2 ROI Mapper"
    WScript.Quit 1
End If

Function Q(ByVal value)
    Q = Chr(34) & value & Chr(34)
End Function
