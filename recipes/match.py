"""Recipe registry and service-to-recipe matching."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from core.model import ServiceSpec
from core.paths import resource_dir
from recipes import fallback
from recipes.schema import RawRecipe, Recipe, RecipeError, build_table, read_inline, read_path

if TYPE_CHECKING:  # pragma: no cover - import only for the annotation
    from core.manifest import Manifest


@dataclass
class Registry:
    """Every recipe available to one generation, in layers.

    Three of them, lowest first: the built-in set, whatever ``recipe_paths:`` names, and
    the ``recipes:`` written inline in ``docker-bundle.yml``. A later layer either
    replaces a name outright or, with ``extends:``, builds on what the layer below said —
    so tuning one detail of a built-in runtime no longer means vendoring the whole recipe
    and inheriting its future bugs.
    """

    recipes: dict[str, Recipe] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, manifest: Manifest | None = None) -> Registry:
        warnings: list[str] = []

        builtin, builtin_warnings = read_path(resource_dir("recipes/builtin"))
        warnings.extend(builtin_warnings)
        layers: list[list[RawRecipe]] = [builtin]

        if manifest is not None:
            external: list[RawRecipe] = []
            for entry in manifest.recipe_paths:
                found, path_warnings = read_path(manifest.resolve(entry))
                external.extend(found)
                warnings.extend(path_warnings)
            if external:
                layers.append(external)

            if manifest.recipes:
                origin = manifest.path.name if manifest.path else "docker-bundle.yml"
                inline, inline_warnings = read_inline(manifest.recipes, origin)
                warnings.extend(inline_warnings)
                layers.append(inline)

        table, table_warnings = build_table(layers)
        warnings.extend(table_warnings)
        return cls(recipes=table, warnings=warnings)

    def get(self, name: str) -> Recipe | None:
        return self.recipes.get(name)

    def match(self, spec: ServiceSpec) -> Recipe | None:
        """Return the best-scoring recipe for a service, or ``None``."""
        image = spec.effective_image
        service = spec.name
        files = _files_in(spec.source_dir)
        command = " ".join([*(spec.entrypoint or []), *(spec.command or [])])
        path = spec.source_dir.as_posix() if spec.source_dir is not None else ""
        env = set(spec.environment) | {entry.key for entry in spec.env_vars}

        best: Recipe | None = None
        best_score = 0
        for recipe in self.recipes.values():
            score = recipe.match.score(
                image=image,
                files=files,
                command=command,
                service=service,
                package=spec.package,
                path=path,
                env=env,
            )
            if score == 0:
                continue
            better = score > best_score
            tie = score == best_score and best is not None and recipe.priority > best.priority
            if better or tie:
                best, best_score = recipe, score
        return best

    def resolve(self, spec: ServiceSpec, *, forced: str = "") -> tuple[Recipe, bool]:
        """Return ``(recipe, is_fallback)`` for a service.

        A forced name that does not exist is an error the caller surfaces; otherwise a
        service with no match gets a generated rootfs-import recipe so that generation
        never dead-ends on an unknown image.
        """
        if forced:
            recipe = self.get(forced)
            if recipe is None:
                raise KeyError(forced)
            return recipe, False

        recipe = self.match(spec)
        if recipe is not None:
            return recipe, False
        return fallback.build(spec), True


def _files_in(directory: Path | None) -> set[str]:
    if directory is None or not directory.is_dir():
        return set()
    try:
        return {entry.name for entry in directory.iterdir()}
    except OSError:
        return set()


__all__ = ["Registry", "RecipeError"]
