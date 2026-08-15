"""What to do with the machine once the downloads have finished.

IDM's scheduler ends with a choice — quit, sleep, hibernate, shut down — and
that is the point of leaving a queue running overnight. This is that choice.

Two decisions worth stating, because both are departures:

1.  **This is the one place the project asks the operating system for
    something.** Powering a machine down is not a protocol that can be
    implemented here: it is a privileged operation owned by the session
    manager, and the way to ask for it is the interface that session manager
    publishes. On Windows that is `shutdown.exe` and `powrprof.dll`; on Linux
    `systemctl`/`loginctl`; on macOS `osascript`/`pmset`. None of them is a
    third-party tool being wrapped — they are the system's own front door.

2.  **Every attempt is recorded, including the ones that worked.** Two
    releases shipped a dead Windows taskbar because a blanket `except`
    swallowed the reason and a backend that had never run looked exactly like
    one that had. So each candidate reports the command it ran and what came
    back, and the caller writes all of it to the Log. On a machine that is
    about to power off, that record is the only thing that will still be there
    afterwards.

Nothing here is called by a test that actually powers a machine down. What the
tests cover is the *choice* — which candidates a platform offers, in what
order, and that an unknown action is refused rather than guessed at.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from enum import Enum


class CompletionAction(str, Enum):
    """What to do when every download has finished."""

    NOTHING = "nothing"
    EXIT = "exit"              # close IXD, leave the machine alone
    SLEEP = "sleep"
    HIBERNATE = "hibernate"
    SHUTDOWN = "shutdown"

    @property
    def label(self) -> str:
        return {
            CompletionAction.NOTHING: "Do nothing",
            CompletionAction.EXIT: "Quit IXD",
            CompletionAction.SLEEP: "Sleep",
            CompletionAction.HIBERNATE: "Hibernate",
            CompletionAction.SHUTDOWN: "Shut down",
        }[self]

    #: Whether the machine — rather than only this application — is affected.
    @property
    def touches_the_machine(self) -> bool:
        return self in {
            CompletionAction.SLEEP,
            CompletionAction.HIBERNATE,
            CompletionAction.SHUTDOWN,
        }


def parse(value: object) -> CompletionAction:
    """A stored string to an action, defaulting to doing nothing.

    Settings files are edited by hand and carried between versions, so an
    unrecognised value must mean "do nothing" rather than raise on a code path
    that runs when a download finishes.
    """
    try:
        return CompletionAction(str(value or "nothing").strip().lower())
    except ValueError:
        return CompletionAction.NOTHING


#: The commands each platform publishes for each action, in the order they are
#: tried. The first that exists *and* returns zero wins.
#:
#: Linux lists `systemctl` before `loginctl` because a desktop session running
#: under logind answers both, while a machine without a session manager may
#: answer neither — in which case the honest outcome is a Log line saying so,
#: not a silent no-op.
_CANDIDATES: dict[str, dict[CompletionAction, list[list[str]]]] = {
    "win32": {
        CompletionAction.SHUTDOWN: [["shutdown", "/s", "/t", "0"]],
        CompletionAction.HIBERNATE: [["shutdown", "/h"]],
        CompletionAction.SLEEP: [
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        ],
    },
    "darwin": {
        CompletionAction.SHUTDOWN: [
            ["osascript", "-e", 'tell application "System Events" to shut down'],
        ],
        CompletionAction.HIBERNATE: [
            ["osascript", "-e", 'tell application "System Events" to sleep'],
        ],
        CompletionAction.SLEEP: [
            ["pmset", "sleepnow"],
            ["osascript", "-e", 'tell application "System Events" to sleep'],
        ],
    },
    "linux": {
        CompletionAction.SHUTDOWN: [
            ["systemctl", "poweroff"],
            ["loginctl", "poweroff"],
            ["shutdown", "-h", "now"],
        ],
        CompletionAction.HIBERNATE: [
            ["systemctl", "hibernate"],
            ["loginctl", "hibernate"],
        ],
        CompletionAction.SLEEP: [
            ["systemctl", "suspend"],
            ["loginctl", "suspend"],
        ],
    },
}


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def candidates(action: CompletionAction,
               platform: str | None = None) -> list[list[str]]:
    """The commands that would be tried for ``action`` on ``platform``."""
    if action in (CompletionAction.NOTHING, CompletionAction.EXIT):
        return []
    table = _CANDIDATES.get(platform or _platform_key(), {})
    return [list(command) for command in table.get(action, [])]


def perform(action: CompletionAction, timeout: float = 20.0) -> tuple[bool, str]:
    """Ask the system to carry ``action`` out.

    Returns ``(succeeded, detail)``. ``detail`` names every command tried and
    what it answered — it is written to the Log by the caller either way,
    because on a machine that is powering off it is the only evidence that
    will survive.

    :data:`CompletionAction.EXIT` returns ``(True, …)`` without running
    anything: quitting is the application's own job, and the caller does it.
    """
    if action is CompletionAction.NOTHING:
        return False, "no action requested"
    if action is CompletionAction.EXIT:
        return True, "quitting the application"

    attempts: list[str] = []
    for command in candidates(action):
        binary = shutil.which(command[0])
        if binary is None:
            attempts.append(f"{command[0]}: not installed")
            continue
        try:
            finished = subprocess.run(          # noqa: S603 - fixed argv, no shell
                [binary, *command[1:]],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            attempts.append(f"{' '.join(command)}: {error}")
            continue
        if finished.returncode == 0:
            attempts.append(f"{' '.join(command)}: ok")
            return True, "; ".join(attempts)
        detail = (finished.stderr or finished.stdout or "").strip().splitlines()
        attempts.append(
            f"{' '.join(command)}: exit {finished.returncode}"
            + (f" — {detail[0][:160]}" if detail else "")
        )

    if not attempts:
        attempts.append(f"no {action.value} command is known for this platform")
    return False, "; ".join(attempts)
