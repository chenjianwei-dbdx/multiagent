"""CLI runtime context: home resolution, lazy AppContainer, error policy.

Every command runs inside ``with cli.errors():`` which guarantees the P5 error
contract (one ``ERROR [<code>]: <message>`` line on stderr, exit code 1, no
stack trace unless ``--debug``) and closes the ledger when the command ends.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from omas.app.bootstrap import AppContainer, build_app
from omas.config.settings import load_models_config
from omas.domain.errors import OmasError

#: Exit code for "template registered but not activatable" (distinct from
#: hard errors: the registration itself succeeded and is auditable).
EXIT_UNSUPPORTED_TEMPLATE = 2

#: Exit code for every OmasError / input error / internal error.
EXIT_ERROR = 1


def _passthrough_exceptions() -> tuple[type[BaseException], ...]:
    """Exceptions typer itself must render (usage errors, exits, aborts).

    typer 0.27 vendors click (``typer._click``): its ``Exit`` is a plain
    ``RuntimeError`` subclass and its ``ClickException`` hierarchy is distinct
    from the real click's — and neither base is exported. Derive the vendored
    ``ClickException`` from ``typer.BadParameter``'s MRO so exceptions our own
    commands raise reach typer's handler (usage rendering, exit code 2)
    instead of falling into the generic error gate below.
    """
    vendored = [
        klass for klass in typer.BadParameter.__mro__ if klass.__name__ == "ClickException"
    ]
    return (typer.Exit, typer.Abort, *vendored)


_PASSTHROUGH: tuple[type[BaseException], ...] = (*_passthrough_exceptions(), KeyboardInterrupt)


def default_home() -> Path:
    return Path.home() / ".omas"


class CliContext:
    """Per-invocation state stored on ``click.Context.obj``."""

    def __init__(
        self, home: Path, debug: bool = False, models_path: Path | None = None
    ) -> None:
        self.home = home
        self.debug = debug
        self.models_path = models_path
        self._container: AppContainer | None = None

    # ------------------------------------------------------------- container

    def container(self) -> AppContainer:
        """Build (once) the composition root for this invocation."""
        if self._container is None:
            from omas.config.settings import default_models_path

            models_path = self.models_path or default_models_path(self.home)
            self._container = build_app(self.home, load_models_config(models_path))
        return self._container

    def close(self) -> None:
        if self._container is not None:
            self._container.ledger.close()
            self._container = None

    # ----------------------------------------------------------- command text

    @property
    def program(self) -> str:
        """Executable prefix for copy-pasteable example commands."""
        if self.home == default_home():
            return "omas"
        return f"omas --home {self.home}"

    # ------------------------------------------------------------ error gate

    @contextlib.contextmanager
    def errors(self) -> Iterator[None]:
        try:
            yield
        except _PASSTHROUGH:
            raise
        except OmasError as exc:
            self._fail(getattr(exc, "code", "OMAS_ERROR"), str(exc))
        except (ValidationError, ValueError) as exc:
            self._fail("INPUT_INVALID", str(exc))
        except OSError as exc:
            self._fail("IO_ERROR", str(exc))
        except Exception as exc:
            self._fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        finally:
            self.close()

    def _fail(self, code: str, message: str) -> None:
        if self.debug:
            raise
        typer.echo(f"ERROR [{code}]: {message}", err=True)
        raise typer.Exit(code=EXIT_ERROR)


def cli_context(ctx: typer.Context) -> CliContext:
    """Fetch the CliContext installed by the root callback."""
    obj: Any = ctx.obj
    if not isinstance(obj, CliContext):  # pragma: no cover - defensive
        raise typer.BadParameter("CLI context missing; invoke via the omas entry point")
    return obj
