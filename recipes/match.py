"""Recipe registry and service-to-recipe matching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.model import ServiceSpec
from core.paths import PROJECT_RECIPES_DIRNAME, resource_dir
from recipes import fallback
from recipes.schema import Recipe, load_dir


@dataclass
class Registry:
    """Built-in recipes, overlaid with any the project defines.

    A project-local ``recipes/foo.yml`` replaces the built-in of the same name outright,
    so a user can retune a runtime without vendoring the whole set.
    """

    recipes: dict[str, Recipe]
    warnings: list[str]

    @classmethod
    def load(cls, project_dir: Path | None = None) -> Registry:
        warnings: list[str] = []
        table: dict[str, Recipe] = {}

        builtin, builtin_warnings = load_dir(resource_dir("recipes/builtin"))
        warnings.extend(builtin_warnings)
        for recipe in builtin:
            table[recipe.name] = recipe

        if project_dir is not None:
            local, local_warnings = load_dir(project_dir / PROJECT_RECIPES_DIRNAME)
            warnings.extend(local_warnings)
            for recipe in local:
                table[recipe.name] = recipe

        return cls(recipes=table, warnings=warnings)

    def get(self, name: str) -> Recipe | None:
        return self.recipes.get(name)

    def match(self, spec: ServiceSpec) -> Recipe | None:
        """Return the best-scoring recipe for a service, or ``None``."""
        image = spec.effective_image
        service = spec.name
        files = _files_in(spec.source_dir)
        command = " ".join([*(spec.entrypoint or []), *(spec.command or [])])

        best: Recipe | None = None
        best_score = 0
        for recipe in self.recipes.values():
            score = recipe.match.score(
                image=image,
                files=files,
                command=command,
                service=service,
                package=spec.package,
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
