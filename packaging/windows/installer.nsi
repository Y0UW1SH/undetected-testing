; Build through build.py so every File comes from the reviewed staging tree.
Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
Name "Stealth Wallet Monitor"
OutFile "${OUTFILE}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\StealthWalletMonitor"
InstallDirRegKey HKCU "Software\StealthWalletMonitor" "InstallDir"
SetCompressor /FINAL zlib
ShowInstDetails nevershow
ShowUninstDetails nevershow
VIProductVersion "0.1.0.0"
VIAddVersionKey "ProductName" "Stealth Wallet Monitor"
VIAddVersionKey "FileDescription" "Stealth Wallet Monitor per-user setup"
VIAddVersionKey "FileVersion" "0.1.0"
VIAddVersionKey "LegalCopyright" "See bundled licenses"
Var NoIntegration

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "This application requires 64-bit Windows."
    Abort
  ${EndIf}
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/NOINTEGRATION" $1
  IfErrors +2
  StrCpy $NoIntegration 1
FunctionEnd

Function CheckDirectory
  IfFileExists "$INSTDIR\wallet-install.id" marked
  FindFirst $0 $1 "$INSTDIR\*"
  loop:
    StrCmp $1 "" empty
    StrCmp $1 "." next
    StrCmp $1 ".." next
    FindClose $0
    MessageBox MB_OK|MB_ICONSTOP "Choose an empty directory or the existing Stealth Wallet Monitor installation." /SD IDOK
    SetErrorLevel 2
    Abort
  next:
    FindNext $0 $1
    Goto loop
  empty:
    FindClose $0
    Return
  marked:
    FileOpen $0 "$INSTDIR\wallet-install.id" r
    FileRead $0 $1
    FileClose $0
    StrCmp $1 "StealthWalletMonitor/v1" done
    MessageBox MB_OK|MB_ICONSTOP "This directory belongs to another application." /SD IDOK
    SetErrorLevel 2
    Abort
  done:
FunctionEnd

!macro StopBeforeChange
  FindWindow $0 "" "Stealth Wallet Monitor"
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Close the Stealth Wallet Monitor window, then run setup again." /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  IfFileExists "$INSTDIR\app\runtime\python.exe" 0 stopped
  ExecWait '"$INSTDIR\app\runtime\python.exe" -B "$INSTDIR\app\wallet_monitor.py" stop' $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Stop the monitor in its console, then try again." /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  stopped:
!macroend

Section "Wallet Monitor"
  SetShellVarContext current
  Call CheckDirectory
  !insertmacro StopBeforeChange
  SetOutPath "$INSTDIR\app"
  File /r "${PAYLOAD}\*"
  FileOpen $0 "$INSTDIR\wallet-install.id" w
  FileWrite $0 "StealthWalletMonitor/v1"
  FileClose $0
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  ${If} $NoIntegration == 1
    FileOpen $0 "$INSTDIR\no-shell-integration" w
    FileClose $0
  ${Else}
    Delete "$INSTDIR\no-shell-integration"
    WriteRegStr HKCU "Software\StealthWalletMonitor" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StealthWalletMonitor" "DisplayName" "Stealth Wallet Monitor"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StealthWalletMonitor" "DisplayVersion" "0.1.0"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StealthWalletMonitor" "DisplayIcon" "$INSTDIR\app\StealthWalletMonitor.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StealthWalletMonitor" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StealthWalletMonitor" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
    CreateDirectory "$SMPROGRAMS\Stealth Wallet Monitor"
    CreateShortcut "$SMPROGRAMS\Stealth Wallet Monitor\Stealth Wallet Monitor.lnk" "$INSTDIR\app\StealthWalletMonitor.exe"
    CreateShortcut "$SMPROGRAMS\Stealth Wallet Monitor\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  FileOpen $0 "$INSTDIR\wallet-install.id" r
  FileRead $0 $1
  FileClose $0
  ${If} $1 != "StealthWalletMonitor/v1"
    SetErrorLevel 2
    Abort
  ${EndIf}
  !insertmacro StopBeforeChange
  IfFileExists "$INSTDIR\no-shell-integration" skiplinks
  ReadRegStr $0 HKCU "Software\StealthWalletMonitor" "InstallDir"
  StrCmp $0 "$INSTDIR" 0 skiplinks
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StealthWalletMonitor"
  DeleteRegKey HKCU "Software\StealthWalletMonitor"
  Delete "$SMPROGRAMS\Stealth Wallet Monitor\Stealth Wallet Monitor.lnk"
  Delete "$SMPROGRAMS\Stealth Wallet Monitor\Uninstall.lnk"
  RMDir "$SMPROGRAMS\Stealth Wallet Monitor"
  skiplinks:
  !include "${CLEANUP}"
  Delete "$INSTDIR\wallet-install.id"
  Delete "$INSTDIR\no-shell-integration"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
