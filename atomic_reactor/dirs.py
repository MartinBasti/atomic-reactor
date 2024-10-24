"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging
import reflink
import re

from pathlib import Path
import shutil
from shutil import copytree
from typing import Dict, List, Callable, Iterable, Optional, TypeVar

from dockerfile_parse import DockerfileParser

from atomic_reactor.constants import (
    DOCKERFILE_FILENAME,
    EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE,
    EXPORTED_SQUASHED_IMAGE_NAME,
    INSPECT_CONFIG,
)
from atomic_reactor.source import Source
from atomic_reactor.types import ImageInspectionData

logger = logging.getLogger(__name__)


def reflink_copy(src, dst, *, follow_symlinks=True):
    if follow_symlinks:
        reflink.reflink(str(Path(src).resolve()), str(dst))
    else:
        reflink.reflink(str(src), str(dst))


class DockerfileNotExist(Exception):
    """Dockerfile does not exist."""


class BuildDirIsNotInitialized(Exception):
    """Build directories are not initialized."""

    def __init__(self, msg: Optional[str] = None):
        super().__init__(msg or "Build directory is not initialized yet.")


class BuildDir(object):
    """Representing a directory which is specific to a platform."""

    def __init__(self, path: Path, platform: str) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Build directory {path} does not exist.")
        self.path = path
        self.platform = platform
        self.exported_squashed_image: Path = self.path / EXPORTED_SQUASHED_IMAGE_NAME

    def exported_compressed_image(self, ext: str) -> Path:
        """Return the filename of an exported compressed image.

        :param str ext: the extension of the filename for a specific type of compress algorithm.
        :return: the absolute path to the file. Nothing is created in the filesystem.
        :rtype: pathlib.Path
        """
        return self.path / EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE.format(ext)

    @property
    def dockerfile_path(self) -> Path:
        """An absolute path to the dockerfile within this build directory."""
        f = self.path / DOCKERFILE_FILENAME
        real_path = f.resolve()
        if real_path.parent != self.path:
            raise DockerfileNotExist(
                f"Dockerfile is linked from {real_path}, which is not supported."
            )
        return f

    @property
    def dockerfile(self) -> DockerfileParser:
        """Return the parsed Dockerfile.

        :return: the parsed Dockerfile.
        :rtype: DockerfileParser
        """
        return DockerfileParser(str(self.dockerfile_path))

    @staticmethod
    def _get_env_from_inspection(data: ImageInspectionData) -> Optional[Dict[str, str]]:
        """Get environment variables defined by ENV.

        :param data: the data inspected from an image.
        :type data: dict[str, any]
        :return: a mapping of environment variables got from the image
            inspection data. If no Env is found from the inspection data, None
            will be returned.
        :rtype: None or dict[str, str]
        """
        envs = data.get(INSPECT_CONFIG, {}).get("Env")
        if envs is None:
            return None
        if isinstance(envs, dict):
            return envs
        if isinstance(envs, list):
            return dict(item.split("=", 1) for item in envs)
        raise TypeError(f"Unexpected envs type: {type(envs)}; {envs!r}")

    def dockerfile_with_parent_env(self, parent_inspect: ImageInspectionData) -> DockerfileParser:
        """Get the parsed Dockerfile with parent information injected.

        :param parent_inspect: a mapping containing the image inspection data,
            which is used as parent image's inspection data to be injected into
            the parsed Dockerfile.
        :type parent_inspect: dict[str, any]
        :return: the parsed Dockerfile
        :rtype: DockerfileParser
        """
        envs = self._get_env_from_inspection(parent_inspect)
        if envs is None:
            logger.debug("Parent Environment not found, not applied to Dockerfile")
        return DockerfileParser(str(self.dockerfile_path), parent_env=envs)


FileCreationFunc = Callable[[BuildDir], Iterable[Path]]

T = TypeVar("T")


