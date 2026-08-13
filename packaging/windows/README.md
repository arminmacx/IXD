# Building the Windows version

**This cannot be done from Linux.** PyInstaller does not cross-compile: it
freezes an application by bundling *the interpreter and the extension modules of
the machine it runs on*. A Windows build needs a Windows Python, a Windows
PySide6 wheel and a Windows PyInstaller — there is no flag that produces one
from here, and anything claiming otherwise produces a file that will not start.

Everything that *can* be prepared here has been: the spec, the entry point, the
`.ico`, the packaging step and both extension zips are platform-neutral and
compile cleanly. What follows is the whole procedure on a Windows machine, and
it is four commands.

---

## What your friend needs

* **Windows 10 or 11, 64-bit.**
* **Python 3.11 or newer**, 64-bit, from python.org — tick *Add python.exe to
  PATH* in the installer. (3.14 matches the development machine; anything from
  3.11 works, because PySide6 ships an `abi3` wheel.)
* This folder, copied across. `.venv/`, `dist/` and `build/` can be left behind —
  they are Linux artefacts and are rebuilt.

## One script

**Double-click `packaging\windows\build.bat`**. That is the whole of
it. It creates the environment, installs PySide6 and PyInstaller, builds,
registers the browser bridge, checks that the files it expected actually exist,
and stops with the window open so the result can be read.

Everything it prints also goes into **`build-windows.log`** beside it. If
anything fails, that file is the thing to send back — it holds pip's output and
PyInstaller's, which is where the reason always is.

> It moves to its own folder before doing anything. Double-clicking a script
> from Explorer starts it wherever Explorer happened to be, and every relative
> path after that points at nothing — which is what an earlier version got
> wrong, and it failed silently because the window closed before anything could
> be read.

If you prefer to run the steps by hand, from a Command Prompt **in the project
folder**:

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install PySide6==6.11.1 pyinstaller
.venv\Scripts\python packaging\build.py --package
```

Either way the results are in `dist\`:

| Artefact | What it is |
|---|---|
| `dist\ixd\ixd.exe` | the application |
| `dist\ixd-<version>-windows-x64.zip` | the same, zipped, to send on |
| `dist\ixd-extension-chrome-<version>.zip` | the browser extension |

## The browser bridge

`build-windows.bat` does this already. By hand it is:

```bat
.venv\Scripts\python native-host\install_host.py
.venv\Scripts\python native-host\install_host.py --verify
```

The first writes the native-messaging registry entries for every Chrome-family
browser and Firefox it finds; the second launches the host exactly as a browser
does and checks the reply. It should print **`Every registered launcher
answered.`** If it does not, that message is the thing to send back.

Load the extension: `chrome://extensions` → Developer mode → **Load unpacked** →
the `extension` folder. The extension's ID is fixed by a key in its manifest, so
no copying of IDs is needed and the host is already registered for it.

## Running it

`dist\ixd\ixd.exe` — or the `.exe` inside the
zip after unpacking. It needs no installation and writes its settings and
database under `%LOCALAPPDATA%\ixd`.

## What to send back

The **Log** button in the toolbar holds everything the engine and the extension
have reported, with a **Copy all** button. That is the useful report: it names
the container of every file it assembled, every capture the extension made, and
the reason for every refusal. A description of a symptom costs a round trip;
the log usually does not.

## What has never been run on Windows

Every Windows path in this project is implemented and none of it has been
executed — this machine is Linux. The parts most likely to need a correction,
in the order they are likely to hit:

1. **Native-messaging registration**, which on Windows is registry keys rather
   than files (`HKCU\Software\Google\Chrome\NativeMessagingHosts\…`).
2. **Interface binding**, which falls back to a source-address bind rather than
   `SO_BINDTODEVICE`.
3. **The system proxy**, read from the registry rather than GSettings.
4. **Path handling** anywhere a filename came from a site — Windows forbids
   characters that Linux allows, and `sanitize_filename` is what stands between
   a video title and an unopenable file.
