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
; It is written **outside `dist/`** on purpose: every `build.py --package` run
; wipes that folder, and the first copy of this preview was quietly deleted by
; the next build before anybody had run it.
;
; The window is built by hand rather than by MUI2, which is the whole point:
; MUI2 draws the grey wizard everybody recognises.
;
; ---------------------------------------------------------------------------
; What the first run on Windows found, and what it cost
; ---------------------------------------------------------------------------
;
; The first build of this compiled cleanly and was unusable. The screenshot is
; worth more than the description: every control this file *drew* was perfect —
; the cards, the radio dots, the headings, the step list, the close cross — and
; every control it asked **Windows** for was wrong. That split is the whole
; lesson, and it is why the rule below exists.
;
;   **A label is the only control here that takes our colours.** `SetCtlColors`
;   works by answering `WM_CTLCOLOR*`, and a push button ignores the brush it
;   is handed and paints its own face. The Browse button asked for the dark
;   surface colour and Windows drew it stock white, in the middle of a dark
;   window. So every button on these pages is a `${NSD_CreateLabel}` with
;   `SS_CENTER|SS_CENTERIMAGE` and a rounded region — nsDialogs gives every
;   label `SS_NOTIFY`, so it reports its own clicks and needs nothing else.
;
; Three more, each of which showed up as "it just does not work":
;
;   * **NSIS's own Next and Cancel never appeared.** They belong to
;     `$HWNDPARENT`, and the page dialog is sized over the whole window, so
;     they sat underneath it. Raising them by z-order was the previous attempt
;     and it did not hold. They are hidden outright now and the flow is driven
;     by posting `WM_COMMAND` to the parent, which is the same thing clicking
;     them would have done. The one button a user could find by hunting was
;     Cancel — so the report was "the next button quits".
;
;   * **The folder box's sunken 3D frame is an ex-style, not a theme.**
;     `nsDialogs.nsh` gives every text box `WS_EX_WINDOWEDGE|WS_EX_CLIENTEDGE`
;     (line 307), which `SetWindowTheme` cannot touch — that is a non-client
;     frame, drawn before the theme is consulted. It has to come off with
;     `SetWindowLong` and a `SWP_FRAMECHANGED`, and the box then sits inside a
;     rounded card that supplies the border we actually wanted.
;
;   * **A 15pt heading does not fit in a 20px label.** The name in the panel
;     was clipped through the middle of its descenders and the two lines ran
;     into each other. Segoe UI at 15pt needs about 26px of line box.
;
; The rule those four share: **anything Windows draws for itself has to be
; taken apart before it will match this design.** When a control here looks
; wrong on Windows, check what nsDialogs created it with before changing a
; colour — the answer has been in `nsDialogs.nsh` all four times.
;
; Known and deliberate: the window cannot be dragged. Making a borderless
; window movable means answering `WM_NCHITTEST`, and NSIS gives no way to
; subclass a window without a plugin — which rule 2 of this project forbids.

Unicode true

!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "WinMessages.nsh"

!define APP_VERSION "1.0.21"

Name "Internet Xtreme Downloader"
OutFile "..\..\XAI-notes\ixd-installer-preview.exe"
RequestExecutionLevel user
Caption "Internet Xtreme Downloader"
BrandingText " "
Icon "icons\ixd.ico"
XPStyle on

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
; `WinMessages.nsh` and `nsDialogs.nsh` already define some of these, so each
; is guarded: a redefinition is a hard error, and which ones those headers
; carry has changed between NSIS versions.
!ifndef GWL_STYLE
  !define GWL_STYLE -16
!endif
!ifndef GWL_EXSTYLE
  !define GWL_EXSTYLE -20
!endif
!ifndef WS_EX_WINDOWEDGE
  !define WS_EX_WINDOWEDGE 0x00000100
!endif
!ifndef WS_EX_CLIENTEDGE
  !define WS_EX_CLIENTEDGE 0x00000200
!endif
!define WS_CHROME      0x00CF0000   ; caption|sysmenu|thickframe|min|max
!define SWP_FRAMECHANGED 0x0020
!define SWP_NOZORDER     0x0004
!define SWP_NOMOVE       0x0002
!define SWP_NOSIZE       0x0001
!define IXD_HWND_BOTTOM  1
!ifndef SM_CXSCREEN
  !define SM_CXSCREEN 0
!endif
!ifndef SM_CYSCREEN
  !define SM_CYSCREEN 1
!endif

; SS_CENTER|SS_CENTERIMAGE, precomputed. `${NSD_AddStyle}` passes its argument
; to `System::Int64Op`, which takes one number and not an expression — writing
; `${SS_CENTER}|${SS_CENTERIMAGE}` there compiles and sets the wrong style.
!define SS_BUTTONTEXT 0x00000201

