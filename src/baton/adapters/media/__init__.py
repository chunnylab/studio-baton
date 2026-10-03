"""Media drivers, selected by configuration."""

from __future__ import annotations

from ...core.config import Config
from ...errors import ConfigError
from .base import (
    CombineResult,
    EncodeProfile,
    MediaSource,
    SourceClip,
    UploadResult,
    VideoEncoder,
    VideoPublisher,
)
from .ffmpeg import FfmpegEncoder

#: Drivers this build implements, per role.
SOURCE_DRIVERS = ("gdrive", "local")
ENCODER_DRIVERS = ("ffmpeg",)
PUBLISHER_DRIVERS = ("youtube",)


def open_source(config: Config) -> MediaSource:
    """Build the configured clip source."""
    driver = str(config.get("media.source.driver", "gdrive"))
    if driver == "gdrive":
        from .google import DriveSource

        return DriveSource.from_config(config)
    if driver == "local":
        from .local import LocalSource

        return LocalSource.from_config(config)
    raise ConfigError(
        f"Unknown media source driver `{driver}`.",
        remedy=f"Set media.source.driver to one of: {', '.join(SOURCE_DRIVERS)}.",
    )


def open_encoder(config: Config) -> VideoEncoder:
    """Build the configured encoder."""
    driver = str(config.get("media.encode.driver", "ffmpeg"))
    if driver == "ffmpeg":
        return FfmpegEncoder(binary=str(config.get("media.encode.binary", "ffmpeg")))
    raise ConfigError(
        f"Unknown encoder driver `{driver}`.",
        remedy=f"Set media.encode.driver to one of: {', '.join(ENCODER_DRIVERS)}.",
    )


def open_publisher(config: Config) -> VideoPublisher:
    """Build the configured publisher."""
    driver = str(config.get("media.publish.driver", "youtube"))
    if driver == "youtube":
        from .google import YouTubePublisher

        return YouTubePublisher.from_config(config)
    raise ConfigError(
        f"Unknown publisher driver `{driver}`.",
        remedy=f"Set media.publish.driver to one of: {', '.join(PUBLISHER_DRIVERS)}.",
    )


def encode_profile(config: Config) -> EncodeProfile:
    """The configured encode profile."""
    return EncodeProfile(
        name=str(config.get("media.encode.profile", "auto")),
        timeout_seconds=int(config.get("media.encode.timeout_minutes", 30)) * 60,
        extra_args=[str(item) for item in config.get("media.encode.extra_args", [])],
        codec=str(config.get("media.encode.codec", "libx264")),
        hwaccel=str(config.get("media.encode.hwaccel", "") or ""),
        tone_map_hdr=bool(config.get("media.encode.tone_map_hdr", True)),
        fps=int(config.get("media.encode.fps", 0)),
        copy_when_safe=bool(config.get("media.encode.copy_when_safe", True)),
    )


__all__ = [
    "ENCODER_DRIVERS",
    "PUBLISHER_DRIVERS",
    "SOURCE_DRIVERS",
    "CombineResult",
    "EncodeProfile",
    "FfmpegEncoder",
    "MediaSource",
    "SourceClip",
    "UploadResult",
    "VideoEncoder",
    "VideoPublisher",
    "encode_profile",
    "open_encoder",
    "open_publisher",
    "open_source",
]
