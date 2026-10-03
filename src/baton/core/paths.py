"""Where Baton looks for a profile, and where it keeps working state.

A *profile* is a directory holding one ``baton.yaml`` plus whatever private
material an installation needs: theory notes, message templates, job state.
Keeping all of it in one directory is what lets the private overlay repository
be nothing but config: the code never reaches outside the profile.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from ..errors import ConfigError

CONFIG_FILENAME = "baton.yaml"

#: Environment variable pointing at a profile directory (or directly at a
#: ``baton.yaml``). Set this in a harness container and every command finds it.
PROFILE_ENV = "BATON_PROFILE"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_key(value: str) -> str:
    """A filesystem-safe key for a learner id or folder name.

    Ids and folder names come from a studio's own data and may be anything,
    so they are never used as a path component unescaped. ``_UNSAFE`` keeps
    only ASCII, so a name written entirely in Thai strips to nothing; every
    such learner used to collapse onto one literal ``"unknown"`` file, and one
    child's job record silently became another's. The hash keeps those apart
    while staying deterministic and readable as "this was non-ASCII".
    """
    cleaned = _UNSAFE.sub("_", str(value)).strip("._")
    if not cleaned:
        cleaned = (
            "unknown_"
            + hashlib.sha1(  # noqa: S324 - filename key, not a digest
                str(value).encode("utf-8")
            ).hexdigest()[:12]
        )
    return cleaned[:100]


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw) if raw else Path.home() / ".config"


def candidate_profiles(explicit: str | Path | None = None) -> list[Path]:
    """Profile directories to try, most specific first.

    Order: ``--profile``, then ``$BATON_PROFILE``, then the current directory,
    then ``$XDG_CONFIG_HOME/baton``. The first one containing a ``baton.yaml``
    wins; nothing is merged across profiles, because a half-applied config is
    far harder to debug than a missing one.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_value = os.environ.get(PROFILE_ENV)
    if env_value:
        candidates.append(Path(env_value).expanduser())
    candidates.append(Path.cwd())
    candidates.append(_xdg_config_home() / "baton")
    return candidates


def find_config(explicit: str | Path | None = None) -> Path:
    """Locate the ``baton.yaml`` to use.

    Args:
        explicit: A profile directory or a direct path to a config file.

    Returns:
        Path to an existing config file.

    Raises:
        ConfigError: No profile was found in any candidate location.
    """
    for candidate in candidate_profiles(explicit):
        if candidate.is_file():
            return candidate
        config = candidate / CONFIG_FILENAME
        if config.is_file():
            return config

    searched = [str(c) for c in candidate_profiles(explicit)]
    listing = "\n".join(f"  - {c}" for c in searched)
    raise ConfigError(
        f"No {CONFIG_FILENAME} found in any of:\n{listing}",
        remedy=f"Run `baton init` to create one, or set {PROFILE_ENV} to a profile directory.",
        details={"searched": searched},
    )


def state_dir(profile_dir: Path) -> Path:
    """Directory for mutable run state (staging, job files, caches).

    Overridable with ``BATON_STATE_DIR`` so a container can mount state on a
    volume separate from the read-only config.
    """
    override = os.environ.get("BATON_STATE_DIR")
    path = Path(override).expanduser() if override else profile_dir / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path