class RootBuildDir(object):
    """A directory containing all artifacts for building images.

    The following is a typical directory structure:

    /path/to/root_build_dir
    |-- aarch64/
    |-- ppc64le/
    |-- s390x/
    +-- x86_64/
    """

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Path {path} does not exist.")
        self.path = path
        self.platforms: List[str] = []

    @property
    def source_container_sources_dir(self) -> Path:
        """The directory holding sources for building a source container."""
        path: Path = self.path / "sources"
        path.mkdir(exist_ok=True)
        return path

    @property
    def source_container_output_dir(self) -> Path:
        """The directory holding the output from bsi for a source container build."""
        path: Path = self.path / "output"
        path.mkdir(exist_ok=True)
        return path

    def _copy_sources(self, source: Source) -> None:
        """Create platform-specific build directories from source.

        :param source: the original source from where to copy the content into
            every platform-specific directory.
        :type source: Source
        """
        src_path = source.path

        copy_method = shutil.copy2
        if reflink.supported_at(self.path):
            copy_method = reflink_copy
        logger.debug("copy method used for copy sources: %s", copy_method.__name__)

        for platform in self.platforms:
            copytree(src_path, self.path / platform, symlinks=True, copy_function=copy_method)

    @property
    def has_sources(self) -> bool:
        """Check if all platform-specific directories exist.

        :return: True if all platform-specific directories exist. Otherwise,
            False is returned.
        :rtype: bool
        """
        if not self.platforms:
            return False
        for platform in self.platforms:
            if not self.path.joinpath(platform).exists():
                return False
        return True

    def init_build_dirs(self, platforms: List[str], source: Source) -> None:
        """Initialize the root build directory with specific determined platforms.

        :param list[str] platforms: a list of platforms to build for.
        """
        self.platforms = sorted(platforms)
        if self.has_sources:
            return
        self._copy_sources(source)

    @property
    def any_platform(self) -> BuildDir:
        """Get a platform-specific build directory.

        This is typically used by code that does not care about platform. The
        returned directory is guaranteed to be the same one between the
        multiple calls.

        :return: a source directory.
        :rtype: BuildDir
        """
        if not self.has_sources:
            raise BuildDirIsNotInitialized()
        return self.platform_dir(self.platforms[0])

    def platform_dir(self, platform: str) -> BuildDir:
        """Get the build directory for the specified platform."""
        return BuildDir(self.path / platform, platform)

    def for_each_platform(self, action: Callable[[BuildDir], T]) -> Dict[str, T]:
        """Apply an action on every platform-specific directory.

        The action callable will be applied to the platform-specific
        directories in the order of the platforms specified when initializing
        this RootBuildDir object. The caller should not depend on or assume the
        order how action is applied.

        The action callable might raise errors, that will cause the
        ``for_each`` terminates immediately and the raised error is propagated
        to the caller. As a result, the action will not be applied to the rest
        of the platforms.

        :param action: a callable object that will be applied on every
            platform-specific directory. This callable must accept one single
            argument in BuildDir type, and it can return data in any type.
        :type action: Callable
        :return: a mapping from platform to the value returned from the
            function which is called for that platform.
        :rtype: dict[str, any]
        """
        if not self.has_sources:
            raise BuildDirIsNotInitialized()
        results: Dict[str, T] = {}
        for platform in self.platforms:
            results[platform] = action(self.platform_dir(platform))
        return results

    def for_all_platforms_copy(self, action: FileCreationFunc) -> List[Path]:
        """Ensure created files are present in all platform-specific directories.

        ``for_all_copy`` accepts either absolute or relative path returned from
        the action callable. Both normal file and directory are acceptable.
        Whatever the form of the path, it must be relative to the
        platform-specific directory where the file or directory is created.

        :param action: a callable that creates files in a given build directory
            and returns the files it created. The action accepts one single
            argument in type BuildDir, and returns an iterable object that
            yields paths of the created files.
        :type action: callable
        :return: the list of absolute paths of the created files.
        :rtype: list[pathlib.Path]
        """
        if not self.has_sources:
            raise BuildDirIsNotInitialized()

        build_dir = self.platform_dir(self.platforms[0])
        created_files = action(build_dir)

        the_new_files: List[Path] = []
        file_path: Path
        for file_path in created_files:
            if not file_path.is_absolute():
                file_path = build_dir.path / file_path
            file_path = file_path.resolve()
            if file_path == build_dir.path:
                raise ValueError(
                    f"{file_path} should not be added as a created directory."
                )
            try:
                file_path.relative_to(build_dir.path)
            except ValueError as e:
                raise ValueError(
                    f"File must be created inside the build directory. "
                    f"File {file_path} is not allowed."
                ) from e
            if not file_path.exists():
                raise FileNotFoundError(
                    f"{file_path} does not exist inside build directory."
                )
            the_new_files.append(file_path)

        copy_method = shutil.copy2
        if reflink.supported_at(self.path):
            copy_method = reflink_copy
        logger.debug("copy method used for all platforms copy: %s", copy_method.__name__)

        for platform in self.platforms[1:]:
            for src_file in the_new_files:
                dest = self.path / platform / src_file.relative_to(build_dir.path)

                if src_file.is_dir():
                    copytree(src_file, dest, symlinks=True, copy_function=copy_method)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    copy_method(src_file, dest, follow_symlinks=False)

        return the_new_files


class ContextDir:
    """Represents the context directory holding data through the whole image build pipeline."""

    def __init__(self, path: Path):
        if not path.exists():
            logger.debug("Context directory %s does not exist yet. Create it.", path)
            path.mkdir(parents=True)
        self._path = path
        self.workflow_json = path / "workflow.json"

    def get_platform_dir(self, platform: str) -> Path:
        """Get the directory specific to the specified platform.

        :param str platform: the platform of the directory. The value must be a
            valid platform supported by OSBS, e.g. x86_64.
        :return: path of the platform specific directory.
        :rtype: pathlib.Path
        :raise ValueError: if value of argument ``platform`` is empty or None.
        """
        if not platform:
            raise ValueError("No platform is specified.")
        platform_dir = self._path / platform
        platform_dir.mkdir(exist_ok=True)
        return platform_dir

    def get_platform_build_log(self, platform: str) -> Path:
        """Get platform-specific build log file."""
        return self._path / f"{platform}-build.log"

