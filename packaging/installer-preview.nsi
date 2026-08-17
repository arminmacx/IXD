; A preview of the custom installer window.
;
; It shows the real thing — the borderless dark window, the three pages, the
; controls and the flow — and **installs nothing**. That is deliberate: this
; machine builds Linux, so the only Windows payload it could put inside would
; be the wrong one, and an installer that puts a Linux binary on a Windows
; machine is worse than no sample at all. The final page says so on screen.
;
; Everything here is stock NSIS: nsDialogs and System.dll ship with it, and no
; plugin is downloaded or vendored. Compile with:
;
;     makensis -V2 packaging/installer-preview.nsi
;
; The window is built by hand rather than by MUI2, which is the whole point:
; MUI2 draws the grey wizard everybody recognises.

Unicode true

!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "WinMessages.nsh"

Name "Internet Xtreme Downloader"
OutFile "..\dist\ixd-installer-preview.exe"
RequestExecutionLevel user
Caption "Internet Xtreme Downloader"
BrandingText " "
Icon "icons\ixd.ico"
XPStyle on
ShowInstDetails show

; ---------------------------------------------------------------------------
; The window
; ---------------------------------------------------------------------------
!define WIN_W 720
!define WIN_H 460
!define PANEL_W 250

; Colours, straight out of `ixd/ui/theme.py` so the installer and the
; application are the same object rather than two things that resemble each
; other.
!define C_BG      0x0D0F16
!define C_PANEL   0x0A0C12
!define C_SURFACE 0x141827
!define C_CARD    0x181D2F
!define C_TEXT    0xE7ECFF
!define C_DIM     0x95A0C2
!define C_FAINT   0x6B7597
!define C_ACCENT  0x5B8CFF
!define C_GOOD    0x43D6A0
!define C_WHITE   0xFFFFFF

; Win32 constants used through System.dll. Named, because a bare 0x00CF0000 in
; the middle of a script is a number nobody can check.
; `WinMessages.nsh` already defines some of these, so each is guarded: a
; redefinition is a hard error, and which ones that header carries has changed
; between NSIS versions.
!ifndef GWL_STYLE
  !define GWL_STYLE -16
!endif
!define WS_CHROME      0x00CF0000   ; caption|sysmenu|thickframe|min|max
!define SWP_FRAMECHANGED 0x0020
!define SWP_NOZORDER     0x0004
!define SWP_NOMOVE       0x0002
!define SWP_NOSIZE       0x0001
!define IXD_HWND_TOP     0
!ifndef SM_CXSCREEN
  !define SM_CXSCREEN 0
!endif
!ifndef SM_CYSCREEN
  !define SM_CYSCREEN 1
!endif

Var Dialog
Var FontH1
Var FontBody
Var FontSmall
Var FontButton
Var Mode              ; "user" or "all"
Var CardUser
Var CardAll
Var CardUserDot
Var CardAllDot
Var FolderText
Var BtnNext
Var BtnCancel
Var BtnBack
Var Step
Var Tmp

; ---------------------------------------------------------------------------
; Helpers
; ---------------------------------------------------------------------------

; Rounded corners on any control, by handing Windows a region to clip it to.
; This is what makes a flat colour look like the application's own buttons
; without owner-drawing anything.
!macro RoundCorners hwnd w h radius
  System::Call 'gdi32::CreateRoundRectRgn(i 0, i 0, i ${w}, i ${h}, i ${radius}, i ${radius}) p .s'
  Pop $Tmp
  System::Call 'user32::SetWindowRgn(p ${hwnd}, p $Tmp, i 1)'
!macroend

; Put a control exactly where the design says, in pixels. nsDialogs positions
; in dialog units, which depend on the system font — so every position here is
; set afterwards instead, and the layout is the same on every machine.
!macro Place hwnd x y w h
  System::Call 'user32::MoveWindow(p ${hwnd}, i ${x}, i ${y}, i ${w}, i ${h}, i 1)'
!macroend

!macro Font hwnd font
  SendMessage ${hwnd} ${WM_SETFONT} ${font} 1
!macroend

; A label with our colours, our font and an exact box.
!macro Label var text x y w h font fg bg
  ${NSD_CreateLabel} 0 0 10u 10u "${text}"
  Pop ${var}
  SetCtlColors ${var} ${fg} ${bg}
  !insertmacro Font ${var} ${font}
  !insertmacro Place ${var} ${x} ${y} ${w} ${h}
!macroend

