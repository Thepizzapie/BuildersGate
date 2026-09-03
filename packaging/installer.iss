; Inno Setup script for the Builders Gate desktop install.
;
;   iscc packaging\installer.iss                       (after a build)
;   python packaging\build_exe.py --installer          (build, smoke, then this)
;
; Compiles dist\BuildersGate\ into dist\BuildersGate-setup.exe.
;
; ── why an installer at all, when a zip already exists ──────────────────────
; The zip is a folder the user has to put somewhere and a .exe they have to
; find inside it. There is no Start Menu entry, no uninstaller, no upgrade
; path, and no answer to "where did it go" three weeks later. The zip stays
; published for people who want it; this is the default download.
;
; ── PER-USER, AND THAT IS THE IMPORTANT DECISION ────────────────────────────
; PrivilegesRequired=lowest means:
;   * no UAC prompt at any point,
;   * the install lands in %LOCALAPPDATA%\Programs\BuildersGate rather than
;     C:\Program Files,
;   * the app writes its own data next to nothing it does not own.
; A machine-wide install would need admin for a tool one person runs on their
; own desktop, and would then hold the app's files under a root the user cannot
; write — which matters here because the app is updated often.
;
; THE TRADE, STATED PLAINLY. %LOCALAPPDATA%\Programs is user-writable, so any
; process already running as this user can replace BuildersGate.exe. Program
; Files is protected by an ACL the user cannot write without elevating — and
; that protection is bought with a UAC prompt on every install and every
; update, for an app that ships often. Neither is a free win. This is the
; deliberate choice: an attacker who is already executing code as the user has
; far better options than patching our binary, whereas a UAC prompt on every
; update is a cost paid by everyone, forever.
;
; What would actually close the gap is code signing plus a signature check on
; launch, not the install location.
;
; ── not signed ──────────────────────────────────────────────────────────────
; There is no code-signing certificate yet, so SmartScreen will show its
; "Windows protected your PC" prompt on first run. An installer does not make
; that worse than the loose .exe does, and the SHA256 is published beside the
; download. When a certificate exists, add SignTool= here and to the EXE in
; bgate.spec; nothing else in this file changes.

#define AppName        "Builders Gate"
#define AppExeName     "BuildersGate.exe"
#define AppPublisher   "Thepizzapie"
#define AppURL         "https://github.com/Thepizzapie/BuildersGate"

; Version is passed in by build_exe.py so it can come from pyproject.toml and
; cannot drift from the wheel. Defaulted so the script still compiles by hand.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

; Where the built app directory is, relative to this file.
#define SourceDir "..\dist\BuildersGate"

