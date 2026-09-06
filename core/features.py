"""Build-time boolean switches and the ``when:`` conditions that read them.

A stand is rarely one fixed shape: the same project wants MySQL baked in on a laptop and
left outside in production, or a CUDA variant only where a GPU exists. Expressing that by
commenting blocks out of the configuration loses the alternative; expressing it with
``features:`` keeps both shapes in the file and records which one was built.

``when:`` accepts a single flag name, a negation (``!mysql``), or a list meaning "all of
these". An unknown name is an error rather than a silent ``false``: a typo would
otherwise drop a service from the image without saying a word.
"""

from __future__ import annotations

from typing import Any


class FeatureError(ValueError):
    """Raised for an unusable ``features:`` table or ``when:`` expression."""


def parse_features(raw: Any, *, where: str = "features") -> dict[str, bool]:
    """Read the ``features:`` mapping into plain booleans."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FeatureError(f"{where} must be a mapping of name -> true/false")

    result: dict[str, bool] = {}
    for name, value in raw.items():
        key = str(name)
        if not isinstance(value, bool):
            raise FeatureError(f"{where}.{key} must be true or false, got {value!r}")
        result[key] = value
    return result


def apply_overrides(
    features: dict[str, bool], *, enable: list[str], disable: list[str]
) -> dict[str, bool]:
    """Return ``features`` with command-line overrides applied.

    Unknown names are refused here too. A ``--enable`` for a flag the configuration never
    declares means one of the two is out of date, and guessing which would produce an
    image nobody asked for.
    """
    result = dict(features)
    for name in enable:
        if name not in result:
            raise FeatureError(f"--enable {name}: no such feature in docker-bundle.yml")
        result[name] = True
    for name in disable:
        if name not in result:
            raise FeatureError(f"--disable {name}: no such feature in docker-bundle.yml")
        result[name] = False
    return result


def normalise(raw: Any, *, where: str) -> list[str]:
    """Validate a ``when:`` expression into a list of terms.

    ``None`` means "no condition" and returns an empty list, which always evaluates true.
    """
    if raw is None:
        return []
    if isinstance(raw, bool):
        raise FeatureError(
            f"{where}: when: takes a feature name, not {str(raw).lower()}; "
            f"drop the key instead"
        )
    terms = [raw] if isinstance(raw, str) else raw
    if not isinstance(terms, list):
        raise FeatureError(f"{where}: when: must be a name, a !name, or a list of them")

    result: list[str] = []
    for term in terms:
        text = str(term).strip()
        name = text[1:].strip() if text.startswith("!") else text
        if not name:
            raise FeatureError(f"{where}: when: has an empty term")
        result.append(("!" + name) if text.startswith("!") else name)
    return result


def names(terms: list[str]) -> set[str]:
    """The feature names a normalised expression refers to."""
    return {term[1:] if term.startswith("!") else term for term in terms}


def evaluate(terms: list[str], features: dict[str, bool], *, where: str = "when") -> bool:
    """True when every term of the expression holds."""
    for term in terms:
        negated = term.startswith("!")
        name = term[1:] if negated else term
        if name not in features:
            raise FeatureError(
                f"{where}: unknown feature {name!r}; declare it under features: "
                f"in docker-bundle.yml"
            )
        if features[name] == negated:
            return False
    return True


__all__ = [
    "FeatureError",
    "apply_overrides",
    "evaluate",
    "names",
    "normalise",
    "parse_features",
]