Function .onGUIInit
  ; Strip the title bar and the resize frame, then size and centre what is
  ; left. NSIS gives no way to ask for a window like this, so it is asked of
  ; Windows directly.
  System::Call 'user32::GetWindowLongW(p $HWNDPARENT, i ${GWL_STYLE}) i .r0'
  IntOp $1 ${WS_CHROME} ~
  IntOp $0 $0 & $1
  System::Call 'user32::SetWindowLongW(p $HWNDPARENT, i ${GWL_STYLE}, i r0)'

  System::Call 'user32::GetSystemMetrics(i ${SM_CXSCREEN}) i .r2'
  System::Call 'user32::GetSystemMetrics(i ${SM_CYSCREEN}) i .r3'
  IntOp $2 $2 - ${WIN_W}
  IntOp $2 $2 / 2
  IntOp $3 $3 - ${WIN_H}
  IntOp $3 $3 / 2
  IntOp $4 ${SWP_FRAMECHANGED} | ${SWP_NOZORDER}
  System::Call 'user32::SetWindowPos(p $HWNDPARENT, p 0, i r2, i r3, \
      i ${WIN_W}, i ${WIN_H}, i r4)'

  ; Rounded, like every other window this project draws.
  !insertmacro RoundCorners $HWNDPARENT ${WIN_W} ${WIN_H} 14

  SetCtlColors $HWNDPARENT ${C_TEXT} ${C_BG}

  ; The page area is the whole window; the buttons are raised over it below.
  GetDlgItem $0 $HWNDPARENT 1018
  !insertmacro Place $0 0 0 ${WIN_W} ${WIN_H}

  ; The branding strip NSIS puts at the bottom left has no place here.
  GetDlgItem $0 $HWNDPARENT 1028
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1256
  ShowWindow $0 ${SW_HIDE}

  CreateFont $FontH1     "Segoe UI" 15 600
  CreateFont $FontBody   "Segoe UI" 10 400
  CreateFont $FontSmall  "Segoe UI" 9  400
  CreateFont $FontButton "Segoe UI" 10 600

  ; The three buttons NSIS owns. They keep driving the page flow — synthesising
  ; clicks would mean reimplementing it — and are moved, coloured and rounded
  ; into the ones in the design.
  GetDlgItem $BtnNext $HWNDPARENT 1
  GetDlgItem $BtnCancel $HWNDPARENT 2
  GetDlgItem $BtnBack $HWNDPARENT 3
  ShowWindow $BtnBack ${SW_HIDE}

  System::Call 'uxtheme::SetWindowTheme(p $BtnNext, w " ", w " ")'
  System::Call 'uxtheme::SetWindowTheme(p $BtnCancel, w " ", w " ")'
  SetCtlColors $BtnNext ${C_WHITE} ${C_ACCENT}
  SetCtlColors $BtnCancel ${C_TEXT} ${C_SURFACE}
  !insertmacro Font $BtnNext $FontButton
  !insertmacro Font $BtnCancel $FontButton
  !insertmacro Place $BtnNext 560 394 132 38
  !insertmacro Place $BtnCancel 438 394 112 38
  !insertmacro RoundCorners $BtnNext 132 38 9
  !insertmacro RoundCorners $BtnCancel 112 38 9

  StrCpy $Mode "user"
  StrCpy $Step 0
FunctionEnd

; Raise the buttons over the page dialog, which is created after them and
; would otherwise cover them.
Function RaiseButtons
  IntOp $0 ${SWP_NOMOVE} | ${SWP_NOSIZE}
  System::Call 'user32::SetWindowPos(p $BtnNext, p ${IXD_HWND_TOP}, i 0, i 0, i 0, i 0, i r0)'
  System::Call 'user32::SetWindowPos(p $BtnCancel, p ${IXD_HWND_TOP}, i 0, i 0, i 0, i 0, i r0)'
FunctionEnd