; Edit-control margins, for the folder box. `EM_SETMARGINS` is already in
; WinMessages.nsh (line 211); only the flag pair needs naming.
!define EC_BOTHMARGINS 0x0003

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
Var CardUserTitle
Var CardUserSub
Var CardAllTitle
Var CardAllSub
Var FolderCard
Var FolderText
Var Step
Var Tmp
Var Fill              ; the progress bar's filled part
Var FillWidth
Var DetailText
Var StartNow
Var StartBox

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

; A filled rectangle — a card, a dot, a progress track. A label with no text.
!macro Fill var x y w h radius colour
  ${NSD_CreateLabel} 0 0 10u 10u ""
  Pop ${var}
  SetCtlColors ${var} ${colour} ${colour}
  !insertmacro Place ${var} ${x} ${y} ${w} ${h}
  !insertmacro RoundCorners ${var} ${w} ${h} ${radius}
!macroend

; A button. See the header: a real push button cannot be coloured, so this is
; a label that centres its text both ways and reports its own clicks.
!macro Pill var text x y w h fg bg onclick
  ${NSD_CreateLabel} 0 0 10u 10u "${text}"
  Pop ${var}
  ${NSD_AddStyle} ${var} ${SS_BUTTONTEXT}
  SetCtlColors ${var} ${fg} ${bg}
  !insertmacro Font ${var} $FontButton
  !insertmacro Place ${var} ${x} ${y} ${w} ${h}
  !insertmacro RoundCorners ${var} ${w} ${h} 9
  ${NSD_OnClick} ${var} ${onclick}
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

  ; The page area is the whole window.
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

  Call HideStockButtons

  StrCpy $Mode "user"
  StrCpy $Step 0
  StrCpy $StartNow 1
FunctionEnd

; NSIS's three buttons are never shown. Restyling them was the previous
; attempt: they cannot take our colours, and they sit under a page dialog that
; covers the window. The pills post `WM_COMMAND` to the parent instead, which
; is exactly what clicking these would have sent.
Function HideStockButtons
  GetDlgItem $0 $HWNDPARENT 1
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 2
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 3
  ShowWindow $0 ${SW_HIDE}
FunctionEnd

Function GoNext
  Pop $0
  SendMessage $HWNDPARENT ${WM_COMMAND} 1 0
FunctionEnd

Function GoCancel
  Pop $0
  SendMessage $HWNDPARENT ${WM_COMMAND} 2 0
FunctionEnd

Function OnClose
  Pop $0
  SendMessage $HWNDPARENT ${WM_CLOSE} 0 0
FunctionEnd

; The left-hand panel: the mark, the name, and where we are.
Function BrandPanel
  !insertmacro Fill $0 0 0 ${PANEL_W} ${WIN_H} 0 ${C_PANEL}

  ${NSD_CreateBitmap} 0 0 10u 10u ""
  Pop $0
  ${NSD_SetImage} $0 "$PLUGINSDIR\mark.bmp" $1
  !insertmacro Place $0 28 34 40 40

  ; 26px boxes, 26px apart. At 15pt Segoe UI a 20px box cut the letters through
  ; their descenders and the two lines collided — the first thing anyone
  ; noticed in the screenshot.
  !insertmacro Label $1 "Internet Xtreme" 28 84 200 26 $FontH1 ${C_TEXT} ${C_PANEL}
  !insertmacro Label $1 "Downloader" 28 110 200 26 $FontH1 ${C_TEXT} ${C_PANEL}
  !insertmacro Label $1 "Version ${APP_VERSION}" 28 144 200 18 $FontSmall ${C_FAINT} ${C_PANEL}

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
  ${NSD_AddStyle} $0 ${SS_BUTTONTEXT}
  SetCtlColors $0 ${C_DIM} ${C_BG}
  !insertmacro Font $0 $FontBody
  !insertmacro Place $0 664 16 28 28
  ${NSD_OnClick} $0 OnClose
FunctionEnd

