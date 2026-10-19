"""Agent engines: the slice of tools and the policy one chat run works with.

An *engine* pairs a system prompt with the tools it may offer:

- the built-in ``knowledge`` engine (:data:`KNOWLEDGE`): the core tools
  (``ToolSpec.module is None``, today ``search_documents``) under the
  grounding and citation rules of :data:`~app.modules.agent.prompt.SYSTEM_PROMPT`;
  it is also the safe default;
- one engine per catalog module (:func:`module_engine`): only that module's
  tools, under the shared security and language rules around the module's
  own instructions (its manifest's
  :class:`~app.modules.catalog.manifest.AgentProfile`, or the manifest
  description and :data:`~app.modules.agent.prompt.GENERIC_MODULE_INSTRUCTIONS`
  without one).

:func:`available_engines` lists the engines a user may get; the run narrows
its tools with :func:`visible_specs`. An engine never widens anything: the
user's role and the tenant's active modules filter the tools first, and the
executor checks both again on every call.
"""

from collections.abc import Collection
from dataclasses import dataclass

from app.modules.agent.prompt import GENERIC_MODULE_INSTRUCTIONS, SYSTEM_PROMPT, module_prompt
from app.modules.catalog.exceptions import UnknownModuleError
from app.modules.catalog.manifest import ModuleManifest
from app.modules.catalog.registry import ModuleCatalog
from app.modules.tools.executor import role_satisfies
from app.modules.tools.registry import ToolRegistry, ToolSpec, ToolUser

#: Key of the built-in engine (reserved in the module catalog).
KNOWLEDGE_ENGINE = "knowledge"


@dataclass(frozen=True)
class Engine:
    """One engine: ``module`` is ``None`` for ``knowledge``, else the module key.

    The engine offers exactly the tools whose ``ToolSpec.module`` equals
    ``module`` (so ``knowledge`` offers the core tools).
    ``routing_description`` is what the router reads to pick it.
    """

    key: str
    name: str
    routing_description: str
    system_prompt: str
    module: str | None = None


#: The built-in engine: questions answered from the organization's documents.
KNOWLEDGE = Engine(
    key=KNOWLEDGE_ENGINE,
    name="Knowledge base",
    routing_description=(
        "Questions answered from the organization's documents and knowledge base "
        "(policies, procedures, records), greetings, and general help about what the "
        "assistant can do. The safe default when no other engine clearly fits."
    ),
    system_prompt=SYSTEM_PROMPT,
)


def module_engine(manifest: ModuleManifest) -> Engine:
    """The engine of one catalog module, from its agent profile or the fallback."""
    if manifest.agent is not None:
        routing_description = manifest.agent.routing_description
        instructions = manifest.agent.instructions
    else:
        routing_description = manifest.description
        instructions = GENERIC_MODULE_INSTRUCTIONS
    return Engine(
        key=manifest.key,
        name=manifest.name,
        routing_description=routing_description,
        system_prompt=module_prompt(manifest.name, instructions),
        module=manifest.key,
    )


def visible_specs(
    registry: ToolRegistry,
    user: ToolUser,
    active_modules: Collection[str],
    engine: Engine | None = None,
) -> list[ToolSpec]:
    """Specs ``user`` may be offered, in registration order.

    A spec is visible when the user's role satisfies its ``required_role``
    and its module is available (core tools always, module tools only when
    the module is in ``active_modules``). With an ``engine`` the result is
    further narrowed to that engine's tools; ``None`` keeps every visible tool.
    """
    return [
        spec
        for spec in registry.list_specs()
        if role_satisfies(user, spec.required_role)
        and (spec.module is None or spec.module in active_modules)
        and (engine is None or spec.module == engine.module)
    ]


def available_engines(
    *,
    catalog: ModuleCatalog | None,
    registry: ToolRegistry,
    user: ToolUser,
    active_modules: Collection[str],
) -> list[Engine]:
    """The engines ``user`` may get: :data:`KNOWLEDGE` first, then modules by key.

    A module gets an engine when it is active for the tenant, registered in
    ``catalog``, and owns at least one tool the user's role satisfies.
    Modules unknown to the catalog are skipped; without a catalog only
    :data:`KNOWLEDGE` is available.
    """
    engines = [KNOWLEDGE]
    if catalog is None:
        return engines
    modules = {spec.module for spec in visible_specs(registry, user, active_modules)}
    for key in sorted(module for module in modules if module is not None):
        try:
            manifest = catalog.get(key)
        except UnknownModuleError:
            continue
        engines.append(module_engine(manifest))
    return engines