; The left-hand panel: the mark, the name, and where we are.
Function BrandPanel
  ${NSD_CreateLabel} 0 0 10u 10u ""
  Pop $0
  SetCtlColors $0 ${C_TEXT} ${C_PANEL}
  !insertmacro Place $0 0 0 ${PANEL_W} ${WIN_H}

  ${NSD_CreateBitmap} 0 0 10u 10u ""
  Pop $0
  ${NSD_SetImage} $0 "$PLUGINSDIR\mark.bmp" $1
  !insertmacro Place $0 28 34 40 40

  !insertmacro Label $1 "Internet Xtreme" 28 86 200 20 $FontH1 ${C_TEXT} ${C_PANEL}
  !insertmacro Label $1 "Downloader" 28 106 200 20 $FontH1 ${C_TEXT} ${C_PANEL}
  !insertmacro Label $1 "Version 1.0.16" 28 132 200 16 $FontSmall ${C_FAINT} ${C_PANEL}

  StrCpy $2 0
  StrCpy $3 200
  ${Do}
    ${If} $2 == 0
      StrCpy $4 "Where it goes"
    ${ElseIf} $2 == 1
      StrCpy $4 "Installing"
    ${Else}
      StrCpy $4 "Ready"
    ${EndIf}

    ; The dot: green behind us, accent where we are, grey ahead.
    ;
    ; Every colour is written as a literal. `SetCtlColors` reads its arguments
    ; at compile time, so `SetCtlColors $6 $5 $5` compiles without a word of
    ; complaint and paints black — the branch has to carry the constant rather
    ; than a variable holding it.
    ${NSD_CreateLabel} 0 0 10u 10u ""
    Pop $6
    ${If} $2 < $Step
      SetCtlColors $6 ${C_GOOD} ${C_GOOD}
    ${ElseIf} $2 == $Step
      SetCtlColors $6 ${C_ACCENT} ${C_ACCENT}
    ${Else}
      SetCtlColors $6 ${C_FAINT} ${C_FAINT}
    ${EndIf}
    IntOp $7 $3 + 4
    !insertmacro Place $6 28 $7 11 11
    !insertmacro RoundCorners $6 11 11 11

    ${NSD_CreateLabel} 0 0 10u 10u "$4"
    Pop $6
    ${If} $2 == $Step
      SetCtlColors $6 ${C_TEXT} ${C_PANEL}
      StrCpy $8 $FontButton
    ${ElseIf} $2 < $Step
      SetCtlColors $6 ${C_DIM} ${C_PANEL}
      StrCpy $8 $FontBody
    ${Else}
      SetCtlColors $6 ${C_FAINT} ${C_PANEL}
      StrCpy $8 $FontBody
    ${EndIf}
    !insertmacro Font $6 $8
    !insertmacro Place $6 52 $3 180 20

    IntOp $3 $3 + 44
    IntOp $2 $2 + 1
  ${LoopUntil} $2 >= 3

  !insertmacro Label $1 "No ffmpeg. No yt-dlp. No telemetry." \
      28 420 210 16 $FontSmall ${C_FAINT} ${C_PANEL}

  ; The close cross, top right of the content side.
  ; No `$` in front of it: NSIS reads `$x` as a variable and drops the glyph.
  ${NSD_CreateLabel} 0 0 10u 10u "✕"
  Pop $0
  SetCtlColors $0 ${C_DIM} ${C_BG}
  !insertmacro Font $0 $FontBody
  !insertmacro Place $0 668 18 24 24
  ${NSD_OnClick} $0 OnClose
FunctionEnd

Function OnClose
  SendMessage $HWNDPARENT ${WM_CLOSE} 0 0
FunctionEnd