; ---------------------------------------------------------------------------
; Page one — where it goes
; ---------------------------------------------------------------------------
Function PageWhere
  StrCpy $Step 0
  nsDialogs::Create 1018
  Pop $Dialog
  SetCtlColors $Dialog ${C_TEXT} ${C_BG}
  Call HideStockButtons
  Call BrandPanel

  !insertmacro Label $0 "Where should it go?" 284 44 380 30 $FontH1 ${C_TEXT} ${C_BG}
  !insertmacro Label $0 "Two answers, and they are not the same choice." \
      284 78 380 20 $FontBody ${C_DIM} ${C_BG}

  ; Card one.
  !insertmacro Fill $CardUser 284 118 402 62 11 ${C_CARD}
  ${NSD_OnClick} $CardUser OnPickUser

  !insertmacro Fill $CardUserDot 306 141 16 16 16 ${C_ACCENT}

  !insertmacro Label $CardUserTitle "Just me" 332 130 340 20 $FontButton ${C_TEXT} ${C_CARD}
  ${NSD_OnClick} $CardUserTitle OnPickUser
  !insertmacro Label $CardUserSub "No administrator needed  ·  %APPDATA%\IXD" \
      332 151 340 18 $FontSmall ${C_DIM} ${C_CARD}
  ${NSD_OnClick} $CardUserSub OnPickUser

  ; Card two.
  !insertmacro Fill $CardAll 284 190 402 62 11 ${C_SURFACE}
  ${NSD_OnClick} $CardAll OnPickAll

  !insertmacro Fill $CardAllDot 306 213 16 16 16 ${C_FAINT}

  !insertmacro Label $CardAllTitle "Everyone on this PC" 332 202 340 20 $FontButton ${C_TEXT} ${C_SURFACE}
  ${NSD_OnClick} $CardAllTitle OnPickAll
  !insertmacro Label $CardAllSub "Needs administrator  ·  Program Files" \
      332 223 340 18 $FontSmall ${C_DIM} ${C_SURFACE}
  ${NSD_OnClick} $CardAllSub OnPickAll

  !insertmacro Label $0 "FOLDER" 284 274 200 16 $FontSmall ${C_DIM} ${C_BG}

  ; The box first, its card afterwards, and the card pushed to the bottom of
  ; the z-order **explicitly**.
  ;
  ; Three rounds went into that sentence. A label carries WS_EX_TRANSPARENT
  ; (`nsDialogs.nsh:263`) and a transparent sibling paints *after* the windows
  ; beneath it, so a filled label laid over the box hid it. Stripping the
  ; ex-style did not help either: in this dialog the *earlier*-created control
  ; is the one on top, so a card created first covers a box created second
  ; whatever its ex-style says. Both observations fit, and both were guesses
  ; about ordering — so the ordering is no longer guessed. `SetWindowPos` with
  ; HWND_BOTTOM says where the card goes and nothing has to be inferred.
  ;
  ; The card exists so the box can be text-height: a single-line EDIT does
  ; **not** centre its text vertically. It draws at the top of whatever height
  ; it is given, which is why the path sat against the ceiling of a 38px box.
  ${NSD_CreateText} 0 0 10u 10u "$APPDATA\IXD"
  Pop $FolderText
  SetCtlColors $FolderText ${C_TEXT} ${C_SURFACE}
  System::Call 'uxtheme::SetWindowTheme(p $FolderText, w " ", w " ")'
  !insertmacro Font $FolderText $FontBody

  ; Off with the sunken frame. `nsDialogs.nsh` line 307 gives every text box
  ; WS_EX_WINDOWEDGE|WS_EX_CLIENTEDGE; that is non-client, so no amount of
  ; theming or colouring reaches it, and it has to be recalculated with
  ; SWP_FRAMECHANGED before Windows stops drawing it.
  System::Call 'user32::GetWindowLongW(p $FolderText, i ${GWL_EXSTYLE}) i .r0'
  IntOp $1 ${WS_EX_CLIENTEDGE} | ${WS_EX_WINDOWEDGE}
  IntOp $1 $1 ~
  IntOp $0 $0 & $1
  System::Call 'user32::SetWindowLongW(p $FolderText, i ${GWL_EXSTYLE}, i r0)'
  IntOp $2 ${SWP_FRAMECHANGED} | ${SWP_NOMOVE}
  IntOp $2 $2 | ${SWP_NOSIZE}
  IntOp $2 $2 | ${SWP_NOZORDER}
  System::Call 'user32::SetWindowPos(p $FolderText, p 0, i 0, i 0, i 0, i 0, i r2)'

  ; Text height, centred inside the card by arithmetic rather than by hoping
  ; the control does it: 296 + (38 - 18) / 2 = 306.
  SendMessage $FolderText ${EM_SETMARGINS} ${EC_BOTHMARGINS} 0x00040004
  !insertmacro Place $FolderText 296 306 272 18

  !insertmacro Fill $FolderCard 284 296 296 38 9 ${C_SURFACE}
  ${NSD_RemoveExStyle} $FolderCard ${WS_EX_TRANSPARENT}
  IntOp $0 ${SWP_NOMOVE} | ${SWP_NOSIZE}
  System::Call 'user32::SetWindowPos(p $FolderCard, p ${IXD_HWND_BOTTOM}, \
      i 0, i 0, i 0, i 0, i r0)'

  !insertmacro Pill $0 "Browse" 594 296 92 38 ${C_TEXT} ${C_SURFACE} OnBrowse

  !insertmacro Pill $0 "Cancel" 438 394 112 38 ${C_TEXT} ${C_SURFACE} GoCancel
  !insertmacro Pill $0 "Install" 560 394 132 38 ${C_WHITE} ${C_ACCENT} GoNext

  nsDialogs::Show
