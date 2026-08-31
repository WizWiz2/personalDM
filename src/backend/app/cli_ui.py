from __future__ import annotations

import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import questionary
from prompt_toolkit.styles import Style


T = TypeVar("T")

_MENU_STYLE = Style(
    [
        ("qmark", "#d8a25b bold"),
        ("question", "bold"),
        ("pointer", "#d8a25b bold"),
        ("highlighted", "#f1c98f bold"),
        ("selected", "#98b88f"),
        ("instruction", "#85857c"),
    ]
)


def _interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _ask_outside_running_loop(prompt):
    """Run questionary's synchronous prompt without nesting asyncio.run().

    The CLI menus are called from async service flows, while questionary's
    synchronous ``ask()`` owns its own event loop.  A tiny worker thread keeps
    those two event-loop lifecycles separate and preserves the interactive
    arrow-key UI.
    """
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="personal-dm-prompt") as pool:
        return pool.submit(prompt.ask).result()


def _fallback_select(message: str, choices: Sequence[tuple[str, T]]) -> T | None:
    """Numbered fallback for redirected stdin, tests and unusual terminals."""
    print(message)
    for index, (label, _) in enumerate(choices, start=1):
        print(f" [{index}] {label}")
    print(" [Q] Назад")
    while True:
        try:
            raw = input("Выбор: ").strip()
        except (EOFError, StopIteration):
            return None
        if raw.casefold() == "q":
            return None
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(choices):
                return choices[index][1]
        print("Неверный выбор.")


def select_menu(message: str, choices: Sequence[tuple[str, T]]) -> T | None:
    """Select one item with arrows/Enter, preserving a non-TTY fallback."""
    if not choices:
        return None
    if not _interactive_terminal():
        return _fallback_select(message, choices)
    try:
        prompt = questionary.select(
            message,
            choices=[questionary.Choice(title=label, value=value) for label, value in choices],
            qmark="",
            pointer="❯",
            style=_MENU_STYLE,
        )
        return _ask_outside_running_loop(prompt)
    except (KeyboardInterrupt, EOFError):
        return None


def confirm_menu(message: str, *, default: bool = False) -> bool:
    """Confirm destructive actions without forcing yes/no text in an interactive TTY."""
    if not _interactive_terminal():
        suffix = "Y/n" if default else "y/N"
        try:
            raw = input(f"{message} [{suffix}]: ").strip().casefold()
        except (EOFError, StopIteration):
            return False
        if not raw:
            return default
        return raw in {"y", "yes", "д", "да"}
    try:
        prompt = questionary.confirm(
            message,
            default=default,
            qmark="",
            style=_MENU_STYLE,
        )
        result = _ask_outside_running_loop(prompt)
        return bool(result)
    except (KeyboardInterrupt, EOFError):
        return False