; ---------------------------------------------------------------------------
; Page one — where it goes
; ---------------------------------------------------------------------------
Function PageWhere
  StrCpy $Step 0
  nsDialogs::Create 1018
  Pop $Dialog
  SetCtlColors $Dialog ${C_TEXT} ${C_BG}
  Call BrandPanel

  !insertmacro Label $0 "Where should it go?" 284 44 380 30 $FontH1 ${C_TEXT} ${C_BG}
  !insertmacro Label $0 "Two answers, and they are not the same choice." \
      284 78 380 20 $FontBody ${C_DIM} ${C_BG}

  ; Card one.
  ${NSD_CreateLabel} 0 0 10u 10u ""
  Pop $CardUser
  SetCtlColors $CardUser ${C_TEXT} ${C_CARD}
  !insertmacro Place $CardUser 284 118 402 62
  !insertmacro RoundCorners $CardUser 402 62 11
  ${NSD_OnClick} $CardUser OnPickUser

  ${NSD_CreateLabel} 0 0 10u 10u ""
  Pop $CardUserDot
  SetCtlColors $CardUserDot ${C_ACCENT} ${C_ACCENT}
  !insertmacro Place $CardUserDot 306 141 16 16
  !insertmacro RoundCorners $CardUserDot 16 16 16

  !insertmacro Label $0 "Just me" 332 130 340 20 $FontButton ${C_TEXT} ${C_CARD}
  ${NSD_OnClick} $0 OnPickUser
  !insertmacro Label $0 "No administrator needed  ·  %APPDATA%\IXD" \
      332 151 340 18 $FontSmall ${C_DIM} ${C_CARD}
  ${NSD_OnClick} $0 OnPickUser

  ; Card two.
  ${NSD_CreateLabel} 0 0 10u 10u ""
  Pop $CardAll
  SetCtlColors $CardAll ${C_TEXT} ${C_SURFACE}
  !insertmacro Place $CardAll 284 190 402 62
  !insertmacro RoundCorners $CardAll 402 62 11
  ${NSD_OnClick} $CardAll OnPickAll

  ${NSD_CreateLabel} 0 0 10u 10u ""
  Pop $CardAllDot
  SetCtlColors $CardAllDot ${C_FAINT} ${C_FAINT}
  !insertmacro Place $CardAllDot 306 213 16 16
  !insertmacro RoundCorners $CardAllDot 16 16 16

  !insertmacro Label $0 "Everyone on this PC" 332 202 340 20 $FontButton ${C_TEXT} ${C_SURFACE}
  ${NSD_OnClick} $0 OnPickAll
  !insertmacro Label $0 "Needs administrator  ·  Program Files" \
      332 223 340 18 $FontSmall ${C_DIM} ${C_SURFACE}
  ${NSD_OnClick} $0 OnPickAll

  !insertmacro Label $0 "FOLDER" 284 274 200 16 $FontSmall ${C_DIM} ${C_BG}

  ${NSD_CreateText} 0 0 10u 10u "$APPDATA\IXD"
  Pop $FolderText
  SetCtlColors $FolderText ${C_TEXT} ${C_SURFACE}
  System::Call 'uxtheme::SetWindowTheme(p $FolderText, w " ", w " ")'
  !insertmacro Font $FolderText $FontBody
  !insertmacro Place $FolderText 284 296 296 38
  !insertmacro RoundCorners $FolderText 296 38 9

  ${NSD_CreateButton} 0 0 10u 10u "Browse"
  Pop $0
  System::Call 'uxtheme::SetWindowTheme(p $0, w " ", w " ")'
  SetCtlColors $0 ${C_TEXT} ${C_SURFACE}
  !insertmacro Font $0 $FontBody
  !insertmacro Place $0 594 296 92 38
  !insertmacro RoundCorners $0 92 38 9
  ${NSD_OnClick} $0 OnBrowse

  SendMessage $BtnNext ${WM_SETTEXT} 0 "STR:Install"
  Call RaiseButtons
  nsDialogs::Show
FunctionEnd

Function OnPickUser
  Pop $0
  StrCpy $Mode "user"
  SetCtlColors $CardUser ${C_TEXT} ${C_CARD}
  SetCtlColors $CardAll ${C_TEXT} ${C_SURFACE}
  SetCtlColors $CardUserDot ${C_ACCENT} ${C_ACCENT}
  SetCtlColors $CardAllDot ${C_FAINT} ${C_FAINT}
  ${NSD_SetText} $FolderText "$APPDATA\IXD"
  Call Repaint
FunctionEnd

Function OnPickAll
  Pop $0
  StrCpy $Mode "all"
  SetCtlColors $CardAll ${C_TEXT} ${C_CARD}
  SetCtlColors $CardUser ${C_TEXT} ${C_SURFACE}
  SetCtlColors $CardAllDot ${C_ACCENT} ${C_ACCENT}
  SetCtlColors $CardUserDot ${C_FAINT} ${C_FAINT}
  ${NSD_SetText} $FolderText "$PROGRAMFILES64\IXD"
  Call Repaint
FunctionEnd

Function Repaint
  System::Call 'user32::InvalidateRect(p $Dialog, p 0, i 1)'
FunctionEnd

Function OnBrowse
  Pop $0
  ${NSD_GetText} $FolderText $1
  nsDialogs::SelectFolderDialog "Where should it go?" "$1"
  Pop $2
  ${If} $2 != error
    ${NSD_SetText} $FolderText "$2"
  ${EndIf}
FunctionEnd

Function PageWhereLeave
  StrCpy $Step 1
FunctionEnd

