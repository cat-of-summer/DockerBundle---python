"""Global, cross-project user state: ``~/.dockerbundle/config.json``.

Deliberately tiny. Anything that affects generated output belongs in the per-project
``docker-bundle.yml`` so it can be committed and reproduced in CI; only preferences live here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from core.paths import config_file


@dataclass
class Config:
    language: str = ""
    """Empty means "detect from the environment"."""

    sources: list[str] = field(default_factory=list)
    """Recently used catalogue directories, most recent first."""

    @classmethod
    def load(cls) -> Config:
        path = config_file()
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return cls()

        if not isinstance(raw, dict):
            return cls()

        language = raw.get("language")
        sources = raw.get("sources")
        return cls(
            language=language if isinstance(language, str) else "",
            sources=[s for s in sources if isinstance(s, str)] if isinstance(sources, list) else [],
        )

    def save(self) -> None:
        path = config_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(asdict(self), handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            tmp.replace(path)
        except OSError:
            pass

    def remember_source(self, path: str, limit: int = 10) -> None:
        self.sources = [path, *(s for s in self.sources if s != path)][:limit]