FunctionEnd

; Both handlers repaint *everything* that belongs to a card. The labels sitting
; on a card carry their own background, and only the card itself was being
; swapped — so the chosen card changed colour and the words on it kept the old
; one, which reads as the box changing colour when the mode changes.
Function OnPickUser
  Pop $0
  StrCpy $Mode "user"
  SetCtlColors $CardUser ${C_TEXT} ${C_CARD}
  SetCtlColors $CardUserTitle ${C_TEXT} ${C_CARD}
  SetCtlColors $CardUserSub ${C_DIM} ${C_CARD}
  SetCtlColors $CardAll ${C_TEXT} ${C_SURFACE}
  SetCtlColors $CardAllTitle ${C_TEXT} ${C_SURFACE}
  SetCtlColors $CardAllSub ${C_DIM} ${C_SURFACE}
  SetCtlColors $CardUserDot ${C_ACCENT} ${C_ACCENT}
  SetCtlColors $CardAllDot ${C_FAINT} ${C_FAINT}
  ${NSD_SetText} $FolderText "$APPDATA\IXD"
  Call RestateFolder
FunctionEnd

Function OnPickAll
  Pop $0
  StrCpy $Mode "all"
  SetCtlColors $CardAll ${C_TEXT} ${C_CARD}
  SetCtlColors $CardAllTitle ${C_TEXT} ${C_CARD}
  SetCtlColors $CardAllSub ${C_DIM} ${C_CARD}
  SetCtlColors $CardUser ${C_TEXT} ${C_SURFACE}
  SetCtlColors $CardUserTitle ${C_TEXT} ${C_SURFACE}
  SetCtlColors $CardUserSub ${C_DIM} ${C_SURFACE}
  SetCtlColors $CardAllDot ${C_ACCENT} ${C_ACCENT}
  SetCtlColors $CardUserDot ${C_FAINT} ${C_FAINT}
  ${NSD_SetText} $FolderText "$PROGRAMFILES64\IXD"
  Call RestateFolder
FunctionEnd

; The folder box belongs to neither card and must not follow either of them.
; Said again on every change, and the caret dropped, because a box holding the
; selection after a click reads as a third colour nobody chose.
Function RestateFolder
  SetCtlColors $FolderCard ${C_SURFACE} ${C_SURFACE}
  SetCtlColors $FolderText ${C_TEXT} ${C_SURFACE}
  SendMessage $FolderText ${EM_SETSEL} 0 0
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

; ---------------------------------------------------------------------------
; Page two — the install itself
; ---------------------------------------------------------------------------
;
; A custom page rather than `instfiles`. NSIS's own install page brings its
; own dialog, and controls cannot be added to it through nsDialogs — the two
; headings the previous version drew here were created against the *previous*
; page's dialog, which NSIS had already destroyed, so they never appeared and
; this page had no left-hand panel at all. Nothing is installed either way, so
; the bar and the running commentary are drawn here, on the same panel as
; every other page, and the page moves itself on when it reaches the end.
Function PageInstall
  StrCpy $Step 1
  nsDialogs::Create 1018
  Pop $Dialog
  SetCtlColors $Dialog ${C_TEXT} ${C_BG}
  Call HideStockButtons
  Call BrandPanel

  !insertmacro Label $0 "Installing" 284 44 380 30 $FontH1 ${C_TEXT} ${C_BG}
  !insertmacro Label $0 "About twenty seconds — it is a whole runtime." \
      284 78 380 20 $FontBody ${C_DIM} ${C_BG}

  ; Flat, like the application's own. A real msctls_progress32 arrives with
  ; WS_EX_CLIENTEDGE and a system-drawn chunk, which is the grey wizard again.
  !insertmacro Fill $0 284 150 402 8 4 ${C_SURFACE}
  !insertmacro Fill $Fill 284 150 402 8 4 ${C_ACCENT}
  StrCpy $FillWidth 0
  !insertmacro Place $Fill 284 150 1 8

  !insertmacro Label $DetailText "Extracting ixd.exe" \
      284 176 402 20 $FontSmall ${C_DIM} ${C_BG}

  ${NSD_CreateTimer} OnInstallTick 60
  nsDialogs::Show