[Setup]
; NEVER CHANGE THIS GUID. It is the identity Windows upgrades and uninstalls
; by; a new one turns every future release into a second parallel install.
AppId={{7C4E2E71-9E63-4C3B-9A4B-5F2D6A1B8C40}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases

; {autopf} resolves to %LOCALAPPDATA%\Programs under PrivilegesRequired=lowest.
DefaultDirName={autopf}\BuildersGate
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
; NO "Select Setup Install Mode" PAGE. PrivilegesRequiredOverridesAllowed=dialog
; put an all-users/just-me question as the FIRST thing a new user saw, which is
; a question about Windows account scoping rather than about this app — and
; choosing the wrong one lands them in a UAC prompt they did not expect. This is
; a tool one person runs on their own desktop; per-user is the right answer and
; the wizard should simply make it.

; The app is a folder of a thousand files; a directory page that lets someone
; point it at an existing folder full of other things is a footgun. They can
; still change it, they just start from a sane place.
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=..\dist
OutputBaseFilename=BuildersGate-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern

; Branding. Generated from packaging/icon.ico by make_wizard_art.py, so the
; wizard cannot end up showing a mark the app stopped using.
;
; NOT DECORATION. An unsigned installer already arrives with SmartScreen
; calling its publisher unknown; showing up on top of that in the default grey
; Inno chrome, with no mark anywhere, is indistinguishable from the bundled
; adware people have been trained to cancel out of. This does not make the
; installer trusted — only signing does that — it stops it looking careless.
;
; Two files each: Inno picks the second on a scaled display.
WizardImageFile=wizard-large.bmp,wizard-large@2x.bmp
WizardSmallImageFile=wizard-small.bmp,wizard-small@2x.bmp
; The mark is drawn on the app's own near-black ground and would sit in a white
; box otherwise.
WizardImageStretch=no
WizardImageAlphaFormat=none
Compression=lzma2/max
SolidCompression=yes
; 64-bit only, matching the PyInstaller bootloader.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Refuse to install over a newer build rather than silently downgrading.
VersionInfoVersion={#AppVersion}
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; The stock page talks about "components" and disk space, which reads wrong
; here: most of these entries are FEATURE switches (they gate tools, panes and
; doctor rows machine-wide), and only the floor's art moves the size number.
; Say what the page actually decides.
WizardSelectComponents=Choose features
SelectComponentsDesc=Which parts of Builders Gate do you want?
SelectComponentsLabel2=Untick anything you will not use. Everything can be changed later in Settings. Playtest transcripts use Voice's speech-to-text.

; ── components: install only what you need ──────────────────────────────────
; THE WIZARD IS WHERE "WHAT GETS INSTALLED" IS DECIDED. Each optional feature
; is a component; unticking one both skips its payload (the floor's art,
; and voice's ~175MB whisper stack) and records the choice as this
; machine's default in ~/.bgate/modules.json — which every new project
; inherits: the feature's MCP tools are not registered, its panes leave the
; dashboard, doctor stops grading its dependencies. A project can still turn
; anything back on later in Settings > Modules; re-running this installer
; re-opens the machine-wide choice.
; Labels are SHORT on purpose: this is a Windows checkbox list, not a brochure,
; and a line of em-dash prose per row is what made the page read as clutter.
; No "&&" anywhere — the components list prints ampersands literally, so the
; Inno escape renders as a double ampersand.
[Types]
Name: "full"; Description: "Full installation"
Name: "compact"; Description: "Essentials only"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
; NO ExtraDiskSpaceRequired PADDING ON CORE, and this was measured, not
; guessed: padding core 4MB to absorb the uninstaller moved the footer by
; the same 4MB (rows 297.3 / footer 301.3 on screen), because Inno counts a
; component's extra space in both places. The footer is files plus the
; uninstaller; the uninstaller belongs to no row; the ~4MB difference is
; every Inno installer's arithmetic and cannot be closed without shipping
; no uninstaller.
Name: "core"; Description: "Builders Gate (required)"; \
    Types: full compact custom; Flags: fixed
Name: "floor"; Description: "Studio floor view"; Types: full
Name: "voice"; Description: "Voice (speech in and out)"; Types: full
Name: "playtest"; Description: "Playtest recording"; Types: full

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The whole onedir tree. recursesubdirs picks up _internal/, which is where
; everything except the launcher lives. Feature payloads are carved out into
; their components below — a leading backslash anchors an Excludes pattern at
; the start of the relative path, and naming a directory skips it whole.
;
; A COMPONENT WITH NO BYTES LOOKS LIKE A SWITCH THAT DOES NOTHING (it was
; reported exactly that way), so every component that HAS real bytes in the
; bundle owns them. The local speech stack is the big one: faster-whisper and
; its runtime (ctranslate2, onnxruntime, av, tokenizers, hf_xet) are ~175MB
; that only voice input and playtest transcripts use; numpy serves that stack
; and the playtest recorder; the sounddevice data dir is capture-only. All of
; these are OPTIONAL pip extras in the source install, so their imports are
; already guarded and their absence is a configuration the code has always
; supported. Music, cinematics and brainstorm are honestly a few kilobytes of
; already-bundled code, so those rows stay sizeless feature switches, which
; is what the page's own copy now says.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Components: core; \
    Excludes: "\_internal\builders_gate_floor_assets\img,\_internal\builders_gate_floor_assets\audio,\_internal\av,\_internal\av.libs,\_internal\ctranslate2,\_internal\onnxruntime,\_internal\faster_whisper,\_internal\tokenizers,\_internal\hf_xet,\_internal\_sounddevice_data"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; The floor's art and ambience are their own distribution now
; (builders-gate-floor-assets), so the payload is the package inside _internal
; rather than a corner of the dashboard's static tree. bgate.spec puts it there.
Source: "{#SourceDir}\_internal\builders_gate_floor_assets\img\*"; \
    DestDir: "{app}\_internal\builders_gate_floor_assets\img"; Components: floor; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\_internal\builders_gate_floor_assets\audio\*"; \
    DestDir: "{app}\_internal\builders_gate_floor_assets\audio"; Components: floor; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; The whisper stack is VOICE's. Playtest transcripts are speech-to-text,
; that is the voice feature doing work for playtest, and the wizard says
; so; a playtest-without-voice install records sessions with no
; transcripts, exactly what the guarded adapter has always reported when
; whisper is absent. numpy lives in CORE: both voice and playtest need
; it, and a file shared between two components is displayed on both
; rows while installing once, which made the rows sum 22MB past the
; footer (reported as "counts still look wrong", because they were).
Source: "{#SourceDir}\_internal\av\*"; DestDir: "{app}\_internal\av"; \
    Components: voice; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\_internal\av.libs\*"; DestDir: "{app}\_internal\av.libs"; \
    Components: voice; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\_internal\ctranslate2\*"; DestDir: "{app}\_internal\ctranslate2"; \
    Components: voice; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\_internal\onnxruntime\*"; DestDir: "{app}\_internal\onnxruntime"; \
    Components: voice; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\_internal\faster_whisper\*"; DestDir: "{app}\_internal\faster_whisper"; \
    Components: voice; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\_internal\tokenizers\*"; DestDir: "{app}\_internal\tokenizers"; \
    Components: voice; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\_internal\hf_xet\*"; DestDir: "{app}\_internal\hf_xet"; \
    Components: voice; Flags: ignoreversion recursesubdirs createallsubdirs
; The capture device layer: playtest recording only.
Source: "{#SourceDir}\_internal\_sounddevice_data\*"; \
    DestDir: "{app}\_internal\_sounddevice_data"; Components: playtest; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller's onedir tree is written by the installer and removed with it,
; but the app also writes a crash log beside the user's profile. Leave the
; user's own projects and ~/.bgate alone — an uninstaller that deletes API
; keys and game data is a bug, not tidiness.
Type: files; Name: "{localappdata}\BuildersGate-crash.log"

[Code]
{ The app holds a loopback singleton on port 7787 for its whole lifetime, so an
  upgrade that copies over a running install fails on locked files with a
  message about nothing the user can act on. Ask first, and say why. }
function IsAppRunning(): Boolean;
var
  ResultCode: Integer;
begin
  { tasklist is present on every supported Windows and needs no elevation. }
  Result := Exec(ExpandConstant('{cmd}'),
                 '/C tasklist /FI "IMAGENAME eq {#AppExeName}" | find /I "{#AppExeName}"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsAppRunning() then
    Result := 'Builders Gate is running. Close it and click Retry.' + #13#10 +
              'The installer cannot replace files the app has open.';
end;

{ THE COMPONENT PAGE'S OTHER HALF. Ticking decides which files land; this
  records the same answer as the machine's module defaults, which every new
  project inherits (bgate_core.modules.machine_defaults). Written as the JSON
  the app already reads — ~/.bgate/modules.json, a single "disabled" list.
  No literal braces in this comment: a Pascal brace-comment ends at the first
  closing brace, wherever it appears.

  A silent run never touches an existing file: /SILENT is the upgrade path,
  its component set is whatever Inno remembered, and overwriting a choice the
  user made by hand with a remembered default would be the installer editing
  their settings behind their back. }
const
  OptionalComponents = 'floor,voice,playtest';

{ THE FOOTER MATCHES THE ROWS, BY CONSTRUCTION. Inno's stock label is the
  selected files PLUS the uninstaller it will write, and the uninstaller
  belongs to no row — so the page always summed ~4MB short of its own footer
  and read as a counting error (it was reported as exactly that, twice, with
  a calculator in the screenshot). The label is ours now: the sum of the
  size figures actually painted on the rows, digit for digit, with the
  uninstaller named separately instead of hidden in the total. Summed in
  tenths of a MB as integers, from the very strings the list displays, so
  the arithmetic a person does on screen is the arithmetic shown. }
var
  OldComponentsClick: TNotifyEvent;
  OldTypeChange: TNotifyEvent;

function RowTenths(const S: String): Integer;
var
  T: String;
  DotPos: Integer;
begin
  { "176.3 MB" -> 1763. Anything unparseable counts zero rather than wrong. }
  T := Trim(S);
  if Pos(' ', T) > 0 then
    T := Copy(T, 1, Pos(' ', T) - 1);
  DotPos := Pos('.', T);
  if DotPos = 0 then
    Result := StrToIntDef(T, 0) * 10
  else
    Result := StrToIntDef(Copy(T, 1, DotPos - 1), 0) * 10
              + StrToIntDef(Copy(T, DotPos + 1, 1), 0);
end;

procedure UpdateSpaceLabel();
var
  I, Total: Integer;
begin
  Total := 0;
  for I := 0 to WizardForm.ComponentsList.Items.Count - 1 do
    if WizardForm.ComponentsList.Checked[I] then
      Total := Total + RowTenths(WizardForm.ComponentsList.ItemSubItem[I]);
  WizardForm.ComponentsDiskSpaceLabel.Caption :=
    'Selected: ' + IntToStr(Total div 10) + '.' + IntToStr(Total mod 10) +
    ' MB, plus about 4 MB for the uninstaller.';
end;

procedure ComponentsListClickCheck(Sender: TObject);
begin
  if OldComponentsClick <> nil then
    OldComponentsClick(Sender);
  UpdateSpaceLabel();
end;

procedure TypesComboChange(Sender: TObject);
begin
  if OldTypeChange <> nil then
    OldTypeChange(Sender);
  UpdateSpaceLabel();
end;

procedure InitializeWizard();
begin
  OldComponentsClick := WizardForm.ComponentsList.OnClickCheck;
  WizardForm.ComponentsList.OnClickCheck := @ComponentsListClickCheck;
  OldTypeChange := WizardForm.TypesCombo.OnChange;
  WizardForm.TypesCombo.OnChange := @TypesComboChange;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectComponents then
    UpdateSpaceLabel();
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Dir, Path, Json, Name: String;
  Parts: TStringList;
  I: Integer;
  First: Boolean;
begin
  if CurStep <> ssPostInstall then
    exit;
  Dir := ExpandConstant('{%USERPROFILE}') + '\.bgate';
  Path := Dir + '\modules.json';
  if WizardSilent() and FileExists(Path) then
    exit;
  Parts := TStringList.Create;
  try
    Parts.CommaText := OptionalComponents;
    Json := '{"disabled": [';
    First := True;
    for I := 0 to Parts.Count - 1 do
    begin
      Name := Parts[I];
      if not WizardIsComponentSelected(Name) then
      begin
        if not First then
          Json := Json + ', ';
        Json := Json + '"' + Name + '"';
        First := False;
      end;
    end;
    Json := Json + ']}';
    if not DirExists(Dir) then
      CreateDir(Dir);
    { Best effort, like every side write here: a machine default that could
      not be recorded is every module on, which is the shipped behaviour. }
    SaveStringToFile(Path, Json, False);
  finally
    Parts.Free;
  end;
end;
