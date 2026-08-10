"""nwn_wiki.db -- the module database, split one concern per module.

`Db` is a single class with a large surface: it loads the unpacked GFF-as-JSON
tree, builds a dozen derived indexes, parses scripts and dialogs, and answers
name lookups.  Rather than move it wholesale, it is **assembled from mixins**:
each concern lives in its own module here as a plain mixin class, and the
concrete `Db` inherits from all of them.

The convention, which every `db-*` extraction follows:

* One module per concern (`core`, `index`, `scripts`, `dialogs`, `derived`,
  `names`), each defining exactly one mixin class named `Db<Concern>Mixin` --
  except :mod:`nwn_wiki.db.core`, whose class is `DbCore` because it owns
  ``__init__`` and so must sit last in the MRO.
* Mixins never subclass each other and never import each other.  They may call
  ``self.<anything>`` freely: the assembled class is what has to be coherent,
  not the individual mixin.
* The concrete class is declared where it is used, currently as
  ``class Db(<other mixins...>, DbCore)`` in :mod:`nwn_wiki.cli`.  New mixins
  are prepended to the bases list; `DbCore` stays last.  Because mixins are
  disjoint (no method name appears in two of them) the MRO order is not
  load-bearing beyond that rule.
* Nothing here may import :mod:`nwn_wiki.cli` or the renderers -- that would be
  a cycle.  A helper a mixin needs must therefore travel with that mixin, and
  the renderers import it back from here (see ``_conversation_key`` in
  :mod:`nwn_wiki.db.core`, ``_store_instance_slug`` in
  :mod:`nwn_wiki.db.index`).

This package deliberately re-exports nothing; import from the submodules.
"""

from __future__ import annotations