FunctionEnd

Function OnInstallTick
  IntOp $FillWidth $FillWidth + 5
  ${If} $FillWidth >= 402
    ${NSD_KillTimer} OnInstallTick
    ${NSD_SetText} $DetailText "Nothing was installed — this is a preview."
    !insertmacro Place $Fill 284 150 402 8
    ; Posted, not sent: this runs inside the dialog's own timer handler, and
    ; tearing the page down from underneath it is how an installer disappears
    ; with no window and no error.
    System::Call 'user32::PostMessageW(p $HWNDPARENT, i ${WM_COMMAND}, p 1, p 0)'
    Return
  ${EndIf}
  !insertmacro Place $Fill 284 150 $FillWidth 8

  ${If} $FillWidth > 320
    ${NSD_SetText} $DetailText "Writing the uninstaller"
  ${ElseIf} $FillWidth > 240
    ${NSD_SetText} $DetailText "Extracting PySide6\Qt6Gui.dll"
  ${ElseIf} $FillWidth > 160
    ${NSD_SetText} $DetailText "Extracting PySide6\Qt6Core.dll"
  ${ElseIf} $FillWidth > 80
    ${NSD_SetText} $DetailText "Extracting _internal\base_library.zip"
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------------------
; Page three — done
; ---------------------------------------------------------------------------
Function PageDone
  StrCpy $Step 2
  nsDialogs::Create 1018
  Pop $Dialog
  SetCtlColors $Dialog ${C_TEXT} ${C_BG}
  Call HideStockButtons
  Call BrandPanel

  ${NSD_CreateBitmap} 0 0 10u 10u ""
  Pop $0
  ${NSD_SetImage} $0 "$PLUGINSDIR\tick.bmp" $1
  !insertmacro Place $0 284 44 36 36

  !insertmacro Label $0 "Installed" 334 44 340 30 $FontH1 ${C_TEXT} ${C_BG}
  !insertmacro Label $0 "One step left, and it is in the browser." \
      334 78 340 20 $FontBody ${C_DIM} ${C_BG}

  !insertmacro Fill $0 284 130 402 96 11 ${C_SURFACE}

  !insertmacro Label $0 "Load the extension" 302 144 370 20 $FontButton ${C_TEXT} ${C_SURFACE}
  !insertmacro Label $0 "Open chrome://extensions, turn on Developer mode and load$\r$\nthe folder beside the application. The app shows the exact path." \
      302 168 370 40 $FontSmall ${C_DIM} ${C_SURFACE}

  ; A drawn tick box. A real check box is a BUTTON, and a BUTTON paints its own
  ; face — the same reason the Browse button came out white.
  !insertmacro Fill $StartBox 284 246 18 18 5 ${C_ACCENT}
  ${NSD_OnClick} $StartBox OnToggleStart
  !insertmacro Label $0 "Start Internet Xtreme Downloader now" \
      312 245 360 20 $FontBody ${C_TEXT} ${C_BG}
  ${NSD_OnClick} $0 OnToggleStart

  !insertmacro Label $0 "This is a preview: it has installed nothing." \
      284 296 402 20 $FontSmall ${C_FAINT} ${C_BG}

  !insertmacro Pill $0 "Finish" 560 394 132 38 ${C_WHITE} ${C_ACCENT} GoNext

  nsDialogs::Show
FunctionEnd

Function OnToggleStart
  Pop $0
  ${If} $StartNow == 1
    StrCpy $StartNow 0
    SetCtlColors $StartBox ${C_FAINT} ${C_SURFACE}
  ${Else}
    StrCpy $StartNow 1
    SetCtlColors $StartBox ${C_ACCENT} ${C_ACCENT}
  ${EndIf}
  Call Repaint
FunctionEnd

; ---------------------------------------------------------------------------
Page custom PageWhere
Page custom PageInstall
Page custom PageDone

Function .onInit
  InitPluginsDir
  File /oname=$PLUGINSDIR\mark.bmp "installer-art\mark.bmp"
  File /oname=$PLUGINSDIR\tick.bmp "installer-art\tick.bmp"
FunctionEnd

; "no sections will be executed" is the correct state for this file and not
; something to fix: there is no `instfiles` page because nothing is installed.
; Silenced so a real warning in a later edit is not lost in an expected one.
!pragma warning disable 8000

Section "Preview"
  ; Nothing is written, and nothing runs here: the progress on page two is
  ; drawn by that page. NSIS requires a section to produce an installer at all.
SectionEnd
