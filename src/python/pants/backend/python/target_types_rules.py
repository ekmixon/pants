# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Rules for the core Python target types.

This is a separate module to avoid circular dependencies. Note that all types used by call sites are
defined in `target_types.py`.
"""

import dataclasses
import logging
import os.path
from collections import defaultdict
from textwrap import dedent
from typing import DefaultDict, Dict, Generator, Optional, Tuple

from pants.backend.python.dependency_inference.module_mapper import PythonModule, PythonModuleOwners
from pants.backend.python.dependency_inference.rules import PythonInferSubsystem, import_rules
from pants.backend.python.goals.setup_py import InvalidEntryPoint
from pants.backend.python.target_types import (
    EntryPoint,
    PexBinaryDependencies,
    PexEntryPointField,
    PythonDistributionDependencies,
    PythonDistributionEntryPoint,
    PythonDistributionEntryPointsField,
    PythonProvidesField,
    ResolvedPexEntryPoint,
    ResolvedPythonDistributionEntryPoints,
    ResolvePexEntryPointRequest,
    ResolvePythonDistributionEntryPointsRequest,
)
from pants.engine.addresses import Address, Addresses, UnparsedAddressInputs
from pants.engine.fs import GlobMatchErrorBehavior, PathGlobs, Paths
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import (
    Dependencies,
    DependenciesRequest,
    ExplicitlyProvidedDependencies,
    InjectDependenciesRequest,
    InjectedDependencies,
    InvalidFieldException,
    Targets,
    WrappedTarget,
)
from pants.engine.unions import UnionRule
from pants.source.source_root import SourceRoot, SourceRootRequest
from pants.util.docutil import doc_url
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.ordered_set import OrderedSet

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------------------------
# `pex_binary` rules
# -----------------------------------------------------------------------------------------------


@rule(desc="Determining the entry point for a `pex_binary` target", level=LogLevel.DEBUG)
async def resolve_pex_entry_point(request: ResolvePexEntryPointRequest) -> ResolvedPexEntryPoint:
    ep_val = request.entry_point_field.value
    address = request.entry_point_field.address

    # We support several different schemes:
    #  1) `<none>` or `<None>` => set to `None`.
    #  2) `path.to.module` => preserve exactly.
    #  3) `path.to.module:func` => preserve exactly.
    #  4) `app.py` => convert into `path.to.app`.
    #  5) `app.py:func` => convert into `path.to.app:func`.

    # Case #1.
    if ep_val.module in ("<none>", "<None>"):
        return ResolvedPexEntryPoint(None, file_name_used=False)

    # If it's already a module (cases #2 and #3), simply use that. Otherwise, convert the file name
    # into a module path (cases #4 and #5).
    if not ep_val.module.endswith(".py"):
        return ResolvedPexEntryPoint(ep_val, file_name_used=False)

    # Use the engine to validate that the file exists and that it resolves to only one file.
    full_glob = os.path.join(address.spec_path, ep_val.module)
    entry_point_paths = await Get(
        Paths,
        PathGlobs(
            [full_glob],
            glob_match_error_behavior=GlobMatchErrorBehavior.error,
            description_of_origin=f"{address}'s `{request.entry_point_field.alias}` field",
        ),
    )
    # We will have already raised if the glob did not match, i.e. if there were no files. But
    # we need to check if they used a file glob (`*` or `**`) that resolved to >1 file.
    if len(entry_point_paths.files) != 1:
        raise InvalidFieldException(
            f"Multiple files matched for the `{request.entry_point_field.alias}` "
            f"{ep_val.spec!r} for the target {address}, but only one file expected. Are you using "
            f"a glob, rather than a file name?\n\n"
            f"All matching files: {list(entry_point_paths.files)}."
        )
    entry_point_path = entry_point_paths.files[0]
    source_root = await Get(
        SourceRoot,
        SourceRootRequest,
        SourceRootRequest.for_file(entry_point_path),
    )
    stripped_source_path = os.path.relpath(entry_point_path, source_root.path)
    module_base, _ = os.path.splitext(stripped_source_path)
    normalized_path = module_base.replace(os.path.sep, ".")
    return ResolvedPexEntryPoint(
        dataclasses.replace(ep_val, module=normalized_path), file_name_used=True
    )


class InjectPexBinaryEntryPointDependency(InjectDependenciesRequest):
    inject_for = PexBinaryDependencies


@rule(desc="Inferring dependency from the pex_binary `entry_point` field")
async def inject_pex_binary_entry_point_dependency(
    request: InjectPexBinaryEntryPointDependency, python_infer_subsystem: PythonInferSubsystem
) -> InjectedDependencies:
    if not python_infer_subsystem.entry_points:
        return InjectedDependencies()
    original_tgt = await Get(WrappedTarget, Address, request.dependencies_field.address)
    explicitly_provided_deps, entry_point = await MultiGet(
        Get(ExplicitlyProvidedDependencies, DependenciesRequest(original_tgt.target[Dependencies])),
        Get(
            ResolvedPexEntryPoint,
            ResolvePexEntryPointRequest(original_tgt.target[PexEntryPointField]),
        ),
    )
    if entry_point.val is None:
        return InjectedDependencies()
    owners = await Get(PythonModuleOwners, PythonModule(entry_point.val.module))
    address = original_tgt.target.address
    explicitly_provided_deps.maybe_warn_of_ambiguous_dependency_inference(
        owners.ambiguous,
        address,
        # If the entry point was specified as a file, like `app.py`, we know the module must
        # live in the pex_binary's directory or subdirectory, so the owners must be ancestors.
        owners_must_be_ancestors=entry_point.file_name_used,
        import_reference="module",
        context=(
            f"The pex_binary target {address} has the field "
            f"`entry_point={repr(original_tgt.target[PexEntryPointField].value.spec)}`, which "
            f"maps to the Python module `{entry_point.val.module}`"
        ),
    )
    maybe_disambiguated = explicitly_provided_deps.disambiguated(
        owners.ambiguous, owners_must_be_ancestors=entry_point.file_name_used
    )
    unambiguous_owners = owners.unambiguous or (
        (maybe_disambiguated,) if maybe_disambiguated else ()
    )
    return InjectedDependencies(unambiguous_owners)


# -----------------------------------------------------------------------------------------------
# `python_distribution` rules
# -----------------------------------------------------------------------------------------------


def _classify_entry_points(
    all_entry_points: FrozenDict[str, FrozenDict[str, str]]
) -> Generator[Tuple[bool, str, str, str], None, None]:
    """Looks at each entry point to see if it is a target address or not.

    Yields tuples: is_target, category, name, entry_point_str.
    """
    for category, entry_points in all_entry_points.items():
        for name, entry_point_str in entry_points.items():
            yield (
                entry_point_str.startswith(":") or "/" in entry_point_str,
                category,
                name,
                entry_point_str,
            )


@rule(desc="Determining the entry points for a `python_distribution` target", level=LogLevel.DEBUG)
async def resolve_python_distribution_entry_points(
    request: ResolvePythonDistributionEntryPointsRequest,
) -> ResolvedPythonDistributionEntryPoints:
    field_value = request.entry_points_field.value
    if field_value is None:
        return ResolvedPythonDistributionEntryPoints()

    address = request.entry_points_field.address
    classified_entry_points = list(_classify_entry_points(field_value))

    # Pick out all target addresses up front, so we can use MultiGet later.
    #
    # This calls for a bit of trickery however (using the "y_by_x" mapping dicts), so we keep track
    # of which address belongs to which entry point. I.e. the `address_by_ref` and
    # `binary_entry_point_by_address` variables.

    target_refs = [
        entry_point_str for is_target, _, _, entry_point_str in classified_entry_points if is_target
    ]

    # Intermediate step, as Get(Targets) returns a deduplicated set.. which breaks in case of
    # mulitple input refs that maps to the same target.
    target_addresses = await Get(
        Addresses, UnparsedAddressInputs(target_refs, owning_address=address)
    )
    address_by_ref = dict(zip(target_refs, target_addresses))
    targets = await Get(Targets, Addresses, target_addresses)

    # Check that we only have targets with a pex entry_point field.
    for target in targets:
        if not target.has_field(PexEntryPointField):
            raise InvalidFieldException(
                "All target addresses in the entry_points field must be for pex_binary targets, "
                f"but the target {address} includes the value {target.address}, which has the "
                f"target type {target.alias}.\n\n"
                'Alternatively, you can use a module like "project.app:main". '
                f"See {doc_url('python-distributions')}."
            )

    binary_entry_points = await MultiGet(
        Get(
            ResolvedPexEntryPoint,
            ResolvePexEntryPointRequest(target[PexEntryPointField]),
        )
        for target in targets
    )
    binary_entry_point_by_address = {
        target.address: entry_point for target, entry_point in zip(targets, binary_entry_points)
    }

    entry_points: DefaultDict[str, Dict[str, PythonDistributionEntryPoint]] = defaultdict(dict)

    # Parse refs/replace with resolved pex entry point, and validate console entry points have function.
    for is_target, category, name, ref in classified_entry_points:
        owner: Optional[Address] = None
        if is_target:
            owner = address_by_ref[ref]
            entry_point = binary_entry_point_by_address[owner].val
            if entry_point is None:
                logger.warning(
                    f"The entry point {name} in {category} references a pex binary {ref}, "
                    "which has set its entry point to '<none>'. "
                    "Skipping this entry because '<none>' is not valid as an entry point."
                )
                continue
        else:
            entry_point = EntryPoint.parse(ref, f"{name} for {address} {category}")

        if category in ["console_scripts", "gui_scripts"] and not entry_point.function:
            url = "https://python-packaging.readthedocs.io/en/latest/command-line-scripts.html#the-console-scripts-entry-point"
            raise InvalidEntryPoint(
                dedent(
                    f"""\
                Every entry point in `{category}` for {address} must end in the format `:my_func`,
                but {name} set it to {entry_point.spec!r}. For example, set
                `entry_points={{"{category}": {{"{name}": "{entry_point.module}:main}} }}`.
                See {url}.
                """
                )
            )

        entry_points[category][name] = PythonDistributionEntryPoint(entry_point, owner)

    return ResolvedPythonDistributionEntryPoints(
        FrozenDict(
            {category: FrozenDict(entry_points) for category, entry_points in entry_points.items()}
        )
    )


class InjectPythonDistributionDependencies(InjectDependenciesRequest):
    inject_for = PythonDistributionDependencies


@rule
async def inject_python_distribution_dependencies(
    request: InjectPythonDistributionDependencies, python_infer_subsystem: PythonInferSubsystem
) -> InjectedDependencies:
    """Inject dependencies that we can infer from entry points in the distribution."""
    if not python_infer_subsystem.entry_points:
        return InjectedDependencies()

    original_tgt = await Get(WrappedTarget, Address, request.dependencies_field.address)
    explicitly_provided_deps, all_entry_points = await MultiGet(
        Get(ExplicitlyProvidedDependencies, DependenciesRequest(original_tgt.target[Dependencies])),
        Get(
            ResolvedPythonDistributionEntryPoints,
            ResolvePythonDistributionEntryPointsRequest(
                original_tgt.target[PythonDistributionEntryPointsField]
            ),
        ),
    )

    address = original_tgt.target.address
    all_module_entry_points = [
        (category, name, entry_point)
        for category, entry_points in all_entry_points.explicit_modules.items()
        for name, entry_point in entry_points.items()
    ]
    all_module_owners = iter(
        await MultiGet(
            Get(PythonModuleOwners, PythonModule(entry_point.module))
            for _, _, entry_point in all_module_entry_points
        )
    )
    module_owners: OrderedSet[Address] = OrderedSet()
    for (category, name, entry_point), owners in zip(all_module_entry_points, all_module_owners):
        field_str = repr({category: {name: entry_point.spec}})
        explicitly_provided_deps.maybe_warn_of_ambiguous_dependency_inference(
            owners.ambiguous,
            address,
            import_reference="module",
            context=(
                f"The python_distribution target {address} has the field "
                f"`entry_points={field_str}`, which maps to the Python module"
                f"`{entry_point.module}`"
            ),
        )
        maybe_disambiguated = explicitly_provided_deps.disambiguated(owners.ambiguous)
        unambiguous_owners = owners.unambiguous or (
            (maybe_disambiguated,) if maybe_disambiguated else ()
        )
        module_owners.update(unambiguous_owners)

    with_binaries = original_tgt.target[PythonProvidesField].value.binaries
    if not with_binaries:
        with_binaries_addresses = Addresses()
    else:
        # Note that we don't validate that these are all `pex_binary` targets; we don't care about
        # that here. `setup_py.py` will do that validation.
        with_binaries_addresses = await Get(
            Addresses,
            UnparsedAddressInputs(
                with_binaries.values(), owning_address=request.dependencies_field.address
            ),
        )

    return InjectedDependencies(
        Addresses(module_owners) + with_binaries_addresses + all_entry_points.pex_binary_addresses
    )


def rules():
    return (
        *collect_rules(),
        *import_rules(),
        UnionRule(InjectDependenciesRequest, InjectPexBinaryEntryPointDependency),
        UnionRule(InjectDependenciesRequest, InjectPythonDistributionDependencies),
    )