; ---------------------------------------------------------------------------
; Page two — the install itself, restyled rather than replaced
; ---------------------------------------------------------------------------
Function PageInstallShow
  StrCpy $Step 1
  ; NSIS owns this page: the progress bar and the details list are its own
  ; controls, so they are moved and coloured instead of rebuilt.
  FindWindow $0 "#32770" "" $HWNDPARENT
  SetCtlColors $0 ${C_TEXT} ${C_BG}

  GetDlgItem $1 $0 1004        ; the "show details" button
  ShowWindow $1 ${SW_HIDE}
  GetDlgItem $1 $0 1006        ; the step label above the bar
  SetCtlColors $1 ${C_TEXT} ${C_BG}
  !insertmacro Font $1 $FontBody
  !insertmacro Place $1 284 168 402 20

  GetDlgItem $1 $0 1004
  GetDlgItem $2 $0 1016        ; the progress bar
  System::Call 'uxtheme::SetWindowTheme(p $2, w " ", w " ")'
  SendMessage $2 0x0409 0 ${C_ACCENT}      ; PBM_SETBARCOLOR
  SendMessage $2 0x2001 0 ${C_SURFACE}     ; PBM_SETBKCOLOR
  !insertmacro Place $2 284 150 402 8

  GetDlgItem $3 $0 1000        ; the details list
  SetCtlColors $3 ${C_FAINT} ${C_BG}
  !insertmacro Font $3 $FontSmall
  !insertmacro Place $3 284 226 402 140

  ; The panel and the headings are ours, drawn on the parent behind it.
  !insertmacro Label $4 "Installing" 284 44 380 30 $FontH1 ${C_TEXT} ${C_BG}
  !insertmacro Label $4 "About twenty seconds — it is a whole runtime." \
      284 78 380 20 $FontBody ${C_DIM} ${C_BG}

  EnableWindow $BtnNext 0
  Call RaiseButtons
FunctionEnd

; ---------------------------------------------------------------------------
; Page three — done
; ---------------------------------------------------------------------------
Function PageDone
  StrCpy $Step 2
  nsDialogs::Create 1018
  Pop $Dialog
  SetCtlColors $Dialog ${C_TEXT} ${C_BG}
  Call BrandPanel

  ${NSD_CreateBitmap} 0 0 10u 10u ""
  Pop $0
  ${NSD_SetImage} $0 "$PLUGINSDIR\tick.bmp" $1
  !insertmacro Place $0 284 44 36 36

  !insertmacro Label $0 "Installed" 334 44 340 30 $FontH1 ${C_TEXT} ${C_BG}
  !insertmacro Label $0 "One step left, and it is in the browser." \
      334 78 340 20 $FontBody ${C_DIM} ${C_BG}

  ${NSD_CreateLabel} 0 0 10u 10u ""
  Pop $0
  SetCtlColors $0 ${C_TEXT} ${C_SURFACE}
  !insertmacro Place $0 284 130 402 96
  !insertmacro RoundCorners $0 402 96 11

  !insertmacro Label $0 "Load the extension" 302 144 370 20 $FontButton ${C_TEXT} ${C_SURFACE}
  !insertmacro Label $0 "Open chrome://extensions, turn on Developer mode and load$\r$\nthe folder beside the application. The app shows the exact path." \
      302 168 370 40 $FontSmall ${C_DIM} ${C_SURFACE}

  ${NSD_CreateCheckBox} 0 0 10u 10u "  Start Internet Xtreme Downloader now"
  Pop $0
  System::Call 'uxtheme::SetWindowTheme(p $0, w " ", w " ")'
  SetCtlColors $0 ${C_TEXT} ${C_BG}
  !insertmacro Font $0 $FontBody
  !insertmacro Place $0 284 246 380 22
  ${NSD_Check} $0

  !insertmacro Label $0 "This is a preview: it has installed nothing." \
      284 296 402 20 $FontSmall ${C_FAINT} ${C_BG}

  SendMessage $BtnNext ${WM_SETTEXT} 0 "STR:Finish"
  ShowWindow $BtnCancel ${SW_HIDE}
  EnableWindow $BtnNext 1
  Call RaiseButtons
  nsDialogs::Show
FunctionEnd

; ---------------------------------------------------------------------------
Page custom PageWhere PageWhereLeave
Page instfiles "" PageInstallShow
Page custom PageDone

Function .onInit
  InitPluginsDir
  File /oname=$PLUGINSDIR\mark.bmp "installer-art\mark.bmp"
  File /oname=$PLUGINSDIR\tick.bmp "installer-art\tick.bmp"
FunctionEnd

Section "Preview"
  ; Nothing is written. The steps are named so the details list has something
  ; true to show while the bar moves.
  DetailPrint "Extracting ixd.exe"
  Sleep 400
  DetailPrint "Extracting _internal\base_library.zip"
  Sleep 400
  DetailPrint "Extracting PySide6\Qt6Core.dll"
  Sleep 400
  DetailPrint "Extracting PySide6\Qt6Gui.dll"
  Sleep 400
  DetailPrint "Writing the uninstaller"
  Sleep 400
  DetailPrint "Nothing was installed — this is a preview."
SectionEnd
