import os
import sys
import importlib
import importlib.util
import inspect
from pathlib import Path
from typing import Any, Protocol
from atlas_provider_sqlalchemy import parser

import sqlalchemy as sa


class DBTableDesc(Protocol):
    """Database table description (SQLAlchemy table or model)."""

    metadata: sa.MetaData


class ModuleImportError(Exception):
    pass


class ModelsNotFoundError(Exception):
    pass


def sqlalchemy_version() -> tuple[int, ...]:
    """Get major and minor version of sqlalchemy."""

    return tuple(int(x) for x in sa.__version__.split("."))


def create_mock_engine(url: str, executor: Any) -> Any:
    """Create a "mock" engine used for echoing DDL."""

    if sqlalchemy_version() < (1, 4):
        return sa.create_engine(url, strategy="mock", executor=executor)
    else:
        return sa.create_mock_engine(url, executor)


def resolve_package_module(file_path: Path) -> "tuple[str, Path] | None":
    """Resolve a file inside a package to its dotted module name and import root.

    Returns `None` for a file that is not part of a package, which is the
    common case and keeps the path-based import below as the default.

    A file whose directory holds an `__init__.py` belongs to a package, and
    importing it under a synthetic top-level name strips it of that context, so
    any relative import inside it fails. Walking up while `__init__.py` keeps
    appearing finds the top-level package; its parent is the directory that
    needs to be on `sys.path` for the dotted name to resolve.
    """

    if not (file_path.parent / "__init__.py").exists():
        return None

    parts = [] if file_path.name == "__init__.py" else [file_path.stem]
    package = file_path.parent
    while (package / "__init__.py").exists():
        parts.append(package.name)
        package = package.parent

    return ".".join(reversed(parts)), package


def import_module_from_path(file_path: Path, imported: "set[str]") -> "Any | None":
    """Import a single file, preserving package context where there is any.

    `imported` accumulates the dotted names already loaded during this walk, so
    a package imported as a side effect of one of its submodules is not
    executed a second time. Re-executing it would define the same models twice
    against the same declarative base, which SQLAlchemy warns about and which
    leaves the string-lookup table pointing at the later copy.
    """

    resolved = resolve_package_module(file_path)

    if resolved is None:
        # Not in a package: import by location under a unique name that
        # includes the file's modification time, to avoid caching issues that
        # can occur after git branch switches.
        prefix = f"atlas_dynamic_module_{abs(hash(str(file_path.absolute())))}"
        file_mtime = int(file_path.stat().st_mtime)
        unique_module_name = f"{prefix}_{file_mtime}"

        # Clear any existing module with similar names from sys.modules
        for name in list(sys.modules):
            if name.startswith(prefix):
                del sys.modules[name]

        # Also invalidate import caches to force fresh loading
        importlib.invalidate_caches()

        module_spec = importlib.util.spec_from_file_location(
            unique_module_name,
            file_path,
        )
        if module_spec is None or module_spec.loader is None:
            return None
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        return module

    module_name, search_root = resolved

    if module_name in imported:
        return sys.modules.get(module_name)

    root = str(search_root.absolute())
    if root not in sys.path:
        # Left in place for the rest of the walk rather than removed straight
        # after the import: a model module may defer an import into a function
        # body, and that has to keep resolving.
        sys.path.insert(0, root)

    # Drop any previous copy for the same reason as above -- a stale module
    # cached from another branch would silently win over the file on disk.
    for name in list(sys.modules):
        is_this_module = name == module_name or name.startswith(f"{module_name}.")
        if is_this_module and name not in imported:
            del sys.modules[name]

    importlib.invalidate_caches()

    module = importlib.import_module(module_name)

    # Importing a submodule executes its package too, so record everything that
    # actually landed rather than just the name that was asked for.
    imported.update(
        name
        for name in sys.modules
        if name == module_name or name.startswith(f"{module_name.split('.')[0]}.")
    )
    imported.add(module_name)

    return module


def get_metadata(db_dir: Path, skip_errors: bool = False) -> sa.MetaData:
    """Walk the directory tree starting at the root, import all models and
    tables, and return metadata for one of them, as they all keep a reference
    to the `MetaData` object.  The way SQLAlchemy works, you must import all
    models and tables in order for them to be registered in metadata.
    """

    metadata: set[sa.MetaData] = set()
    imported: set[str] = set()
    original_sys_path = list(sys.path)

    try:
        for root, _, _ in os.walk(db_dir):
            python_file_paths = Path(root).glob("*.py")
            for file_path in python_file_paths:
                try:
                    module = import_module_from_path(file_path, imported)
                except Exception as e:
                    if skip_errors:
                        print(
                            f"Skipping {file_path}: {e.__class__.__name__}: {e!s}",
                            file=sys.stderr,
                        )
                        continue

                    raise ModuleImportError(
                        f"{e.__class__.__name__}: {str(e)} in {file_path}"
                    )

                if module is None:
                    continue

                ms = {
                    v.metadata
                    for (_, v) in inspect.getmembers(module)
                    if hasattr(v, "metadata") and isinstance(v.metadata, sa.MetaData)
                }
                metadata.update(ms)
    finally:
        sys.path[:] = original_sys_path

    if not metadata:
        raise ModelsNotFoundError(
            "Found no sqlalchemy models/tables in the directory tree."
        )

    return metadata.pop()


def get_file_directives(db_dir: Path, metadata: sa.MetaData) -> list[str]:
    """Get all file directives from the given directory."""
    directives = []
    for root, _, files in os.walk(db_dir):
        for file in files:
            if file.endswith(".py"):
                # Parse the file for directives
                file_path = Path(root) / file
                abs_file_path = file_path.absolute()
                visitor = parser.parse_file(file_path)
                # Extract table directives
                if visitor.tables:
                    for table_name, (line_number, _) in visitor.tables.items():
                        # If table_name is not in metadata, skip it
                        if table_name not in metadata.tables:
                            continue
                        directive = f"atlas:pos {table_name}[type=table] {abs_file_path}:{line_number}"
                        directives.append(directive)
    return directives


def dump_ddl(
    dialect_driver: str, metadata: list[sa.MetaData], directives: list[str]
) -> list[sa.MetaData]:
    """Dump DDL statements for the given metadata to stdout."""

    def dump(sql, *multiparams, **params):
        print(
            str(sql.compile(dialect=engine.dialect))
            .replace("\t", "")
            .replace("\n", ""),
            end=";\n\n",
        )

    """Add File directives to the DDL dump."""
    if directives:
        for directive in directives:
            print(f"-- {directive}")
        print()
    engine = create_mock_engine(f"{dialect_driver}://", dump)
    for meta in metadata:
        meta.create_all(engine, checkfirst=False)
    return metadata


def print_ddl(dialect_driver: str, models: list[DBTableDesc]) -> None:
    """Dump DDL statements for the metadata from the given models/tables to stdout."""
    if len(models) == 0:
        print("No models provided", file=sys.stderr)
        return
    dump_ddl(
        dialect_driver=dialect_driver, metadata=[models[0].metadata], directives=[]
    )
