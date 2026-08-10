"""Where a page lands in the output tree -- and therefore how it links out.

Every relative URL the wiki emits depends on one fact: how deep in ``docs/``
the page being rendered will be written.  That fact used to be re-stated by
hand at every call site as a ``root_rel`` string (``"."`` / ``".."`` /
``"../.."``) and threaded through the link helpers, whose defaults did not even
agree with each other; correctness rested on an unwritten "only called from
depth 1" rule.

:class:`PageCtx` replaces that with the page's output-root-relative path, from
which both the ``root_rel`` prefix *and* the file actually written are derived
-- see :func:`nwn_wiki.htmlgen.chrome.write_page`.  A page whose links point at
the wrong depth is therefore no longer expressible: there is one path, and the
links and the write location both come from it.

Stdlib only; imported by both the link helpers and the page shell.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class PageCtx:
    """The output path of the page currently being rendered.

    ``rel`` is relative to the wiki output root and always names an HTML file,
    e.g. ``"index.html"``, ``"areas/foo.html"``,
    ``"items/properties/bar.html"``.
    """

    rel: PurePosixPath

    def __post_init__(self) -> None:
        if not isinstance(self.rel, PurePosixPath):
            object.__setattr__(self, "rel", PurePosixPath(self.rel))

    @property
    def dir(self) -> PurePosixPath:
        """The page's containing directory, relative to the output root."""
        return self.rel.parent

    @property
    def depth(self) -> int:
        """How many directories deep the page sits (0 at the output root)."""
        return len(self.rel.parts) - 1

    @property
    def root_rel(self) -> str:
        """Relative prefix from this page back to the output root."""
        return "/".join([".."] * self.depth) if self.depth else "."

    def url(self, target: str) -> str:
        """URL for ``target`` (an output-root-relative path) from this page.

        Kept as ``<root_rel>/<target>`` rather than a shortest-path relative
        URL: that is the form the site has always emitted.
        """
        return f"{self.root_rel}/{target}"

    def dir_url(self, target_dir: str) -> str:
        """URL prefix for files living in ``target_dir``, relative to this page.

        ``""`` when the page is already in that directory, otherwise a prefix
        ending in ``/`` -- so a caller always writes ``f"{prefix}{name}"``.
        Unlike :meth:`url` this is a true shortest relative path, which is what
        the item tables and creature pictures have always emitted.
        """
        rel = posixpath.relpath(target_dir, str(self.dir))
        return "" if rel == "." else rel + "/"

    def path(self, out: Path) -> Path:
        """Where this page is written under the output root ``out``."""
        return out / self.rel
